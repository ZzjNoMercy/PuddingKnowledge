from __future__ import annotations

import os
from pathlib import Path

from scripts.phase6_local_wiki_query_shadow import _materialize_catalog


def test_materialize_catalog_normalizes_relative_wiki_root(monkeypatch, tmp_path: Path) -> None:
    legacy_source = Path(os.environ["PUDDINGKNOWLEDGE_LEGACY_SOURCE"]).expanduser().resolve()
    monkeypatch.chdir(legacy_source)
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "page.md").write_text("# Local page\n\nagent boundary\n", encoding="utf-8")

    relative_wiki_root = Path(os.path.relpath(wiki_root, legacy_source))
    materialized = _materialize_catalog(
        legacy_source / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3",
        tmp_path / "catalog.sqlite3",
        relative_wiki_root,
    )

    assert materialized["pages"] == 1
    assert all(path.is_absolute() for path in materialized["file_bindings"].values())
