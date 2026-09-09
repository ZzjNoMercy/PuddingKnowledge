"""Run a local Phase 7 sidecar/legacy stop and rollback rehearsal.

Each capability is executed against its existing isolated local shadow.  The
canonical Catalog is copied before every invocation, and this rehearsal never
activates a production worker or writes a production Catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from knowledge_platform.continuity import (
    CONTINUITY_CAPABILITIES,
    ContinuityRequest,
    SqlitePlatformSidecar,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_WIKI_ASSET_ID = "asset_doc_09213484d2c84b3285c7c8c0_73c066b66ad7"
_CAPTURE_ID = "web_capture_later_d46aa7bd1c3f4c7c8842abda"
_CONNECTOR_ID = "connector_src_b8579e2221b45d9a35af5098"
_SOURCE_ITEM_ID = "source_item_sitem_675bab9905991ba3dbe4e946"
_WIKI_FILE = Path("/Users/pet/Documents/knowledge/imported/20260819/多智能体机制.md")
_CAPTURE_FILE = Path(
    "/Users/pet/Documents/knowledge/imported/20260804/Behind the scenes- How we build, test, and scale Google Agent Skills.md"
)
_STRUCTURED_FILE = Path("/Users/pet/Documents/knowledge/imported/20260710/2023年7月乘用车市场上险量.xlsx")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load_semantic_shadow():
    path = _ROOT / "backend/scripts/phase7_local_semantic_dimension_worker_shadow.py"
    spec = importlib.util.spec_from_file_location("phase7_semantic_dimension_shadow", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("semantic shadow module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _invoke_shadow(*, capability: str, catalog: Path, output_dir: Path, reports: dict[str, dict[str, str]], args: argparse.Namespace) -> str:
    output_dir.mkdir(parents=True, exist_ok=True)
    staged_catalog = output_dir / "knowledge-platform.sqlite3"
    shutil.copy2(catalog, staged_catalog)
    if capability == "semantic_dimension":
        outcome = _load_semantic_shadow().run_shadow(output_dir=output_dir, canonical_catalog=staged_catalog)
        status = str(outcome["status"])
        report_path = Path(str(outcome["report"]))
    else:
        script_names = {
            "wiki_compile": "phase7_local_wiki_compile_shadow.py",
            "capture_processing": "phase7_local_capture_processing_shadow.py",
            "connector_sync": "phase7_local_connector_sync_shadow.py",
            "gbrain_projection": "phase7_local_gbrain_projection_shadow.py",
            "logical_dataset_processing": "phase7_local_logical_dataset_worker_shadow.py",
        }
        command = [sys.executable, str(_ROOT / "backend/scripts" / script_names[capability]), "--output-dir", str(output_dir)]
        if capability == "wiki_compile":
            command.extend(["--asset-id", _WIKI_ASSET_ID, "--file", str(args.wiki_file)])
        elif capability == "capture_processing":
            command.extend(["--capture-id", _CAPTURE_ID, "--file", str(args.capture_file)])
        elif capability == "connector_sync":
            command.extend(
                ["--connector-id", _CONNECTOR_ID, "--source-item-id", _SOURCE_ITEM_ID, "--file", str(args.capture_file)]
            )
        elif capability == "gbrain_projection":
            command.extend(
                [
                    "--catalog",
                    str(staged_catalog),
                    "--space-id",
                    _SPACE_ID,
                    "--asset-id",
                    _WIKI_ASSET_ID,
                    "--file",
                    str(args.wiki_file),
                ]
            )
        elif capability == "logical_dataset_processing":
            command.extend(["--catalog", str(staged_catalog), "--file", str(args.structured_file)])
        environment = {**dict(__import__("os").environ), "PYTHONPATH": str(_ROOT / "backend")}
        completed = subprocess.run(command, cwd=_ROOT, env=environment, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError(f"{capability} shadow did not complete")
        try:
            outcome = json.loads(completed.stdout.strip().splitlines()[-1])
            status = str(outcome["status"])
            report_path = Path(str(outcome["report"]))
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{capability} shadow output is invalid") from exc
    if not status.endswith("NOT_ACTIVATABLE") or not report_path.is_file():
        raise RuntimeError(f"{capability} shadow is not a passing isolated rehearsal")
    reports[capability] = {"status": status, "report_digest": _digest(report_path)}
    return f"knowledge://spaces/{_SPACE_ID}/continuity/{capability}/shadow"


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_file: Path = _WIKI_FILE,
    capture_file: Path = _CAPTURE_FILE,
    structured_file: Path = _STRUCTURED_FILE,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    catalog = catalog.expanduser().absolute()
    args = argparse.Namespace(
        wiki_file=wiki_file.expanduser().absolute(),
        capture_file=capture_file.expanduser().absolute(),
        structured_file=structured_file.expanduser().absolute(),
    )
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-continuity-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_CONTINUITY_SHADOW_REJECTED",
        "canonical_catalog_digest_before": None,
        "canonical_catalog_digest_after": None,
        "legacy_stop": None,
        "capabilities": {},
        "replay_matches": None,
        "rollback": None,
        "audit_event_types": [],
    }
    continuity_state_dir = tempfile.TemporaryDirectory(prefix="phase7-continuity-state-")
    try:
        before = _digest(catalog)
        reports: dict[str, dict[str, str]] = {}
        def handler(request: ContinuityRequest) -> str:
            if request.resource_key == "forced-failure":
                raise RuntimeError("controlled continuity failure")
            with tempfile.TemporaryDirectory(prefix=f"phase7-{request.capability}-") as temp_dir:
                return _invoke_shadow(
                    capability=request.capability,
                    catalog=catalog,
                    output_dir=Path(temp_dir),
                    reports=reports,
                    args=args,
                )

        continuity_db = Path(continuity_state_dir.name) / "continuity.sqlite3"
        sidecar = SqlitePlatformSidecar(
            database_path=continuity_db,
            handlers={capability: handler for capability in CONTINUITY_CAPABILITIES},
        )
        sidecar.stop_legacy()
        result["legacy_stop"] = {"stopped": not sidecar.legacy_enabled}
        revision = "platform-local-phase7-v1"
        sidecar.activate(deployment_revision=revision)
        first = []
        second = []
        for capability in CONTINUITY_CAPABILITIES:
            request = ContinuityRequest(
                capability=capability,
                space_id=_SPACE_ID,
                resource_key=f"phase7-{capability}",
                idempotency_key=f"phase7-continuity-{capability}",
                deployment_revision=revision,
            )
            first.append(sidecar.process(request))
        def replay_guard(_request: ContinuityRequest) -> str:
            raise AssertionError("completed continuity run was executed after sidecar restart")

        restarted = SqlitePlatformSidecar(
            database_path=continuity_db,
            handlers={capability: replay_guard for capability in CONTINUITY_CAPABILITIES},
        )
        for capability in CONTINUITY_CAPABILITIES:
            second.append(
                restarted.process(
                    ContinuityRequest(
                        capability=capability,
                        space_id=_SPACE_ID,
                        resource_key=f"phase7-{capability}",
                        idempotency_key=f"phase7-continuity-{capability}",
                        deployment_revision=revision,
                    )
                )
            )
        result["capabilities"] = reports
        result["replay_matches"] = all(
            left.resource_uri == right.resource_uri and not left.replayed and right.replayed
            for left, right in zip(first, second, strict=True)
        )
        try:
            sidecar.process(
                ContinuityRequest(
                    capability="connector_sync",
                    space_id=_SPACE_ID,
                    resource_key="forced-failure",
                    idempotency_key="phase7-continuity-failure",
                    deployment_revision=revision,
                )
            )
        except RuntimeError:
            sidecar.rollback(reason="controlled local continuity failure")
        result["rollback"] = {
            "legacy_restored": sidecar.legacy_enabled,
            "sidecar_inactive": not sidecar.active,
            "event_types": [event.event_type for event in sidecar.audit_events()],
        }
        result["audit_event_types"] = Counter(event.event_type for event in sidecar.audit_events())
        result["canonical_catalog_digest_before"] = before
        result["canonical_catalog_digest_after"] = _digest(catalog)
        if not result["replay_matches"] or result["canonical_catalog_digest_before"] != result["canonical_catalog_digest_after"]:
            raise RuntimeError("continuity rehearsal changed the canonical Catalog or replayed inconsistently")
        if not result["rollback"]["legacy_restored"] or not result["rollback"]["sidecar_inactive"]:
            raise RuntimeError("continuity rollback did not restore legacy")
        result["status"] = "PHASE7_CONTINUITY_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        result["status"] = "PHASE7_CONTINUITY_SHADOW_FAILED"
        result["error"] = "local continuity rehearsal failed"
    continuity_state_dir.cleanup()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "phase7-local-continuity-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-file", type=Path, default=_WIKI_FILE)
    parser.add_argument("--capture-file", type=Path, default=_CAPTURE_FILE)
    parser.add_argument("--structured-file", type=Path, default=_STRUCTURED_FILE)
    args = parser.parse_args()
    outcome = run_shadow(
        output_dir=args.output_dir,
        catalog=args.catalog,
        wiki_file=args.wiki_file,
        capture_file=args.capture_file,
        structured_file=args.structured_file,
    )
    print(json.dumps({"status": outcome["status"], "report": outcome["report"]}, ensure_ascii=False))
    return 0 if str(outcome["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
