"""Replay a local QueryResult artifact through the MCP Resource read edge.

The shadow copies the staged local Catalog, upgrades only that copy to the
current schema, binds one existing local QueryResult to an existing local
Space, and supplies a temporary host-owned artifact file.  It proves that
MCP reads require an explicit QueryResult-to-Space binding and matching Space
scope.  It never binds or mutates the canonical Catalog and never persists
artifact content, local paths, or credentials in the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from knowledge_contracts import Principal
from knowledge_platform.catalog import (
    SqliteCatalogQueryRepository,
    migrate_to_latest,
)
from knowledge_platform.catalog.query_result_scope import SqliteQueryResultScopeStore
from knowledge_platform.retrieval import (
    LocalFilesystemQueryResultBlobReader,
    QueryResultArtifactReadService,
)
from scripts.phase8_local_platform_http_shadow import _build_app

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DEFAULT_WIKI_ROOT = Path("/Users/pet/Documents/knowledge/llm-wiki/wiki")
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase9-local-query-result-artifact-shadow-report.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _read_summary(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"http_status": response.status_code, "status": "invalid_json"}
    result = payload.get("result") if isinstance(payload, Mapping) else None
    structured = result.get("structuredContent") if isinstance(result, Mapping) else None
    if not isinstance(structured, Mapping):
        return {"http_status": response.status_code, "status": "invalid"}
    provenance = structured.get("provenance")
    contents = result.get("contents") if isinstance(result, Mapping) else None
    content = contents[0] if isinstance(contents, list) and contents else None
    return {
        "http_status": response.status_code,
        "status": structured.get("status"),
        "error_code": (structured.get("error") or {}).get("code")
        if isinstance(structured.get("error"), Mapping)
        else None,
        "space_id": provenance.get("space_id") if isinstance(provenance, Mapping) else None,
        "content_present": isinstance(content, Mapping) and bool(content.get("blob")),
    }


def _local_fixture_bytes(wiki_root: Path) -> bytes:
    candidates = sorted(
        path
        for path in wiki_root.rglob("*.md")
        if path.is_file() and not path.is_symlink() and ".obsidian" not in path.parts
    )
    if not candidates:
        raise FileNotFoundError("local Wiki has no regular Markdown fixture")
    source = candidates[0]
    source_digest = _sha256(source)
    # This is a portable shadow payload derived from local data; the source
    # path itself is intentionally not placed in the artifact or report.
    return json.dumps(
        {"source_digest": source_digest, "sample_bytes": source.stat().st_size},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8") + b"\n"


def run_shadow(*, catalog_path: Path, wiki_root: Path, output_path: Path) -> dict[str, Any]:
    catalog_path = catalog_path.expanduser().absolute()
    wiki_root = wiki_root.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    canonical_before = _sha256(catalog_path)
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-local-query-result-artifact-shadow/v1",
        "status": "PHASE9_LOCAL_QUERY_RESULT_ARTIFACT_SHADOW_FAILED",
        "activation": "not-activated",
        "activation_allowed": False,
        "mode": "explicit-query-result-space-binding-mcp-read-only",
        "catalog": {"canonical_sha256_before": canonical_before, "canonical_unchanged": False},
        "binding": {"query_result_found": False, "space_found": False, "explicit_binding_created_in_copy": False},
        "authorized_read": {},
        "wrong_space_read": {},
        "unbound_read": {},
    }

    with tempfile.TemporaryDirectory(prefix="phase9-local-query-result-artifact-shadow-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        temporary_catalog = temp_root / "knowledge-platform.sqlite3"
        shutil.copy2(catalog_path, temporary_catalog)
        engine = create_engine(f"sqlite:///{temporary_catalog}")
        with engine.begin() as connection:
            migrate_to_latest(connection)
        engine.dispose()

        with sqlite3.connect(temporary_catalog) as connection:
            query_result = connection.execute(
                "SELECT id, artifact_uri FROM knowledge_query_results WHERE status = 'ready' ORDER BY created_at DESC, id LIMIT 1"
            ).fetchone()
            space = connection.execute("SELECT id FROM knowledge_spaces ORDER BY id LIMIT 1").fetchone()
        if query_result is None or space is None:
            raise RuntimeError("local Catalog has no ready QueryResult and Space")
        query_result_id, resource_uri = str(query_result[0]), str(query_result[1])
        space_id = str(space[0])
        report["binding"].update({"query_result_found": True, "space_found": True})
        content = _local_fixture_bytes(wiki_root)
        artifact_path = temp_root / "query-result.jsonl"
        artifact_path.write_bytes(content)
        artifact_digest = _sha256(artifact_path)
        with sqlite3.connect(temporary_catalog) as connection:
            profile = connection.execute(
                "SELECT profile_json FROM knowledge_query_results WHERE id = ?", (query_result_id,)
            ).fetchone()
            profile_json = json.loads(str(profile[0])) if profile and profile[0] else {}
            profile_json = {"_artifact_sha256": artifact_digest}
            connection.execute(
                "UPDATE knowledge_query_results SET profile_json = ? WHERE id = ?",
                (json.dumps(profile_json, ensure_ascii=False, sort_keys=True), query_result_id),
            )
            connection.commit()

        repository = SqliteCatalogQueryRepository(temporary_catalog)
        scope_store = SqliteQueryResultScopeStore(temporary_catalog)
        scope_store.bind_query_result(query_result_id=query_result_id, space_id=space_id)
        metadata = repository.get_query_result(query_result_id=query_result_id)
        report["binding"].update(
            {
                "explicit_binding_created_in_copy": True,
                "artifact_uri_bound": isinstance(metadata, Mapping)
                and metadata.get("artifact_uri") == resource_uri,
                "artifact_digest_present": isinstance(metadata, Mapping)
                and isinstance(metadata.get("profile"), Mapping)
                and isinstance(metadata["profile"].get("_artifact_sha256"), str),
                "scope_space_id_verified": scope_store.get_space_id(query_result_id=query_result_id) == space_id,
            }
        )
        reader = LocalFilesystemQueryResultBlobReader({resource_uri: artifact_path})
        service = QueryResultArtifactReadService(
            catalog=repository,
            reader=reader,
            scope_reader=scope_store,
        )
        allowed_principal = Principal(
            "phase9-local-query-result-artifact-shadow",
            scopes=("knowledge.read", f"knowledge.space:{space_id}"),
        )
        wrong_space_principal = Principal(
            "phase9-local-query-result-artifact-shadow-wrong-space",
            scopes=("knowledge.read", "knowledge.space:space_not_allowed"),
        )
        app = _build_app(repository, {}, allowed_principal, query_result_artifact=service)
        with TestClient(app) as client:
            report["authorized_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-query-result-authorized",
                        "method": "resources/read",
                        "params": {"uri": resource_uri, "start": 0, "end": 4096},
                    },
                )
            )

        wrong_app = _build_app(repository, {}, wrong_space_principal, query_result_artifact=service)
        with TestClient(wrong_app) as client:
            report["wrong_space_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-query-result-wrong-space",
                        "method": "resources/read",
                        "params": {"uri": resource_uri, "start": 0, "end": 4096},
                    },
                )
            )

        unbound_service = QueryResultArtifactReadService(catalog=repository, reader=reader)
        unbound_app = _build_app(repository, {}, allowed_principal, query_result_artifact=unbound_service)
        with TestClient(unbound_app) as client:
            report["unbound_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-query-result-unbound",
                        "method": "resources/read",
                        "params": {"uri": resource_uri, "start": 0, "end": 4096},
                    },
                )
            )

    canonical_after = _sha256(catalog_path)
    report["catalog"].update(
        {
            "canonical_sha256_after": canonical_after,
            "canonical_unchanged": canonical_before == canonical_after,
        }
    )
    if (
        report["catalog"]["canonical_unchanged"]
        and report["binding"]["explicit_binding_created_in_copy"]
        and report["authorized_read"].get("status") == "ok"
        and report["authorized_read"].get("space_id") == space_id
        and report["authorized_read"].get("content_present") is True
        and report["wrong_space_read"].get("error_code") == "permission_denied"
        and report["unbound_read"].get("error_code") == "binding_unavailable"
    ):
        report["status"] = "PHASE9_LOCAL_QUERY_RESULT_ARTIFACT_SHADOW_PASS_NOT_ACTIVATABLE"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run_shadow(catalog_path=args.catalog, wiki_root=args.wiki_root, output_path=args.output)
    print(json.dumps({"status": report["status"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
