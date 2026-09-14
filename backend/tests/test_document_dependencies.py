from pathlib import Path

import pytest

from knowledge_platform.distribution.document_dependencies import collect_document_dependencies


def test_collects_markdown_images_percent_parent_and_cycle(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "docs/index.md").write_text(
        "![image](../images/a%20one.png)\n[other](chapter.md#part)\n", encoding="utf-8"
    )
    (tmp_path / "docs/chapter.md").write_text("[back](index.md)\n", encoding="utf-8")
    (tmp_path / "images/a one.png").write_bytes(b"png")
    result = collect_document_dependencies(tmp_path, {"docs/index.md": "text/markdown"})
    assert sorted(result["files"]) == ["docs/chapter.md", "docs/index.md", "images/a one.png"]
    assert result["edges"] == [
        {"source": "docs/chapter.md", "target": "docs/index.md"},
        {"source": "docs/index.md", "target": "docs/chapter.md"},
        {"source": "docs/index.md", "target": "images/a one.png"},
    ]


def test_rejects_missing_and_escape_references(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("[missing](missing.md)", encoding="utf-8")
    with pytest.raises(ValueError):
        collect_document_dependencies(tmp_path, {"index.md": "text/markdown"})
    (tmp_path / "index.md").write_text("[escape](../../outside.md)", encoding="utf-8")
    with pytest.raises(ValueError):
        collect_document_dependencies(tmp_path, {"index.md": "text/markdown"})


def test_external_references_are_hashed_without_fetching(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text(
        "[web](https://example.invalid/a?q=1) ![pixel](data:image/png;base64,AA==) "
        "<a href='mailto:test@example.invalid'>mail</a>", encoding="utf-8"
    )
    result = collect_document_dependencies(tmp_path, {"index.md": "text/markdown"})
    assert result["edges"] == []
    assert len(result["external_references"]) == 3
    assert all(value.startswith("sha256:") for value in result["external_references"])


def test_rejects_symlinked_dependency(tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("![x](image.png)", encoding="utf-8")
    (tmp_path / "real.png").write_bytes(b"x")
    (tmp_path / "image.png").symlink_to(tmp_path / "real.png")
    with pytest.raises(ValueError):
        collect_document_dependencies(tmp_path, {"index.md": "text/markdown"})


def test_code_fences_are_not_rendered_html_dependencies(tmp_path):
    root=tmp_path.resolve();(root/'body.md').write_text('```html\n<img src="missing.png">\n```\n\n`<img src="also-missing.png">`\n')
    graph=collect_document_dependencies(root,{'body.md':'text/markdown'})
    assert set(graph['files'])=={'body.md'} and graph['edges']==[]


def test_percent_encoded_fragment_character_is_part_of_filename(tmp_path):
    root=tmp_path.resolve();(root/'body.md').write_text('![image](pic%23hash.png#fragment)\n');(root/'pic#hash.png').write_bytes(b'image')
    graph=collect_document_dependencies(root,{'body.md':'text/markdown'})
    assert 'pic#hash.png' in graph['files']


@pytest.mark.parametrize('html',['<base href="other/">','<img style="background:url(missing.png)">','<link rel="stylesheet" href="style.css">','<script src="main.js"></script>'])
def test_unsupported_html_dependency_semantics_reject(tmp_path,html):
    root=tmp_path.resolve();(root/'body.md').write_text(html)
    with pytest.raises(ValueError):collect_document_dependencies(root,{'body.md':'text/markdown'})


def test_invalid_utf8_url_escape_rejects(tmp_path):
    root=tmp_path.resolve();(root/'body.md').write_text('<img src="%FF.png">')
    with pytest.raises(ValueError):collect_document_dependencies(root,{'body.md':'text/markdown'})
