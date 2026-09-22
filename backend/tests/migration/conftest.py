"""Opt-in tests that observe a separately supplied legacy Knowledge checkout."""

import os
from pathlib import Path

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    source = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if source:
        return
    skip = pytest.mark.skip(
        reason="migration observation requires explicit PUDDINGKNOWLEDGE_LEGACY_SOURCE"
    )
    migration_root = Path(__file__).resolve().parent
    for item in items:
        item_path = Path(str(item.path)).resolve()
        if item_path.is_relative_to(migration_root):
            item.add_marker(skip)
