"""Validate the current local Phase 8 sidecar capability evidence matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_ARTIFACT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_OUTPUT = _DEFAULT_ARTIFACT_DIR / "phase8-local-capability-matrix-shadow-report.json"

_MATRIX = (
    ("document_wiki_query", "phase8-local-platform-process-shadow-report.json"),
    ("document_lexical_query", "phase8-local-lexical-platform-process-shadow-report.json"),
    ("document_dense_query", "phase8-local-dense-platform-process-shadow-report.json"),
    ("table_query", "phase8-local-table-platform-process-shadow-report.json"),
    ("database_query_execute", "phase8-local-database-platform-process-shadow-report.json"),
    ("logical_processing_authoring", "phase8-local-processing-authoring-platform-process-shadow-report.json"),
    ("wiki_compile", "phase8-local-wiki-compile-platform-process-shadow-report.json"),
    ("semantic_dimension_processing", "phase8-local-semantic-dimension-platform-process-shadow-report.json"),
    ("capture_processing", "phase8-local-capture-platform-process-shadow-report.json"),
    ("connector_sync", "phase8-local-connector-sync-platform-process-shadow-report.json"),
    ("gbrain_projection", "phase8-local-gbrain-platform-process-shadow-report.json"),
)


def _safe_report_path(root: Path, name: str) -> Path:
    path = root.expanduser().absolute() / name
    current = path
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("capability matrix report input must not contain symlink components")
        current = current.parent
    if not path.is_file():
        raise FileNotFoundError(path)
    return path


def _safe_output_path(path: Path) -> Path:
    absolute = path.expanduser().absolute()
    current = absolute
    while current != current.parent:
        if current.is_symlink():
            raise ValueError("capability matrix output must not contain symlink components")
        current = current.parent
    absolute.parent.mkdir(parents=True, exist_ok=True)
    return absolute


def _entry(capability: str, report_name: str, report: dict[str, Any]) -> dict[str, Any]:
    status = report.get("status")
    transport = report.get("transport") if isinstance(report.get("transport"), dict) else {}
    independent_process = transport.get("independent_process") is True or (
        type(transport.get("independent_processes")) is int and transport["independent_processes"] >= 2
    )
    clean_shutdown = report.get("server_shutdown_clean") is True or (
        report.get("first_server_shutdown_clean") is True and report.get("second_server_shutdown_clean") is True
    )
    entry = {
        "capability": capability,
        "report_file": report_name,
        "status": status,
        "pass_not_activatable": isinstance(status, str) and status.endswith("PASS_NOT_ACTIVATABLE"),
        "independent_process": independent_process,
        "clean_shutdown": clean_shutdown,
        "canonical_catalog_unchanged": report.get("canonical_catalog_unchanged") is True,
    }
    if capability == "document_dense_query":
        entry.update(
            {
                "candidate_collection_name": report.get("candidate_collection_name"),
                "catalog_binding_written": report.get("catalog_binding_written"),
                "network_contacted": report.get("network_contacted"),
                "external_network_contacted": report.get("external_network_contacted"),
                "activation_allowed": report.get("activation_allowed"),
                "dense_boundary_safe": (
                    report.get("catalog_binding_written") is False
                    and report.get("network_contacted") is False
                    and report.get("external_network_contacted") is False
                    and report.get("activation_allowed") is False
                ),
            }
        )
    return entry


def run_shadow(
    *,
    artifact_dir: Path = _DEFAULT_ARTIFACT_DIR,
    output: Path = _DEFAULT_OUTPUT,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-capability-matrix-shadow/v1",
        "status": "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_BLOCKED",
        "activation_allowed": False,
        "network_contacted": False,
        "external_network_contacted": False,
        "source_paths_emitted": False,
    }
    try:
        entries: list[dict[str, Any]] = []
        for capability, report_name in _MATRIX:
            report_path = _safe_report_path(artifact_dir, report_name)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("capability matrix report is invalid")
            entries.append(_entry(capability, report_name, payload))
        result["entries"] = entries
        result["capability_count"] = len(entries)
        result["pass_count"] = sum(bool(entry["pass_not_activatable"]) for entry in entries)
        result["independent_process_count"] = sum(bool(entry["independent_process"]) for entry in entries)
        result["clean_shutdown_count"] = sum(bool(entry["clean_shutdown"]) for entry in entries)
        result["canonical_unchanged_count"] = sum(bool(entry["canonical_catalog_unchanged"]) for entry in entries)
        dense = next(entry for entry in entries if entry["capability"] == "document_dense_query")
        if (
            result["pass_count"] == len(entries)
            and result["independent_process_count"] == len(entries)
            and result["clean_shutdown_count"] == len(entries)
            and result["canonical_unchanged_count"] == len(entries)
            and dense.get("dense_boundary_safe") is True
        ):
            result["status"] = "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_PASS_NOT_ACTIVATABLE"
    except Exception as error:
        result["error_type"] = type(error).__name__
    output = _safe_output_path(output)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, default=_DEFAULT_ARTIFACT_DIR)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_shadow(artifact_dir=args.artifact_dir, output=args.output)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
