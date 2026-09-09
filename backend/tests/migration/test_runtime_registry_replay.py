"""Runtime probe replay against the original mixed checkout."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.migration


def _legacy_root() -> Path:
    value = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if not value:
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for runtime replay")
    return Path(value).expanduser().resolve()


def test_checked_in_runtime_registry_replays_all_captured_outputs() -> None:
    root = _legacy_root()
    result = subprocess.run(
        [str(root / "backend/.venv/bin/python"), "backend/scripts/phase0a_runtime_probe_registry.py", "--replay"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []
    assert report["replay"]["replayed_probe_ids"] == report["executed_probe_ids"]


def test_require_complete_always_replays_before_applying_strict_gate() -> None:
    root = _legacy_root()
    result = subprocess.run(
        [str(root / "backend/.venv/bin/python"), "backend/scripts/phase0a_runtime_probe_registry.py", "--require-complete"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 1
    assert report["error"] == "runtime probe registry is not complete"
    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []
