"""Run the explicit local Catalog/Wiki query surface on IPv4 loopback.

This entry point is local-only: it snapshots the supplied Catalog into a new
workspace and does not discover credentials or activate a deployment.
"""
from __future__ import annotations

import argparse
import json
import re
import signal
import socket
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlparse

import uvicorn

from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.local_asset_binding_review_queue import LocalAssetBindingReviewQueue
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.catalog import _materialize_catalog
from knowledge_platform.local.workspace import open_persistent_workspace


class LocalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The CLI owns lifecycle cleanup. Do not re-raise SIGTERM before the
        # outer finally can remove the readiness file.
        previous = {sig: signal.signal(sig, self.handle_exit)
                    for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            yield
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def _output_path(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    # Permit only the standard macOS aliases, not arbitrary symlink parents.
    for prefix in ("/tmp", "/var"):
        if str(candidate).startswith(prefix + "/") and Path(prefix).is_symlink():
            candidate = Path("/private" + str(candidate))
            break
    for parent in (candidate, *candidate.parents):
        if parent.is_symlink():
            raise ValueError("output path contains a symlink")
    if candidate.exists():
        raise FileExistsError("output must be new")
    return candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path)
    parser.add_argument("--wiki-root", type=Path)
    for name in ("temp-dir", "ready-file"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--console-origin")
    parser.add_argument("--asset-binding-review-queue", type=Path)
    parser.add_argument("--database-config", type=Path, help="explicit host-local PostgreSQL/Vanna configuration")
    parser.add_argument("--structured-config", type=Path, help="explicit local CSV/TSV asset bindings")
    parser.add_argument("--wiki-config", type=Path, help="explicit Wiki source bindings and HTTP model configuration; requires state-dir")
    parser.add_argument("--instance-id", help="supervisor-owned runtime instance identity")
    args = parser.parse_args()
    if args.state_dir is None and (args.catalog is None or args.wiki_root is None):
        parser.error("--catalog and --wiki-root are required without --state-dir")
    if not 1 <= args.port <= 65535:
        parser.error("port must be in 1..65535")
    if args.instance_id is not None and not re.fullmatch(r"[0-9a-f]{32}", args.instance_id):
        parser.error("instance-id must be 32 lowercase hexadecimal characters")
    if args.console_origin:
        origin = urlparse(args.console_origin)
        if (origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
                or origin.username or origin.password or origin.path not in {"", "/"}
                or origin.params or origin.query or origin.fragment):
            parser.error("console origin must be an explicit loopback HTTP origin")
        _ = origin.port
    database_config = None
    if args.database_config:
        from knowledge_platform.local.database import load_database_config
        try:
            database_config = load_database_config(args.database_config)
        except (ValueError, OSError, TypeError):
            parser.exit(2, "Invalid local database configuration\n")
    structured_config = None
    if args.structured_config:
        from knowledge_platform.local.structured import load_structured_config
        try:
            structured_config = load_structured_config(args.structured_config)
        except (ValueError, OSError, TypeError):
            parser.exit(2, "Invalid local structured configuration\n")
    wiki_config = None
    if args.wiki_config:
        if args.state_dir is None:
            parser.error("wiki-config requires an explicit persistent state-dir")
        from knowledge_platform.local.wiki import load_wiki_config
        try:
            wiki_config = load_wiki_config(args.wiki_config)
        except (ValueError, OSError, TypeError):
            parser.exit(2, "Invalid local Wiki configuration\n")
    ready = _output_path(args.ready_file)
    persistent = (open_persistent_workspace(args.state_dir, catalog=args.catalog, wiki_root=args.wiki_root)
                  if args.state_dir is not None else nullcontext(None))
    # Hold the persistent workspace lock for the entire server lifetime.
    with persistent as owned, socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        # Permit restart after closed connections enter TIME_WAIT; an active
        # listener still prevents this process from claiming the same port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", args.port))
        if owned is None:
            workspace = _output_path(args.temp_dir)
            workspace.mkdir(mode=0o700)
            catalog = workspace / "knowledge-platform.sqlite3"
            materialized = _materialize_catalog(args.catalog, catalog, args.wiki_root)
        else:
            catalog = owned["catalog"]
            materialized = owned
        database_services = {}
        database_scopes = ()
        if database_config:
            from knowledge_platform.local.database import DATABASE_SCOPES, build_database_services
            try:
                database_services = build_database_services(database_config, catalog)
            except Exception:
                parser.exit(2, "Local database binding failed; check configuration, optional dependencies and source availability\n")
            database_scopes = DATABASE_SCOPES
        structured_services = {}
        structured_scopes = ()
        if structured_config:
            from knowledge_platform.local.structured import STRUCTURED_SCOPES, build_structured_services
            try:
                structured_services = build_structured_services(structured_config, catalog)
            except Exception:
                parser.exit(2, "Local structured binding failed; check approved Assets and source digests\n")
            structured_scopes = STRUCTURED_SCOPES
        wiki_services = {}
        if wiki_config:
            from knowledge_platform.local.wiki import build_wiki_services
            from knowledge_platform.local.wiki_query import PublishedWikiReader
            try:
                services = build_wiki_services(wiki_config, catalog, args.state_dir / "processing")
                published = PublishedWikiReader(SqliteCatalogQueryRepository(catalog), services)
                wiki_services = {"wiki_compilation": services.wiki_compilation,
                                 "wiki_provider": published, "wiki_blob_reader": published}
            except (ValueError, OSError, TypeError):
                parser.exit(2, "Local Wiki configuration or owned state is invalid\n")
        principal = Principal(subject_id="knowledge-local", scopes=(
            "knowledge.list", "knowledge.read", "knowledge.query", "knowledge.search",
            "knowledge.space:space_kb_default",
            *database_scopes,
            *structured_scopes,
            *(("knowledge.processing",) if wiki_config else ()),
            *(("knowledge.admin",) if args.asset_binding_review_queue else ()),
        ))
        app = _build_app(
            SqliteCatalogQueryRepository(catalog), materialized["file_bindings"], principal,
            **database_services,
            **structured_services,
            **wiki_services,
            asset_binding_review_queue=(LocalAssetBindingReviewQueue(args.asset_binding_review_queue)
                                        if args.asset_binding_review_queue else None),
        )
        if args.console_origin:
            from fastapi.middleware.cors import CORSMiddleware
            app.add_middleware(CORSMiddleware, allow_origins=[args.console_origin.rstrip("/")],
                               allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["content-type"])
        if args.instance_id is not None:
            instance_id = args.instance_id

            @app.middleware("http")
            async def add_instance_identity(request, call_next):
                response = await call_next(request)
                response.headers["X-PuddingKnowledge-Instance"] = instance_id
                return response
        with ready.open("x", encoding="utf-8") as stream:
            json.dump({"status": "ready", "pages": materialized["pages"],
                       "activation_allowed": False, "database_configured": bool(database_config),
                       "structured_configured": bool(structured_config),
                       "wiki_configured": bool(wiki_config), "persistent": owned is not None}, stream)
        try:
            LocalServer(uvicorn.Config(app, log_level="error", lifespan="off")).run(sockets=[listener])
        finally:
            ready.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
