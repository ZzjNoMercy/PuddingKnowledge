"""Legacy runtime replay observations requiring the original source modules."""

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
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for legacy runtime replay")
    return Path(value).expanduser().resolve()


def test_checked_in_legacy_registry_replays_all_observers() -> None:
    root = _legacy_root()
    result = subprocess.run(
        [str(root / "backend/.venv/bin/python"), "backend/scripts/phase0a_legacy_runtime_probe_registry.py", "--replay"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["replay"]["status"] == "matched"
    assert report["replay"]["mismatched_probe_ids"] == []
    assert report["replay"]["skipped_probe_ids"] == []
    assert report["replay"]["replayed_probe_ids"] == report["executed_probe_ids"]
    assert set(report["replay"]["run_counts"].values()) == {2}


def test_connector_observer_restores_knowledge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.syspath_prepend(str(_legacy_root() / "backend"))
    from scripts import phase0a_connectors_capture_observer as observer

    original = str(tmp_path / "caller-knowledge")
    monkeypatch.setenv("PUDDINGCLAW_KNOWLEDGE_DIR", original)
    observer.observe()
    assert observer.os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] == original
