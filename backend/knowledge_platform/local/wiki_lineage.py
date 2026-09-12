"""Pure, fail-closed projection of historical Wiki receipts into Raw lineage."""
from __future__ import annotations

from datetime import UTC, datetime
import re
from pathlib import Path
from typing import Any

from knowledge_platform.distribution import wiki_archive

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$")
_RAW = re.compile(r"^[^/\\][^\\]*$")


def _fail(message: str) -> None:
    raise ValueError(message)


def _slug(value: Any) -> str:
    if not isinstance(value, str) or not _SLUG.fullmatch(value) or value in {"index", "log"}:
        _fail("Invalid Wiki page slug")
    return value


def _timestamp(value: Any) -> tuple[str, datetime]:
    if not isinstance(value, str) or not value.strip():
        _fail("Invalid published_at")
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid published_at") from exc
    if parsed.tzinfo is None:
        _fail("published_at must include timezone")
    parsed = parsed.astimezone(UTC)
    return parsed.isoformat().replace("+00:00", "Z"), parsed


def _raw_facts(raw_assets: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Read the exact facts produced by the verified Raw projection."""
    if not isinstance(raw_assets, dict):
        _fail("Raw assets must be an object")
    result = {}
    for asset_id, item in raw_assets.items():
        if not isinstance(asset_id, str) or not isinstance(item, dict) or not isinstance(item.get("metadata"), dict):
            _fail("Invalid Raw asset fact")
        path = item["metadata"].get("snapshot_path")
        revision = item.get("content_digest")
        if not isinstance(path, str) or not path or not _RAW.fullmatch(path) or any(part in {"", ".", ".."} for part in path.split("/")):
            _fail("Invalid Raw snapshot path")
        if not isinstance(revision, str) or not revision.startswith("sha256:") or not _DIGEST.fullmatch(revision[7:]):
            _fail("Invalid Raw digest")
        if path in result:
            _fail("Duplicate Raw facts")
        result[path] = (asset_id, revision[7:])
    return result


def _receipt_paths(evidence_root: Path, archive_manifest: dict[str, Any]) -> list[tuple[str, Path]]:
    files = archive_manifest.get("files") if isinstance(archive_manifest, dict) else None
    if not isinstance(files, dict):
        _fail("Archive manifest inventory is invalid")
    archive = evidence_root / "archive"
    found: list[tuple[str, Path]] = []
    for name in sorted(files):
        # The archive is authoritative: only direct children of this directory qualify.
        if not isinstance(name, str) or not name.startswith(".puddingclaw/jobs/wiki-") or name.count("/") != 2 or not name.endswith(".json"):
            continue
        path = archive / name
        stem = Path(name).stem
        found.append((stem, path))
    return found


def _active_pages(archive_manifest: dict) -> set[str]:
    return {name[5:-3] for name in archive_manifest["files"]
            if name.startswith("wiki/") and name.endswith(".md")
            and name not in {"wiki/index.md", "wiki/log.md"}}


def project_wiki_lineage(evidence_root: Path, archive_manifest: dict, raw_assets: dict) -> dict[str, dict[str, Any]]:
    evidence_root = Path(evidence_root)
    facts = _raw_facts(raw_assets)
    records: list[tuple[datetime, str, str, dict[str, Any], str]] = []
    for job_id, path in _receipt_paths(evidence_root, archive_manifest):
        data, fact = wiki_archive._read(path, private=True, limit=wiki_archive.MAX_JSON)
        relative = path.relative_to(evidence_root / "archive").as_posix()
        if (archive_manifest.get("files") or {}).get(relative) != fact:
            _fail("Wiki receipt inventory changed")
        try:
            record = wiki_archive._json(data)
        except Exception as exc:
            raise ValueError("Invalid Wiki receipt JSON") from exc
        if not isinstance(record, dict):
            _fail("Invalid Wiki receipt")
        if record.get("status") != "published":
            continue
        if record.get("job_id") != job_id or not re.fullmatch(r"wiki-[a-z0-9-]{1,160}", job_id):
            _fail("Wiki receipt job_id does not match filename")
        published_at, sort_at = _timestamp(record.get("published_at"))
        operation = record.get("operation", "publish")
        if not isinstance(operation, str) or operation not in {"publish", "page-retirement", "workspace-prefix-migration"}:
            _fail("Unknown published Wiki operation")
        raw_hashes = record.get("raw_hashes")
        consumed = record.get("consumed_raw_by_page", {})
        if not isinstance(raw_hashes, dict) or not isinstance(consumed, dict):
            _fail("Malformed published Wiki receipt")
        if operation != "page-retirement" and "consumed_raw_by_page" not in record:
            _fail("Malformed published Wiki receipt")
        records.append((sort_at, job_id, published_at, record, fact["sha256"]))

    records.sort(key=lambda item: (item[0], item[1]))
    state: dict[str, dict[str, Any]] = {
        path: {"pages": set(), "jobs": set(), "at": None, "receipts": set(), "retirements": []}
        for path, (_asset, digest) in facts.items()
    }
    retired: set[str] = set()
    page_sources: dict[str, set[str]] = {}
    relations = 0
    def consume_budget():
        nonlocal relations
        relations += 1
        if relations > 100000:
            _fail("Wiki lineage relation budget exceeded")
    for _sort_at, job_id, published_at, receipt, receipt_digest in records:
        operation = receipt.get("operation", "publish")
        raw_hashes = receipt.get("raw_hashes")
        consumed = receipt.get("consumed_raw_by_page", {})
        if not isinstance(raw_hashes, dict) or not isinstance(consumed, dict):
            _fail("Malformed published Wiki receipt")
        retired_map = receipt.get("retired_pages")
        if operation == "page-retirement":
            if not isinstance(retired_map, dict) or not retired_map or consumed:
                _fail("Malformed retired_pages")
            archive_dir = receipt.get("archive_dir")
            expected_dir = f".puddingclaw/retired/{job_id}/wiki"
            if archive_dir != expected_dir:
                _fail("Invalid retirement archive_dir")
            for slug, replacement in retired_map.items():
                slug = _slug(slug)
                replacement = _slug(replacement)
                if slug == replacement:
                    _fail("Retired page replacement must differ")
                if slug in retired:
                    _fail("Conflicting retained retired page")
                listed = f"{expected_dir}/{slug}.md"
                if listed not in (archive_manifest.get("files") or {}):
                    _fail("Missing retired page archive")
                retired.add(slug)
                for raw_path in sorted(page_sources.pop(slug, set())):
                    consume_budget()
                    entry = state[raw_path]
                    entry["pages"].discard(slug)
                    entry["receipts"].add(receipt_digest)
                    entry["retirements"].append({"slug": slug, "replacement": replacement, "job_id": job_id, "retired_at": published_at, "receipt_digest": receipt_digest})
        elif "retired_pages" in receipt:
            _fail("retired_pages on non-retirement receipt")

        moved = receipt.get("moved", {})
        if operation == "workspace-prefix-migration":
            if not isinstance(moved, dict):
                _fail("Malformed moved pages")
            for old, new in moved.items():
                old, new = _slug(old), _slug(new)
                if old == new or new not in consumed:
                    _fail("Moved page lacks replacement publication")
                retired.add(old)
                for raw_path in sorted(page_sources.pop(old, set())):
                    consume_budget()
                    state[raw_path]["pages"].discard(old)
                    state[raw_path]["receipts"].add(receipt_digest)

        for page, paths in consumed.items():
            page = _slug(page)
            if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths) or len(paths) != len(set(paths)):
                _fail("Malformed consumed_raw_by_page")
            retired.discard(page)  # a later publish is a valid replacement
            for raw_path in paths:
                if not isinstance(raw_path, str) or raw_path not in facts:
                    _fail("Consumed Raw path is not registered")
                digest = raw_hashes.get(raw_path)
                if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                    _fail("Invalid receipt Raw digest")
                if digest != facts[raw_path][1]:
                    _fail("Receipt Raw digest mismatch")
                consume_budget()
                page_sources.setdefault(page, set()).add(raw_path)
                entry = state[raw_path]
                entry["pages"].add(page)
                entry["jobs"].add(job_id)
                entry["at"] = published_at
                entry["receipts"].add(receipt_digest)

    active = _active_pages(archive_manifest)
    if retired & active:
        _fail("Retired page remains active without republish")
    return {
        asset_id: {
            "historical_consumed": bool(entry["jobs"]),
            "historical_compiled_pages": sorted(entry["pages"] & active),
            "historical_job_ids": sorted(entry["jobs"]),
            "historical_compiled_at": entry["at"],
            "historical_receipt_digests": sorted(entry["receipts"]),
            "historical_retirements": entry["retirements"],
        }
        for path, entry in state.items()
        for asset_id, _digest in [facts[path]]
    }
