import sqlite3
from pathlib import Path

import pytest

from knowledge_platform.distribution.sqlite_reverse_delta import build_rollback_candidate


def _make(path, rows, *, extra=False, fk=False):
    with sqlite3.connect(path) as db:
        if fk:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("CREATE TABLE parent(id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE child(id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id), value TEXT)")
            db.executemany("INSERT INTO parent VALUES (?,?)", rows[0])
            db.executemany("INSERT INTO child VALUES (?,?,?)", rows[1])
        else:
            db.execute("CREATE TABLE owned(id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE other(id INTEGER PRIMARY KEY, value TEXT)")
            db.executemany("INSERT INTO owned VALUES (?,?)", rows[0])
            db.executemany("INSERT INTO other VALUES (?,?)", rows[1])
        if extra:
            db.execute("CREATE TABLE extra(id INTEGER PRIMARY KEY, value TEXT)")


def test_builds_insert_update_delete_candidate_without_input_writes(tmp_path):
    source, before, after, output = [tmp_path / name for name in ("source.db", "before.db", "after.db", "candidate.db")]
    _make(source, [[(1, "a"), (2, "b")], [(1, "keep")]])
    _make(before, [[(1, "a"), (2, "b")], [(1, "keep")]])
    _make(after, [[(1, "updated"), (3, "new")], [(1, "keep")]])
    before_bytes = before.read_bytes(); after_bytes = after.read_bytes()
    result = build_rollback_candidate(source, before, after, output, ("owned",))
    assert result["activation_allowed"] is False
    assert result["tables"]["owned"] == {"insert": 1, "update": 1, "delete": 1}
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT * FROM owned ORDER BY id").fetchall() == [(1, "updated"), (3, "new")]
        assert db.execute("SELECT * FROM other").fetchall() == [(1, "keep")]
    assert before.read_bytes() == before_bytes and after.read_bytes() == after_bytes
    assert oct(output.stat().st_mode & 0o777) == "0o600"


def test_source_fence_unmapped_changes_and_existing_output_are_rejected(tmp_path):
    source, before, after, output = [tmp_path / name for name in ("source.db", "before.db", "after.db", "candidate.db")]
    _make(source, [[(1, "a")], [(1, "keep")]])
    _make(before, [[(1, "changed")], [(1, "keep")]])
    _make(after, [[(1, "changed")], [(1, "keep")]])
    with pytest.raises(ValueError, match="snapshot|baseline"):
        build_rollback_candidate(source, before, after, output, ("owned",))
    before.unlink()
    after.unlink()
    _make(before, [[(1, "a")], [(1, "keep")]])
    _make(after, [[(1, "a")], [(1, "changed")]])
    with pytest.raises(ValueError, match="Unmapped"):
        build_rollback_candidate(source, before, after, output, ("owned",))
    output.write_bytes(b"owned")
    with pytest.raises(FileExistsError):
        build_rollback_candidate(source, before, after, output, ("owned",))


def test_fencing_schema_allowlist_fk_and_reproducible_digest(tmp_path):
    source, before, after, one, two = [tmp_path / name for name in ("source.db", "before.db", "after.db", "one.db", "two.db")]
    _make(source, [[(1, "a")], [(1, "keep")]], fk=False)
    _make(before, [[(1, "a")], [(1, "keep")]], fk=False)
    _make(after, [[(1, "b")], [(1, "keep")]], fk=False)
    first = build_rollback_candidate(source, before, after, one, ("owned",))
    second = build_rollback_candidate(source, before, after, two, ("owned",))
    assert first["output_digest"] == second["output_digest"]
    with pytest.raises(ValueError):
        build_rollback_candidate(source, before, after, tmp_path / "bad.db", ("other",))


def test_source_may_retain_unowned_harness_table(tmp_path):
    source, before, after, output = [tmp_path / name for name in ("source.db", "before.db", "after.db", "candidate.db")]
    _make(source, [[(1, "a")], [(1, "keep")]], extra=True)
    _make(before, [[(1, "a")], [(1, "keep")]], extra=True)
    _make(after, [[(1, "b")], [(1, "keep")]], extra=True)
    for path in (source, before, after):
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO extra VALUES (1, 'harness')")
    result = build_rollback_candidate(source, before, after, output, ("owned",))
    assert result["activation_allowed"] is False
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT * FROM extra").fetchall() == [(1, "harness")]


