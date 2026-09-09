"""Replay REST and MCP database queries through the local Vanna Collection.

The underlying server is the existing independent local Platform process. A
Collection candidate is injected explicitly, and the report is promoted to a
Phase 9 result only when the child confirms that binding and both REST/MCP
two-phase calls succeed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from scripts.phase8_local_database_platform_process_shadow import run_shadow as run_database_process_shadow

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase9-local-database-vanna-replay"
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DEFAULT_COLLECTIONS = _ROOT / "artifacts/phase9-local-database-vanna-replay/vanna/collections"
_QUESTION = "统计本地车型能源类型数量"


def _current_collection(root: Path) -> Path:
    candidates = sorted(path for path in root.glob("*") if path.is_dir() and not path.is_symlink())
    if len(candidates) != 1:
        raise ValueError("local Vanna Collection candidate is not unique")
    return candidates[0]


def run_shadow(
    *,
    collection_root: Path = _DEFAULT_COLLECTIONS,
    catalog: Path = _DEFAULT_CATALOG,
    question: str = _QUESTION,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    db_host: str = "127.0.0.1",
    db_port: int = 5432,
    db_name: str = "insight_data",
    db_user: str = "pet",
    db_password_env: str = "PUDDINGCLAW_CANONICAL_DB_PASSWORD",
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().absolute()
    result = run_database_process_shadow(
        question=question,
        catalog=catalog,
        output_dir=output_dir,
        db_host=db_host,
        db_port=db_port,
        db_name=db_name,
        db_user=db_user,
        db_password_env=db_password_env,
        vanna_collection=_current_collection(collection_root.expanduser().absolute()),
    )
    result["format"] = "agent-knowledge-platform-phase9-local-vanna-http-mcp-shadow/v1"
    server = result.get("server") if isinstance(result.get("server"), dict) else {}
    prior_status = result.get("status")
    if (
        prior_status == "PHASE8_LOCAL_DATABASE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
        and server.get("database_vanna_collection_bound") is True
    ):
        result["status"] = "PHASE9_LOCAL_VANNA_HTTP_MCP_SHADOW_PASS_NOT_ACTIVATABLE"
    else:
        result["status"] = "PHASE9_LOCAL_VANNA_HTTP_MCP_SHADOW_FAILED"
    result["provider"] = {
        "kind": "local-file-backed-collection",
        "bound": server.get("database_vanna_collection_bound") is True,
        "activation_allowed": False,
    }
    report_path = output_dir / "phase9-local-vanna-http-mcp-shadow.json"
    result.pop("report", None)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection-root", type=Path, default=_DEFAULT_COLLECTIONS)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--question", default=_QUESTION)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="insight_data")
    parser.add_argument("--db-user", default="pet")
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    args = parser.parse_args()
    result = run_shadow(
        collection_root=args.collection_root,
        catalog=args.catalog,
        question=args.question,
        output_dir=args.output_dir,
        db_host=args.db_host,
        db_port=args.db_port,
        db_name=args.db_name,
        db_user=args.db_user,
        db_password_env=args.db_password_env,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
