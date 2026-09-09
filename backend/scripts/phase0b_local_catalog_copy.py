"""Stage independent Catalog databases from the current local PuddingClaw data.

This is a development-data command, not a production cutover command.  It
backs up the configured local SQLite Catalog into a temporary consistent
snapshot, copies the sanitized ownership slices into two independent target
databases, and records physical references that are no longer reachable.
The targets are never activated by this script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from sqlalchemy import MetaData, Table, create_engine, func, inspect, select

from knowledge_platform.catalog import run_catalog_migration_rehearsal
from knowledge_platform.catalog.connector_rehearsal import _physical_path_references
from runtime_identity.paths import PuddingClawPaths

_REFERENCE_FIELDS: dict[str, tuple[str, ...]] = {
    "knowledge_documents": ("source_path", "storage_path"),
    "knowledge_source_items": ("path_json",),
    "knowledge_table_assets": ("storage_path", "profile_path"),
    "knowledge_import_jobs": ("source_path",),
    "read_later_items": ("storage_path", "raw_snapshot_path", "source_path"),
    "semantic_dimension_build_jobs": (
        "staging_path",
        "published_reference_path",
        "requested_scope",
        "input_snapshot",
        "result_summary",
    ),
}
_CONTENT_DIGEST_FIELDS = {
    "knowledge_documents": "content_sha256",
    "knowledge_import_jobs": "source_sha256",
    "knowledge_table_assets": "content_sha256",
}


class _LocalSQLiteDrain:
    """Fence writers to one local SQLite file without issuing write SQL.

    ``BEGIN IMMEDIATE`` waits for any existing writer and then holds SQLite's
    reserved lock while the staged copy is read. This is a real local
    database-level writer fence, but it is not evidence about an external
    process supervisor or a production deployment.
    """

    def __init__(self, source: Path) -> None:
        self.source = source.resolve()
        self.connection: sqlite3.Connection | None = None
        self.events: list[dict[str, Any]] = []

    def enter(self, *, installation_id: str, reason: str) -> None:
        self.connection = sqlite3.connect(f"file:{self.source}?mode=rw", uri=True, timeout=10, isolation_level=None)
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.connection.execute("BEGIN IMMEDIATE")
        self.events.append({"event": "enter", "installation_id": installation_id, "reason": reason})
        self.events.append({"event": "writer_fence", "mode": "sqlite-begin-immediate", "write_sql_issued": False})

    def assert_drained(self) -> None:
        if self.connection is None or not self.connection.in_transaction:
            raise RuntimeError("local SQLite writer fence is not held")
        self.connection.execute("SELECT 1").fetchone()
        self.events.append({"event": "assert_drained", "scope": "local-sqlite-writer-fence"})

    def exit(self) -> None:
        if self.connection is not None:
            self.connection.rollback()
            self.connection.close()
            self.connection = None
        self.events.append({"event": "exit"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _database_fingerprint(path: Path) -> dict[str, dict[str, Any]]:
    files: dict[str, dict[str, Any]] = {}
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_file():
            files[candidate.name] = {"bytes": candidate.stat().st_size, "sha256": _sha256(candidate)}
    return files


def _consistent_sqlite_snapshot(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(destination)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()


def _reference_path(reference: str, *, base_dir: Path) -> Path | None:
    if reference.startswith("file://"):
        parsed = urlparse(reference)
        if parsed.netloc not in {"", "localhost"}:
            return None
        return Path(unquote(parsed.path)) if parsed.path else None
    path = Path(reference).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _physical_references(source: Path) -> set[str]:
    return set(_physical_reference_origins(source))


def _iter_physical_references(value: Any, *, locator: str) -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(reference, locator) for reference in _physical_path_references(value)]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in _iter_physical_references(
                child, locator=f"{locator}/{str(key).replace('~', '~0').replace('/', '~1')}"
            )
        ]
    if isinstance(value, (list, tuple)):
        return [
            item
            for index, child in enumerate(value)
            for item in _iter_physical_references(child, locator=f"{locator}/{index}")
        ]
    return []


def _physical_reference_origins(source: Path) -> dict[str, list[dict[str, Any]]]:
    source_uri = f"sqlite+pysqlite:///file:{quote(str(source.resolve()), safe='/')}?mode=ro&uri=true"
    engine = create_engine(source_uri)
    origins: dict[str, list[dict[str, Any]]] = {}
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            for table_name, fields in _REFERENCE_FIELDS.items():
                if table_name not in inspector.get_table_names():
                    continue
                table = Table(table_name, MetaData(), autoload_with=connection)
                primary_key = tuple(inspector.get_pk_constraint(table_name).get("constrained_columns") or ())
                for row_index, row in enumerate(connection.execute(select(table)).mappings()):
                    row_key = {
                        column: str(row.get(column))
                        for column in primary_key
                        if row.get(column) is not None
                    }
                    if len(row_key) != len(primary_key):
                        row_key = {
                            "row_digest": hashlib.sha256(
                                json.dumps(dict(row), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
                            ).hexdigest()
                        }
                    source_digest_field = _CONTENT_DIGEST_FIELDS.get(table_name)
                    source_digest = row.get(source_digest_field) if source_digest_field else None
                    if source_digest:
                        source_digest = str(source_digest)
                        if not source_digest.startswith("sha256:"):
                            source_digest = f"sha256:{source_digest}"
                    for field in fields:
                        if field in table.c:
                            for reference, locator in _iter_physical_references(
                                row.get(field), locator=f"/{field}"
                            ):
                                origins.setdefault(reference, []).append(
                                    {
                                        "table": table_name,
                                        "field": field,
                                        "locator": locator,
                                        "row_key": row_key,
                                        **({"source_content_digest": source_digest} if source_digest else {}),
                                    }
                                )
    finally:
        engine.dispose()
    return {
        reference: sorted(values, key=lambda item: (item["table"], item["field"], item["locator"], str(item["row_key"])))
        for reference, values in origins.items()
    }


def _candidates(reference: str, roots: tuple[Path, ...]) -> list[dict[str, Any]]:
    name = Path(urlparse(reference).path or reference).name
    result: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob(name):
            if path.is_file():
                result.append({"path": str(path), "bytes": path.stat().st_size, "sha256": _sha256(path)})
    return sorted({item["path"]: item for item in result}.values(), key=lambda item: item["path"])


def _candidate_verification(
    candidates: list[dict[str, Any]], origins: list[dict[str, Any]]
) -> dict[str, Any]:
    digest_origins = [origin for origin in origins if origin.get("source_content_digest")]
    for candidate in candidates:
        matching_origins = [
            origin for origin in digest_origins if origin["source_content_digest"] == candidate["sha256"]
        ]
        candidate["content_digest_match"] = bool(
            digest_origins and len(digest_origins) == len(origins) and len(matching_origins) == len(digest_origins)
        )
        candidate["matching_origin_count"] = len(matching_origins)
        candidate["digest_origin_count"] = len(digest_origins)
    confirmed = sum(bool(candidate["content_digest_match"]) for candidate in candidates)
    if not candidates:
        status = "no-candidate"
    elif confirmed == 1 and len(candidates) == 1:
        status = "unique-content-digest-confirmed-review-required"
    elif confirmed > 0:
        status = "ambiguous-content-digest-confirmed"
    else:
        status = "basename-only-unconfirmed"
    return {
        "status": status,
        "candidate_count": len(candidates),
        "content_digest_confirmed_candidate_count": confirmed,
    }


def _reference_exists(reference: str, *, base_dir: Path) -> bool:
    path = _reference_path(reference, base_dir=base_dir)
    return path is not None and (path.is_file() or path.is_dir())


def _promote_targets(
    staged: tuple[Path, ...], finals: tuple[Path, ...], *, replace: bool, staging_dir: Path
) -> None:
    """Promote the pair with rollback if either individual rename fails."""

    backups: dict[Path, Path] = {}
    try:
        for final in finals:
            if final.exists():
                if not replace:
                    raise RuntimeError(f"target already exists: {final}")
                backup = staging_dir / f"{final.name}.previous"
                os.replace(final, backup)
                backups[final] = backup
        for staged_path, final in zip(staged, finals, strict=True):
            os.replace(staged_path, final)
    except Exception:
        for final in finals:
            if final.exists() and final not in backups:
                final.unlink()
            elif final in backups and final.exists():
                final.unlink()
        for final, backup in backups.items():
            if backup.exists():
                os.replace(backup, final)
        raise


def _table_counts(path: Path) -> dict[str, int]:
    engine = create_engine(f"sqlite:///{path}")
    counts: dict[str, int] = {}
    try:
        with engine.connect() as connection:
            for name in inspect(connection).get_table_names():
                table = Table(name, MetaData(), autoload_with=connection)
                counts[name] = int(connection.execute(select(func.count()).select_from(table)).scalar_one())
    finally:
        engine.dispose()
    return dict(sorted(counts.items()))


def stage_local_catalog(
    *, source: Path, output_dir: Path, reference_roots: tuple[Path, ...], replace: bool = False
) -> dict[str, Any]:
    source = source.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    platform_path = output_dir / "knowledge-platform.sqlite3"
    harness_path = output_dir / "harness.sqlite3"
    final_paths = (platform_path.resolve(), harness_path.resolve())
    if source in final_paths:
        raise RuntimeError("source Catalog must not be one of the generated target paths")
    existing_targets = [path for path in (platform_path, harness_path) if path.exists()]
    if existing_targets and not replace:
        raise RuntimeError(
            "target Catalog files already exist; pass --replace to rebuild only these generated files: "
            + ", ".join(str(path) for path in existing_targets)
        )
    with tempfile.TemporaryDirectory(prefix=".phase0b-local-catalog-", dir=output_dir) as temp_dir:
        staging_dir = Path(temp_dir)
        snapshot = staging_dir / "source.sqlite3"
        _consistent_sqlite_snapshot(source, snapshot)
        reference_origins = _physical_reference_origins(snapshot)
        references = sorted(reference_origins)
        missing = []
        for reference in references:
            if _reference_exists(reference, base_dir=source.parent):
                continue
            origins = reference_origins[reference]
            candidates = _candidates(reference, reference_roots)
            missing.append(
                {
                    "reference": reference,
                    "origins": origins,
                    "candidates": candidates,
                    "candidate_verification": _candidate_verification(candidates, origins),
                }
            )
        staged_platform = staging_dir / "knowledge-platform.sqlite3"
        staged_harness = staging_dir / "harness.sqlite3"
        source_engine = create_engine(f"sqlite:///{snapshot}")
        platform_engine = create_engine(f"sqlite:///{staged_platform}")
        harness_engine = create_engine(f"sqlite:///{staged_harness}")
        drain = _LocalSQLiteDrain(snapshot)
        try:
            manifest = run_catalog_migration_rehearsal(
                source_engine,
                platform_engine,
                harness_engine,
                installation_id="puddingclaw-local",
                source_revision="local-catalog-20260903",
                target_revision="platform-catalog-20260903",
                active_revision="legacy-local-catalog",
                drain_controller=drain,
                # The copy is intentionally staged even when old local file
                # paths are gone.  Reachability is reported below and blocks
                # activation; it is never silently called VERIFIED.
                file_reference_checker=lambda _reference: True,
            )
            structural_manifest = manifest.to_dict()
            structural_manifest.pop("state", None)
            structural_manifest["activation_state"] = "NOT_ACTIVATABLE_STAGE"
            structural_manifest["verification_scope"] = (
                "structural copy and adapter checks executed; physical file reachability was not asserted"
            )
            structural_manifest["local_drain"] = {
                "events": drain.events,
                "database_level_writer_fence": True,
                "process_supervisor_fence": False,
            }
        finally:
            source_engine.dispose()
            platform_engine.dispose()
            harness_engine.dispose()
        _promote_targets(
            (staged_platform, staged_harness),
            final_paths,
            replace=replace,
            staging_dir=staging_dir,
        )
        source_snapshot_sha256 = _sha256(snapshot)
        source_snapshot_counts = _table_counts(snapshot)

    result = {
        "format": "agent-knowledge-platform-local-catalog-stage/v1",
        "status": "STAGED_LOCAL_DATA" if not missing else "STAGED_LOCAL_DATA_FILE_REACHABILITY_BLOCKED",
        "activation": "not-activated",
        "source": {
            "path": str(source),
            "snapshot_sha256": source_snapshot_sha256,
            "live_files": _database_fingerprint(source),
            "table_counts": source_snapshot_counts,
            "physical_reference_count": len(references),
        },
        "targets": {
            "platform": {
                "path": str(platform_path.resolve()),
                "bytes": platform_path.stat().st_size,
                "sha256": _sha256(platform_path),
                "files": _database_fingerprint(platform_path),
                "table_counts": _table_counts(platform_path),
            },
            "harness": {
                "path": str(harness_path.resolve()),
                "bytes": harness_path.stat().st_size,
                "sha256": _sha256(harness_path),
                "files": _database_fingerprint(harness_path),
                "table_counts": _table_counts(harness_path),
            },
        },
        "generator": {
            "script": str(Path(__file__).resolve()),
            "sha256": _sha256(Path(__file__).resolve()),
        },
        "physical_reference_verification": {
            "checked_exact_paths": True,
            "missing_count": len(missing),
            "missing": missing,
            "policy": "missing references block activation; candidates include content hashes but remain suggestions only",
        },
        "structural_copy_execution": structural_manifest,
        "decisions": {
            "delivery_term": "Collection",
            "vanna": "maintain-local-vendored-fork",
            "archive_owner": "PuddingClaw project maintains archive/restore and migration scripts; final organization policy remains a separate approval",
        },
    }
    missing_records = result["physical_reference_verification"]["missing"]
    result["physical_reference_triage"] = {
        "resolution_counts": {
            "no_candidate": sum(not item["candidates"] for item in missing_records),
            "unique_candidate": sum(len(item["candidates"]) == 1 for item in missing_records),
            "multiple_candidates": sum(len(item["candidates"]) > 1 for item in missing_records),
        },
        "content_digest_resolution_counts": dict(
            sorted(
                Counter(item["candidate_verification"]["status"] for item in missing_records).items()
            )
        ),
        "origin_table_counts": dict(
            sorted(
                Counter(origin["table"] for item in missing_records for origin in item["origins"]).items()
            )
        ),
        "extension_counts": dict(
            sorted(
                Counter(Path(item["reference"]).suffix.lower() or "<no-extension>" for item in missing_records).items()
            )
        ),
        "review_policy": "candidate paths require human approval and content-digest confirmation before restore/rebind",
    }
    report_path = output_dir / "local-catalog-stage-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    paths = PuddingClawPaths.from_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=paths.databases() / "catalog.sqlite3")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts/phase0b-local-catalog"))
    parser.add_argument(
        "--reference-root",
        type=Path,
        action="append",
        default=[Path.home() / "Documents" / "knowledge", paths.knowledge()],
        help="root used only to suggest replacements for stale physical references",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace only the two generated target Catalog files in --output-dir",
    )
    args = parser.parse_args()
    result = stage_local_catalog(
        source=args.source,
        output_dir=args.output_dir,
        reference_roots=tuple(args.reference_root),
        replace=args.replace,
    )
    print(json.dumps({"status": result["status"], "report": str(args.output_dir / "local-catalog-stage-report.json")}, ensure_ascii=False))
    print(json.dumps(result["physical_reference_verification"], ensure_ascii=False))
    return 0 if result["status"] == "STAGED_LOCAL_DATA" else 2


if __name__ == "__main__":
    raise SystemExit(main())
