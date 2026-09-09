"""Snapshot Workspace materializer for portable Knowledge Packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .builder import (
    PackageValidationError,
    _create_private_temp_directory,
    _ensure_safe_parent_chain,
    _open_safe_parent_directory,
    _publish_noreplace,
    _read_secure_relative_file,
    _write_secure_relative_file,
    validate_package,
)


@dataclass(frozen=True, slots=True)
class WorkspaceMaterializationResult:
    workspace_root: Path
    package_revision: str
    file_count: int


def _materialize_xls_table_bindings(temporary: Path, entry_names: tuple[str, ...]) -> None:
    """Create path-free CSV derivatives so XLS workspaces need no runtime plugin."""

    assets_index = temporary / "assets/index.json"
    document = json.loads(assets_index.read_text(encoding="utf-8"))
    assets = document.get("assets", []) if isinstance(document, dict) else []
    if not isinstance(assets, list):
        raise PackageValidationError("package assets index is invalid")
    bindings: dict[str, str] = {}
    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            raise PackageValidationError("package assets index is invalid")
        package_path = str(raw_asset.get("package_path") or "")
        if Path(package_path).suffix.casefold() != ".xls":
            continue
        if package_path not in entry_names:
            raise PackageValidationError("XLS asset is absent from the validated package")
        try:
            import pandas as pd
        except ImportError as error:
            raise PackageValidationError("XLS Workspace materialization requires the excel extra") from error
        try:
            frame = pd.read_excel(
                temporary / package_path,
                sheet_name=raw_asset.get("sheet_name") if raw_asset.get("sheet_name") else 0,
                nrows=10_001,
            )
            payload = frame.to_csv(index=False).encode("utf-8")
        except Exception as error:
            raise PackageValidationError("XLS asset cannot be materialized for Workspace") from error
        if len(payload) > 16 * 1024 * 1024:
            raise PackageValidationError("XLS Workspace derivative exceeds local query limit")
        asset_id = str(raw_asset.get("id") or "")
        relative = f"assets/workspace-table-derived/{asset_id}.csv"
        _write_secure_relative_file(temporary, relative, payload)
        bindings[asset_id] = relative
    if bindings:
        _write_secure_relative_file(
            temporary,
            "assets/workspace-table-bindings.json",
            (json.dumps(bindings, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
        )


def _refresh_workspace_checksums(root: Path) -> None:
    files = {}
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"checksums.json", "bindings.local.yaml"}:
            continue
        raw = path.read_bytes()
        files[relative] = {"bytes": len(raw), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    checksums_path = root / "checksums.json"
    if checksums_path.is_symlink() or not checksums_path.is_file():
        raise PackageValidationError("Workspace checksums file is unavailable")
    checksums_path.write_text(
        json.dumps(
            {"format": "agent-knowledge-package-checksums/v1", "files": dict(sorted(files.items()))},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


_KNOWLEDGE_CLI = r'''#!/usr/bin/env python3
import argparse
import csv
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
MAX_PREVIEW_ROWS = 20
SECRET_COLUMN = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
UNSAFE_VALUE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)\s*[:=]\s*\S+|(?:bearer\s+|sk-|ghp_)[A-Za-z0-9._-]{8,}")
MAX_XLSX_UNCOMPRESSED = 64 * 1024 * 1024
LOCAL_BINDING_FILE = "bindings.local.yaml"

def load(name):
    path = ROOT / name
    if path.is_symlink() or not path.is_file():
        raise SystemExit("snapshot file is unavailable")
    checksums_path = ROOT / "checksums.json"
    if checksums_path.is_file():
        try:
            checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
            expected = checksums.get("files", {}).get(name)
            raw = path.read_bytes()
        except (OSError, json.JSONDecodeError, AttributeError) as error:
            raise SystemExit("snapshot manifest is unavailable") from error
        if not isinstance(expected, dict) or expected.get("bytes") != len(raw) or expected.get("sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise SystemExit("snapshot file checksum mismatch")
        return json.loads(raw.decode("utf-8"))
    return json.loads(path.read_text(encoding="utf-8"))

def load_collections():
    """Load canonical Collections, with read-only support for legacy packages."""
    name = "collections/index.json" if (ROOT / "collections/index.json").is_file() else "datasets/index.json"
    return load(name)

def _trace_id(capability, dataset_id, query, limit):
    package_revision = _package_revision()
    material = f"{package_revision}:{capability}:{dataset_id}:{query}:{limit}".encode("utf-8")
    return "workspace_" + capability + "_" + hashlib.sha256(material).hexdigest()[:24]

def _package_revision():
    manifest = load("package-manifest.json")
    revision = manifest.get("package_revision") if isinstance(manifest, dict) else None
    if not isinstance(revision, str) or not revision.startswith("sha256:"):
        raise SystemExit("snapshot package revision is invalid")
    return revision

def validate_snapshot():
    checksums_path = ROOT / "checksums.json"
    try:
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        files = checksums["files"]
        package_manifest = json.loads((ROOT / "package-manifest.json").read_text(encoding="utf-8"))
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit("snapshot manifest is unavailable") from error
    if checksums.get("format") != "agent-knowledge-package-checksums/v1" or not isinstance(files, dict):
        raise SystemExit("snapshot checksum manifest is invalid")
    if not isinstance(package_manifest, dict) or package_manifest.get("format") != "agent-knowledge-package/v1":
        raise SystemExit("snapshot package manifest is invalid")
    expected_files = set(files)
    manifest_files = package_manifest.get("files")
    if not isinstance(manifest_files, dict) or not set(manifest_files).issubset(expected_files):
        raise SystemExit("snapshot package manifest files are invalid")
    for relative, expected in manifest_files.items():
        if files.get(relative) != expected:
            raise SystemExit("snapshot package manifest checksums are inconsistent")
    actual_files = {
        path.relative_to(ROOT).as_posix()
        for path in ROOT.rglob("*")
        if path.is_file() or path.is_symlink()
    } - {"checksums.json", LOCAL_BINDING_FILE}
    if actual_files != expected_files - {"checksums.json", LOCAL_BINDING_FILE}:
        raise SystemExit("snapshot contains unlisted or missing files")
    for relative, expected in files.items():
        if relative == "checksums.json":
            continue
        if not isinstance(expected, dict):
            raise SystemExit("snapshot checksum entry is invalid")
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            raise SystemExit("snapshot contains a symlink or unavailable file")
        raw = path.read_bytes()
        if expected.get("bytes") != len(raw) or expected.get("sha256") != "sha256:" + hashlib.sha256(raw).hexdigest():
            raise SystemExit("snapshot checksum mismatch")
    revision_material = {
        "format": package_manifest.get("format"),
        "id": package_manifest.get("id"),
        "version": package_manifest.get("version"),
        "catalog_revision": package_manifest.get("catalog_revision"),
        "capabilities": package_manifest.get("capabilities"),
        "files": manifest_files,
    }
    package_revision = "sha256:" + hashlib.sha256(
        (json.dumps(revision_material, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
    if package_manifest.get("package_revision") != package_revision:
        raise SystemExit("snapshot package revision is invalid")
    return {"status": "valid", "package_revision": package_revision, "file_count": len(expected_files)}

def _safe_asset_path(row):
    relative = str(row.get("package_path") or "")
    relative_path = Path(relative)
    if relative_path.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.parts):
        raise SystemExit("asset package path is not a safe relative path")
    path = ROOT / relative_path
    try:
        path.relative_to(ROOT / "assets")
    except ValueError as error:
        raise SystemExit("asset package path escapes snapshot") from error
    current = ROOT
    for component in relative_path.parts:
        current /= component
        if current.is_symlink():
            raise SystemExit("asset snapshot contains a symlink")
    if not path.is_file():
        raise SystemExit("asset snapshot file is unavailable")
    if path.stat().st_size > MAX_BYTES:
        raise SystemExit("asset snapshot file exceeds local query limit")
    return path

def _workspace_derived_path(row):
    bindings_path = ROOT / "assets/workspace-table-bindings.json"
    if not bindings_path.is_file():
        return None
    bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    relative = bindings.get(str(row.get("id"))) if isinstance(bindings, dict) else None
    if not isinstance(relative, str) or not relative:
        return None
    candidate = ROOT / relative
    try:
        candidate.relative_to(ROOT / "assets")
    except ValueError as error:
        raise SystemExit("workspace table binding escapes snapshot") from error
    if candidate.is_symlink() or not candidate.is_file() or candidate.stat().st_size > MAX_BYTES:
        raise SystemExit("workspace table binding is unavailable")
    return candidate

def _xlsx_cell_value(cell, shared_strings):
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(item.text or "" for item in cell.findall(".//{*}t"))
    value = cell.find("{*}v")
    raw = "" if value is None or value.text is None else value.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as error:
            raise SystemExit("snapshot XLSX shared string is invalid") from error
    if cell_type == "b":
        return raw == "1"
    if raw == "":
        return None
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number

def _xlsx_rows(path, sheet_name):
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as error:
        raise SystemExit("snapshot XLSX archive is invalid") from error
    with archive:
        infos = archive.infolist()
        if sum(max(0, item.file_size) for item in infos) > MAX_XLSX_UNCOMPRESSED:
            raise SystemExit("snapshot XLSX archive is too large")
        namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main", "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        try:
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ET.ParseError) as error:
            raise SystemExit("snapshot XLSX workbook is invalid") from error
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships
            if "Id" in item.attrib and "Target" in item.attrib
        }
        sheets = []
        for item in workbook.findall("main:sheets/main:sheet", namespace):
            name = item.attrib.get("name", "")
            relationship_id = item.attrib.get("{%s}id" % namespace["rel"], "")
            target = targets.get(relationship_id, "")
            if target.startswith("/"):
                target = target[1:]
            if not target.startswith("xl/"):
                target = "xl/" + target
            sheets.append((name, target))
        if not sheets:
            raise SystemExit("snapshot XLSX has no worksheets")
        selected = next((item for item in sheets if item[0] == sheet_name), sheets[0])
        if sheet_name and selected[0] != sheet_name:
            raise SystemExit("snapshot XLSX sheet is unavailable")
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            try:
                shared = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                shared_strings = ["".join(item.text or "" for item in entry.findall(".//{*}t")) for entry in shared.findall("{*}si")]
            except (KeyError, ET.ParseError) as error:
                raise SystemExit("snapshot XLSX shared strings are invalid") from error
        try:
            worksheet = ET.fromstring(archive.read(selected[1]))
        except (KeyError, ET.ParseError) as error:
            raise SystemExit("snapshot XLSX worksheet is invalid") from error
        parsed = []
        for row in worksheet.findall(".//main:row", namespace):
            cells = {}
            for cell in row.findall("main:c", namespace):
                reference = cell.attrib.get("r", "")
                column = "".join(char for char in reference if char.isalpha()).upper()
                if column:
                    cells[column] = _xlsx_cell_value(cell, shared_strings)
            parsed.append(cells)
            if len(parsed) > MAX_ROWS:
                raise SystemExit("snapshot table exceeds local row limit")
        if not parsed:
            return (), []
        def column_index(value):
            result = 0
            for char in value:
                result = result * 26 + ord(char) - 64
            return result
        columns = tuple(sorted({key for row in parsed for key in row}, key=column_index))
        headers = tuple(str(parsed[0].get(column) or "").strip() for column in columns)
        # headers is derived one-for-one from columns; keep the portable CLI compatible with host Python 3.9.
        rows = [{header: row.get(column) for header, column in zip(headers, columns) if header} for row in parsed[1:]]
        return headers, rows

def _table_asset(row, query):
    if str(row.get("kind") or "") not in {"table", "spreadsheet", "logical_dataset"}:
        raise SystemExit("asset is not declared as a snapshot table")
    path = _safe_asset_path(row)
    suffix = path.suffix.casefold()
    if suffix not in {".csv", ".tsv", ".xlsx", ".xls"}:
        raise SystemExit("snapshot table query supports CSV/TSV/XLSX/XLS assets only")
    raw = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if str(row.get("content_digest") or "") != digest:
        raise SystemExit("asset snapshot content digest does not match manifest")
    if suffix == ".xlsx":
        columns, rows = _xlsx_rows(path, row.get("sheet_name"))
    elif suffix == ".xls":
        path = _workspace_derived_path(row)
        if path is None:
            raise SystemExit("snapshot XLS query requires a materialized table binding")
        reader = csv.DictReader(path.read_text(encoding="utf-8-sig").splitlines())
        columns = tuple(str(item or "").strip() for item in (reader.fieldnames or ()))
        rows = [{column: source_row.get(column) for column in columns} for source_row in reader]
    else:
        reader = csv.DictReader(raw.decode("utf-8-sig").splitlines(), delimiter="\t" if suffix == ".tsv" else ",")
        columns = tuple(str(item or "").strip() for item in (reader.fieldnames or ()))
        rows = [{column: source_row.get(column) for column in columns} for source_row in reader]
    safe_columns = tuple(column for column in columns if column and not SECRET_COLUMN.search(column))
    if not safe_columns:
        raise SystemExit("snapshot table has no safe columns")
    safe_rows = []
    for source_row in rows:
        normalized = {column: source_row.get(column) for column in safe_columns}
        if any(UNSAFE_VALUE.search(str(value)) for value in normalized.values() if value is not None):
            raise SystemExit("snapshot table contains a secret-bearing value")
        safe_rows.append(normalized)
        if len(safe_rows) > MAX_ROWS:
            raise SystemExit("snapshot table exceeds local row limit")
    tokens = {item.casefold() for item in re.findall(r"[\w\u4e00-\u9fff]+", query) if item.strip()}
    haystack = " ".join((*safe_columns, *(str(value or "") for source_row in safe_rows[:100] for value in source_row.values()))).casefold()
    matched = sum(1 for token in tokens if token in haystack)
    if tokens and matched == 0:
        return None
    return {
        "asset_id": row.get("id"),
        "resource_uri": row.get("source_uri"),
        "asset_revision": row.get("revision") or digest,
        "answer": f"snapshot 表格包含 {len(safe_rows)} 行、{len(safe_columns)} 列；查询词命中 {matched}/{len(tokens)}。",
        "columns": list(safe_columns),
        "preview_rows": safe_rows[:MAX_PREVIEW_ROWS],
        "row_count": len(safe_rows),
        "score": matched / max(1, len(tokens)),
        "content_digest": digest,
        "evidence": [{
            "resource_uri": row.get("source_uri"),
            "locator": {"row_count": len(safe_rows)},
            "revision": row.get("revision") or digest,
            "quote": f"snapshot table rows={len(safe_rows)} columns={len(safe_columns)}",
        }],
    }

def table_query(query, asset_id=None, dataset_id=None, limit=5):
    assets = load("assets/index.json").get("assets", [])
    collections = load_collections().get("collections", [])
    if dataset_id:
        matches = [item for item in collections if item.get("id") == dataset_id]
        if not matches:
            raise SystemExit("dataset not found")
        if "table_query" not in {str(item) for item in (matches[0].get("capabilities") or [])}:
            raise SystemExit("dataset does not expose table_query")
        asset_ids = matches[0].get("asset_ids") or []
        candidates = [row for row in assets if row.get("id") in asset_ids]
    elif asset_id:
        candidates = [row for row in assets if row.get("id") == asset_id]
        if not candidates:
            raise SystemExit("asset not found")
    else:
        raise SystemExit("one of --asset-id or --dataset is required")
    results = []
    for row in candidates:
        result = _table_asset(row, query)
        if result is not None:
            results.append(result)
    results.sort(key=lambda item: (-(item.get("score") or 0), str(item.get("asset_id"))))
    return {
        "query": query,
        "dataset_id": dataset_id,
        "dataset_version": next((item.get("version") for item in collections if item.get("id") == dataset_id), None) if dataset_id else None,
        "package_revision": _package_revision(),
        "trace_id": _trace_id("table_query", dataset_id or asset_id or "asset", query, limit),
        "count": len(results[:limit]),
        "limit": limit,
        "tables": results[:limit],
    }

def _text_asset(row):
    path = _safe_asset_path(row)
    raw = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if str(row.get("content_digest") or "") != digest:
        raise SystemExit("asset snapshot content digest does not match manifest")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None, digest
    if UNSAFE_VALUE.search(text):
        raise SystemExit("asset snapshot contains secret-bearing content")
    return text, digest

def knowledge_query(question, dataset_id, limit=5):
    if not isinstance(question, str) or not question.strip():
        raise SystemExit("question must not be empty")
    collections = load_collections().get("collections", [])
    matches = [item for item in collections if item.get("id") == dataset_id]
    if not matches:
        raise SystemExit("dataset not found")
    collection = matches[0]
    if "knowledge_query" not in {str(item) for item in (collection.get("capabilities") or [])}:
        raise SystemExit("dataset does not expose knowledge_query")
    tokens = tuple(dict.fromkeys(item.casefold() for item in re.findall(r"[\w\u4e00-\u9fff]+", question) if item.strip()))
    if not tokens:
        raise SystemExit("question has no searchable terms")
    assets = load("assets/index.json").get("assets", [])
    asset_ids = set(collection.get("asset_ids") or [])
    results = []
    for row in assets:
        if row.get("id") not in asset_ids:
            continue
        text, digest = _text_asset(row)
        if text is None:
            continue
        text_haystack = text.casefold()
        metadata_haystack = (str(row.get("title") or "") + "\n" + str(row.get("description") or "")).casefold()
        matched = sum(1 for token in tokens if token in metadata_haystack or token in text_haystack)
        if not matched:
            continue
        position = min((text_haystack.find(token) for token in tokens if text_haystack.find(token) >= 0), default=0)
        start = max(0, position - 300)
        quote = text[start:start + 1200]
        results.append({
            "asset_id": row.get("id"),
            "resource_uri": row.get("source_uri"),
            "content_digest": digest,
            "score": matched / len(tokens),
            "matched_terms": matched,
            "quote": quote,
            "asset_revision": row.get("revision") or digest,
            "evidence": [{
                "resource_uri": row.get("source_uri"),
                "locator": {"offset": start},
                "revision": row.get("revision") or digest,
                "quote": quote,
            }],
        })
    results.sort(key=lambda item: (-(item.get("score") or 0), str(item.get("asset_id"))))
    bounded = results[:limit]
    return {
        "query": question,
        "dataset_id": dataset_id,
        "dataset_version": collection.get("version"),
        "package_revision": _package_revision(),
        "trace_id": _trace_id("knowledge_query", dataset_id, question, limit),
        "count": len(bounded),
        "limit": limit,
        "results": bounded,
    }

def database_nl2sql(question, dataset_id):
    """Select a matching packaged SQL example; never generate or execute SQL."""
    if not isinstance(question, str) or not question.strip():
        raise SystemExit("question must not be empty")
    collections = load_collections().get("collections", [])
    matches = [item for item in collections if item.get("id") == dataset_id]
    if not matches:
        raise SystemExit("dataset not found")
    collection = matches[0]
    if "database_nl2sql" not in {str(item) for item in (collection.get("capabilities") or [])}:
        raise SystemExit("dataset does not expose database_nl2sql")
    sources = load("database/index.json").get("sources", [])
    question_terms = tuple(dict.fromkeys(
        item.casefold() for item in re.findall(r"[\w\u4e00-\u9fff]+", question) if item.strip()
    ))
    if not question_terms:
        raise SystemExit("question has no searchable terms")
    candidates = []
    for source in sources:
        if source.get("dataset_id") != dataset_id:
            continue
        for example in source.get("sql_examples", []):
            example_question = str(example.get("question") or "")
            example_terms = set(item.casefold() for item in re.findall(r"[\w\u4e00-\u9fff]+", example_question))
            if not question_terms or not set(question_terms).issubset(example_terms):
                continue
            candidates.append((
                -len(example_terms),
                str(source.get("id") or ""),
                str(example.get("id") or ""),
                source,
                example,
            ))
    if not candidates:
        raise SystemExit("snapshot has no matching SQL example; connected Platform NL2SQL is required")
    _, source_id, example_id, source, example = sorted(candidates)[0]
    query_plan_id = "snapshot_sql_example_" + hashlib.sha256(
        f"{dataset_id}:{source_id}:{example_id}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "status": "candidate",
        "dataset_id": dataset_id,
        "dataset_version": collection.get("version"),
        "package_revision": _package_revision(),
        "trace_id": _trace_id("database_nl2sql", dataset_id, question, 1),
        "dialect": source.get("dialect"),
        "provenance": {
            "space_id": collection.get("space_id"),
            "dataset_id": dataset_id,
            "dataset_version": collection.get("version"),
            "capability": "database_nl2sql",
            "provider_versions": {"nl2sql": "snapshot-package-evidence"},
            "workspace_mode": "snapshot",
            "live_connection": False,
        },
        "query_plan": {
            "query_plan_id": query_plan_id,
            "sql": example.get("sql"),
            "source": "package_database_evidence",
            "execution_allowed": False,
        },
        "evidence": [{
            "source_id": source_id,
            "example_id": example_id,
            "question": example.get("question"),
        }],
        "warnings": ["snapshot_workspace_database_execution_disabled"],
    }

def main():
    parser = argparse.ArgumentParser(prog="knowledge")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    sub.add_parser("validate")
    search = sub.add_parser("search")
    search.add_argument("--query", required=True)
    query = sub.add_parser("query")
    query_selector = query.add_mutually_exclusive_group(required=True)
    query_selector.add_argument("--collection", dest="collection")
    query_selector.add_argument("--dataset", dest="collection", help="legacy alias for --collection")
    query.add_argument("--question", required=True)
    query.add_argument("--limit", type=int, default=5)
    read = sub.add_parser("read")
    read_selector = read.add_mutually_exclusive_group(required=True)
    read_selector.add_argument("--id")
    read_selector.add_argument("--uri")
    table = sub.add_parser("table")
    table_sub = table.add_subparsers(dest="table_command", required=True)
    table_query_parser = table_sub.add_parser("query")
    table_query_parser.add_argument("--query", required=True)
    table_selector = table_query_parser.add_mutually_exclusive_group(required=True)
    table_selector.add_argument("--asset-id")
    table_selector.add_argument("--collection", dest="dataset")
    table_selector.add_argument("--dataset", dest="dataset", help="legacy alias for --collection")
    table_query_parser.add_argument("--limit", type=int, default=5)
    database = sub.add_parser("database")
    database_sub = database.add_subparsers(dest="database_command", required=True)
    nl2sql = database_sub.add_parser("nl2sql")
    nl2sql.add_argument("--dataset", required=True, help="structured Database Dataset ID")
    nl2sql.add_argument("--question", required=True)
    execute = database_sub.add_parser("execute")
    execute.add_argument("--query-plan", required=True)
    args = parser.parse_args()
    assets = load("assets/index.json").get("assets", [])
    if args.command == "list":
        print(json.dumps(load_collections(), ensure_ascii=False, sort_keys=True))
    elif args.command == "validate":
        print(json.dumps(validate_snapshot(), ensure_ascii=False, sort_keys=True))
    elif args.command == "search":
        needle = args.query.casefold()
        rows = [row for row in assets if needle in (str(row.get("title", "")) + "\n" + str(row.get("description", ""))).casefold()]
        print(json.dumps({"assets": rows, "count": len(rows)}, ensure_ascii=False, sort_keys=True))
    elif args.command == "query":
        if not 1 <= args.limit <= 20:
            raise SystemExit("limit must be between 1 and 20")
        print(json.dumps(knowledge_query(args.question, args.collection, args.limit), ensure_ascii=False, sort_keys=True))
    elif args.command == "table":
        if not 1 <= args.limit <= 20:
            raise SystemExit("limit must be between 1 and 20")
        print(json.dumps(table_query(args.query, args.asset_id, args.dataset, args.limit), ensure_ascii=False, sort_keys=True))
    elif args.command == "database":
        if args.database_command == "nl2sql":
            print(json.dumps(database_nl2sql(args.question, args.dataset), ensure_ascii=False, sort_keys=True))
        else:
            raise SystemExit("snapshot database execution requires a connected Platform database contract")
    else:
        rows = [row for row in assets if row.get("id") == args.id or row.get("source_uri") == args.uri]
        if not rows:
            raise SystemExit("asset not found")
        print(json.dumps(rows[0], ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
'''


_WORKSPACE_SKILLS = {
    "knowledge-discovery": """# Knowledge discovery

