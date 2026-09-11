import hashlib
import json
import os
from pathlib import Path

import pytest

from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive, verify_archive


def fixture(tmp_path):
    root = tmp_path / "brain"; (root / "raw").mkdir(parents=True); (root / "wiki" / "index").mkdir(parents=True)
    (root / ".puddingclaw" / "retired").mkdir(parents=True)
    body = b"hello archive\n"; (root / "raw" / "page.md").write_bytes(body); (root / "wiki" / "index" / "empty").mkdir()
    row = {"snapshot_path": "page.md", "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body), "source_id": "s1", "asset_id": "a1"}
    (root / "raw" / "manifest.jsonl").write_text(json.dumps(row) + "\n")
    return root, tmp_path / "out"


def test_archive_preserves_tree_and_verifies_without_source(tmp_path):
    root, output = fixture(tmp_path); result = prepare_wiki_archive(root, output, installation_id="i1", source_revision="r1")
    assert result["activation_allowed"] is False and result["wiki_semantics_verified"] is False
    assert (output / "archive/.puddingclaw/retired").is_dir()
    assert verify_archive(output)["files"]["raw/page.md"]["size_bytes"] == 14


def test_manifest_duplicate_and_bad_digest_rejected(tmp_path):
    root, output = fixture(tmp_path)
    row = json.loads((root / "raw/manifest.jsonl").read_text()); (root / "raw/manifest.jsonl").write_text(json.dumps(row) + "\n" + json.dumps(row))
    with pytest.raises(ValueError): prepare_wiki_archive(root, output)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_links_rejected(tmp_path, kind):
    root, output = fixture(tmp_path); target = root / "raw/page.md"; bad = root / "raw/bad"
    if kind == "symlink": bad.symlink_to(target)
    else: os.link(target, bad)
    with pytest.raises(ValueError): prepare_wiki_archive(root, output)


def test_resume_and_unknown_payload_rejected(tmp_path):
    root, output = fixture(tmp_path)
    with pytest.raises(RuntimeError): prepare_wiki_archive(root, output, _after_publish=lambda _: (_ for _ in ()).throw(RuntimeError("stop")))
    (output / "archive/unknown").write_bytes(b"x")
    with pytest.raises(ValueError): prepare_wiki_archive(root, output)


def test_source_change_rejected_and_manifest_tamper_rejected(tmp_path):
    root, output = fixture(tmp_path); prepare_wiki_archive(root, output)
    (root / "raw/page.md").write_bytes(b"changed")
    with pytest.raises(ValueError): prepare_wiki_archive(root, output)
    (output / "archive/raw/page.md").write_bytes(b"forged")
    with pytest.raises(ValueError): verify_archive(output)

