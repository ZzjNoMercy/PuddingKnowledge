"""Capture one sanitized Golden baseline record from an explicit observer."""

from __future__ import annotations

import argparse
import importlib
import json
import re
from collections.abc import Callable, Mapping
from pathlib import Path

from knowledge_platform.baseline import capture_baseline_record_document

_REFERENCE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_OBSERVATION_FIELDS = (
    "result",
    "evidence",
    "database_side_effects",
    "filesystem_side_effects",
    "provider_revision",
    "failure_semantics",
    "sanitized_fixture_manifest",
)


def _resolve_observer(reference: str) -> Callable[[], object]:
    if _REFERENCE_RE.fullmatch(reference) is None:
        raise ValueError("observer must use module:function syntax")
    module_name, function_name = reference.split(":", 1)
    observer = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(observer):
        raise ValueError(f"observer is not callable: {reference}")
    return observer


def _capture(reference: str, *, capability_id: str, source_revision: str) -> dict[str, object]:
    observation = _resolve_observer(reference)()
    if not isinstance(observation, Mapping):
        raise ValueError("observer must return a mapping")
    missing = [field for field in _OBSERVATION_FIELDS if field not in observation]
    if missing:
        raise ValueError(f"observer result is missing fields: {missing}")
    return capture_baseline_record_document(
        capability_id=capability_id,
        source_revision=source_revision,
        result=observation["result"],
        evidence=observation["evidence"],
        database_side_effects=observation["database_side_effects"],
        filesystem_side_effects=observation["filesystem_side_effects"],
        provider_revision=observation["provider_revision"],
        failure_semantics=observation["failure_semantics"],
        sanitized_fixture_manifest=observation["sanitized_fixture_manifest"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", required=True, help="explicit no-argument module:function observer")
    parser.add_argument("--capability-id", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, help="repository-relative record output; defaults to stdout")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    try:
        document = _capture(
            args.observer,
            capability_id=args.capability_id,
            source_revision=args.source_revision,
        )
        encoded = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is None:
            print(encoded, end="")
        else:
            root = args.repo_root.resolve()
            relative = args.output
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("--output must be repository-relative")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encoded, encoding="utf-8")
            print(json.dumps({"output": str(relative), "bytes": target.stat().st_size}, sort_keys=True))
    except (OSError, ImportError, TypeError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-golden-baseline-capture-report/v1", "error": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