Use `bin/knowledge list` to inspect Collections and `bin/knowledge search --query TEXT` to find Assets.
Keep the returned Collection/Space scope and `knowledge://` Resource URI when passing results to another Agent.
""",
    "document-rag": """# Document RAG

Use `bin/knowledge query --collection COLLECTION_ID --question TEXT` for bounded snapshot text retrieval.
The command returns portable Resource URIs, content digests, bounded quotes and offsets; do not infer host paths.
""",
    "wiki-query": """# Wiki query

Treat Wiki pages as Knowledge Platform Assets and use the public `knowledge://` URI from the Package manifest.
Snapshot Workspaces do not resolve external URLs or legacy Claw Session/Run/Goal fields.
""",
    "table-query": """# Table query

Use `bin/knowledge table query --asset-id ID --query TEXT` or `--collection COLLECTION_ID` for CSV/TSV/XLSX/XLS snapshot tables.
Results are bounded and secret-bearing columns/values are rejected.
""",
    "database-query": """# Database query

Database evidence in a Package is portable rebuild input for the Platform Collection.
Snapshot Workspace v1 does not open live database connections; use a separately authorized Platform `database_nl2sql` and `database_execute_readonly` contract when connected mode is available.
""",
}


_WORKSPACE_LOCAL_BINDING = """# Optional local overrides for a snapshot Workspace.
# This file is intentionally outside Package checksums and is never read by the snapshot CLI.
format: agent-knowledge-bindings-local/v1
mode: snapshot
credentials: not-configured
endpoint: null
"""


class WorkspaceMaterializer:
    """Materialize a self-contained snapshot workspace without host paths."""

    def materialize(self, *, package_root: Path, workspace_root: Path) -> WorkspaceMaterializationResult:
        validation = validate_package(package_root)
        destination = workspace_root.expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise PackageValidationError(f"workspace output already exists: {destination}")
        _ensure_safe_parent_chain(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent_descriptor = _open_safe_parent_directory(destination)
        temporary_name = _create_private_temp_directory(parent_descriptor, f".{destination.name}.tmp-")
        temporary = destination.parent / temporary_name
        try:
            for relative in validation.entry_names:
                _write_secure_relative_file(
                    temporary,
                    relative,
                    _read_secure_relative_file(validation.package_root, relative),
                )
            validate_package(temporary)
            _materialize_xls_table_bindings(temporary, validation.entry_names)
            _write_secure_relative_file(
                temporary,
                "AGENTS.md",
                b"# Knowledge Workspace\n\nUse `bin/knowledge validate` before querying, then `list`, `search --query`, `query --collection ... --question ...`, `read --id`, `table query --asset-id ...`, and `database nl2sql --dataset ... --question ...` for snapshot queries. Database execution is unavailable in snapshot mode.\n",
            )
            _write_secure_relative_file(
                temporary,
                "skills/knowledge-discovery/SKILL.md",
                _WORKSPACE_SKILLS["knowledge-discovery"].encode("utf-8"),
            )
            for skill_name, skill_text in _WORKSPACE_SKILLS.items():
                if skill_name == "knowledge-discovery":
                    continue
                _write_secure_relative_file(
                    temporary,
                    f"skills/{skill_name}/SKILL.md",
                    skill_text.encode("utf-8"),
                )
            _write_secure_relative_file(
                temporary,
                "bindings.local.yaml",
                _WORKSPACE_LOCAL_BINDING.encode("utf-8"),
            )
            _write_secure_relative_file(temporary, "bin/knowledge", _KNOWLEDGE_CLI.encode("utf-8"))
            os.chmod(temporary / "bin/knowledge", 0o755, follow_symlinks=False)
            _refresh_workspace_checksums(temporary)
            _ensure_safe_parent_chain(destination)
            _publish_noreplace(temporary.name, destination.name, parent_descriptor, directory=True)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            raise
        finally:
            os.close(parent_descriptor)
        return WorkspaceMaterializationResult(destination, validation.package_revision, sum(1 for item in destination.rglob("*") if item.is_file()))
