"""Opt-in tests that observe a separately supplied legacy Knowledge checkout."""

import os

import pytest


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    source = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if source:
        return
    skip = pytest.mark.skip(
        reason="migration observation requires explicit PUDDINGKNOWLEDGE_LEGACY_SOURCE"
    )
    for item in items:
        item.add_marker(skip)
