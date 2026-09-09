from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.phase9_local_wiki_package_shadow import run_shadow


def test_local_wiki_package_shadow_builds_real_pages_and_workspace(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    (wiki_root / "papers").mkdir(parents=True)
    (wiki_root / "index.md").write_text("index", encoding="utf-8")
    (wiki_root / "log.md").write_text("log", encoding="utf-8")
    (wiki_root / "papers/alpha.md").write_text("---\ntitle: Alpha\n---\n\nAlpha body", encoding="utf-8")
    (wiki_root / "papers/beta.md").write_text("Beta body", encoding="utf-8")
    package_dir = tmp_path / "package"
    package_zip = tmp_path / "package.zip"
    workspace_dir = tmp_path / "workspace"
    result = run_shadow(wiki_root=wiki_root, package_dir=package_dir, package_zip=package_zip, workspace_dir=workspace_dir)

    assert result["status"] == "PHASE9_LOCAL_WIKI_PACKAGE_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["package"]["asset_count"] == 2
    assert result["zip"] == {
        "asset_count": 2,
        "created": True,
        "file_count": 15,
        "imported": True,
        "package_revision": result["package"]["package_revision"],
    }
    assert package_zip.is_file()
    assert result["workspace"]["created"] is True
    assert json.loads(
        subprocess.run(
            [str(workspace_dir / "bin/knowledge"), "validate"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )["status"] == "valid"
    queried = subprocess.run(
        [str(workspace_dir / "bin/knowledge"), "query", "--dataset", "collection_local_wiki", "--question", "Alpha"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Alpha body" in json.loads(queried.stdout)["results"][0]["quote"]
    report = json.loads(Path(result["report"]).read_text(encoding="utf-8"))
    assert "wiki_root" not in json.dumps(report, ensure_ascii=False)
    assert report["catalog_unchanged"] is True


def test_local_wiki_package_shadow_rejects_symlink_page(tmp_path: Path) -> None:
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    source = tmp_path / "source.md"
    source.write_text("body", encoding="utf-8")
    (wiki_root / "linked.md").symlink_to(source)
    result = run_shadow(wiki_root=wiki_root, package_dir=tmp_path / "package")

    assert result["status"] == "PHASE9_LOCAL_WIKI_PACKAGE_SHADOW_FAILED"
    assert result["package"]["created"] is False
