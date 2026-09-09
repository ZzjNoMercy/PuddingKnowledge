from __future__ import annotations

import concurrent.futures
import os
import shutil
from pathlib import Path

import pytest

from knowledge_platform.local.objects import LocalObjectStore


def test_parent_symlink_is_rejected_before_object_io(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    link_parent = tmp_path / "linked"
    link_parent.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        LocalObjectStore(link_parent / "objects")


def test_corrupt_digest_collision_is_refused(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects")
    digest = store.put(b"original")
    object_path = store.root / digest[7:]
    object_path.write_bytes(b"different")
    with pytest.raises(ValueError, match="integrity"):
        store.put(b"original")
    with pytest.raises(ValueError, match="integrity"):
        store.read(digest)
    store.close()


def test_concurrent_same_bytes_are_one_valid_object(tmp_path: Path) -> None:
    root = tmp_path / "objects"

    def put(_: int) -> str:
        with LocalObjectStore(root) as store:
            return store.put(b"same bytes")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(put, range(24)))
    assert len(set(results)) == 1
    with LocalObjectStore(root) as store:
        assert store.read(results[0]) == b"same bytes"


def test_identity_persists_across_full_store_copy_but_empty_store_differs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    with LocalObjectStore(source) as store:
        identity = store.identity
        digest = store.put(b"persisted")
    copied = tmp_path / "copied"
    shutil.copytree(source, copied)
    with LocalObjectStore(copied) as store:
        assert store.identity == identity
        assert store.read(digest) == b"persisted"
    with LocalObjectStore(tmp_path / "empty") as store:
        assert store.identity != identity


def test_identity_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    os.mkfifo(root / "identity.json")
    with pytest.raises(ValueError, match="regular"):
        LocalObjectStore(root)
