from __future__ import annotations

import os
from pathlib import Path

import pytest

from knowledge_platform.package.builder import _publish_noreplace


def test_directory_publication_is_atomic_and_does_not_replace_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "payload").write_text("first", encoding="utf-8")
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _publish_noreplace(source.name, "published", parent_descriptor, directory=True)
    finally:
        os.close(parent_descriptor)

    published = tmp_path / "published"
    assert not source.exists()
    assert (published / "payload").read_text(encoding="utf-8") == "first"

    competitor = tmp_path / "competitor"
    competitor.mkdir()
    (competitor / "payload").write_text("second", encoding="utf-8")
    parent_descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            _publish_noreplace(competitor.name, published.name, parent_descriptor, directory=True)
    finally:
        os.close(parent_descriptor)

    assert (published / "payload").read_text(encoding="utf-8") == "first"
    assert (competitor / "payload").read_text(encoding="utf-8") == "second"