def test_index_schema_change_is_rejected(tmp_path):
    source, before, after = [tmp_path / name for name in ("source.db", "before.db", "after.db")]
    _make(source, [[(1, "a")], [(1, "keep")]])
    _make(before, [[(1, "a")], [(1, "keep")]])
    _make(after, [[(1, "a")], [(1, "keep")]])
    with sqlite3.connect(after) as db:
        db.execute("CREATE INDEX changed_index ON other(value)")
    with pytest.raises(ValueError):
        build_rollback_candidate(source, before, after, tmp_path / "candidate.db", ("owned",))


def test_blob_rows_have_stable_logical_digest(tmp_path):
    paths = [tmp_path / name for name in ("source.db", "before.db", "after.db", "one.db", "two.db")]
    for path in paths[:3]:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE owned(id INTEGER PRIMARY KEY, payload BLOB)")
            db.executemany("INSERT INTO owned VALUES (?,?)", [(2, b"z"), (1, b"\x00\xff")])
    first = build_rollback_candidate(paths[0], paths[1], paths[2], paths[3], ("owned",))
    second = build_rollback_candidate(paths[0], paths[1], paths[2], paths[4], ("owned",))
    assert first["output_digest"] == second["output_digest"]


def test_null_composite_primary_key_is_rejected(tmp_path):
    paths = [tmp_path / name for name in ("source.db", "before.db", "after.db")]
    for path in paths:
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE owned(a TEXT, b TEXT, PRIMARY KEY(a,b))")
            db.execute("INSERT INTO owned VALUES (NULL, 'x')")
    with pytest.raises(ValueError, match="NULL primary key"):
        build_rollback_candidate(*paths, tmp_path / "out.db", ("owned",))


def test_rejects_tables_without_primary_key_and_triggers(tmp_path):
    source, before, after = [tmp_path / name for name in ("source.db", "before.db", "after.db")]
    for path in (source, before, after):
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE owned(value TEXT)")
    with pytest.raises(ValueError, match="primary key"):
        build_rollback_candidate(source, before, after, tmp_path / "out.db", ("owned",))


def test_wal_mode_snapshot_never_creates_input_sidecars(tmp_path):
    paths=[tmp_path/name for name in ('s.db','b.db','a.db')]
    for path in paths:
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE owned(id TEXT PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO owned VALUES ('x','before')")
        db.close()
    with sqlite3.connect(paths[2]) as db: db.execute("UPDATE owned SET value='after'")
    db.close()
    assert all(not Path(str(p)+'-wal').exists() for p in paths)
    build_rollback_candidate(*paths,tmp_path/'out.db',('owned',))
    assert all(not Path(str(p)+suffix).exists() for p in paths for suffix in ('-wal','-shm','-journal'))


def test_input_change_before_publication_refuses_candidate(tmp_path,monkeypatch):
    import knowledge_platform.distribution.sqlite_reverse_delta as module
    paths=[tmp_path/name for name in ('s.db','b.db','a.db')]
    for path in paths:
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE owned(id TEXT PRIMARY KEY, value TEXT)')
            db.execute("INSERT INTO owned VALUES ('x','before')")
    original=module._file_digest
    def mutate(path,copy_to=None):
        if copy_to is None and path == paths[0]:
            with sqlite3.connect(path) as db: db.execute("UPDATE owned SET value='concurrent-change'")
        return original(path,copy_to)
    monkeypatch.setattr(module,'_file_digest',mutate)
    with pytest.raises(ValueError,match='changed'):
        build_rollback_candidate(*paths,tmp_path/'out.db',('owned',))
    assert not (tmp_path/'out.db').exists()
    assert not list(tmp_path.glob('.rollback-inputs-*'))


def test_current_platform_catalog_schema_candidate(tmp_path):
    from test_knowledge_platform_local_runtime import _build_minimal_catalog
    import shutil
    source=tmp_path/'source.db'; _build_minimal_catalog(source)
    before=tmp_path/'before.db'; after=tmp_path/'after.db'
    shutil.copyfile(source,before);shutil.copyfile(source,after)
    with sqlite3.connect(after) as db:
        db.execute("UPDATE knowledge_spaces SET name='Post cutover name' WHERE id='space_kb_default'")
    output=tmp_path/'out.db'
    result=build_rollback_candidate(source,before,after,output,('knowledge_spaces',))
    assert result['tables']['knowledge_spaces']['update']==1
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT name FROM knowledge_spaces WHERE id='space_kb_default'").fetchone()[0]=='Post cutover name'
        assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
