from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.writer_authority import assign, enroll, suspend, thaw
from test_knowledge_platform_local_workspace import _catalog


def _fixture(tmp_path: Path, *, trace=False):
    source = tmp_path / "source.sqlite3"
    _catalog(source)
    wiki = tmp_path / "wiki"
    wiki.mkdir(mode=0o700)
    (wiki / "guide.md").write_text("# Frozen guide\n\nimmutable body\n", encoding="utf-8")
    state = tmp_path / "state"
    with open_persistent_workspace(state, catalog=source, wiki_root=wiki):
        pass

    # Keep a real committed WAL frame live while the export copies files.  The
    # connection owns no application workspace lock, and is intentionally left
    # open by the fixture so SQLite cannot checkpoint the frame away.
    writer = sqlite3.connect(state / "catalog.sqlite3")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE frozen_wal_evidence (value TEXT NOT NULL)")
    writer.execute("INSERT INTO frozen_wal_evidence VALUES ('committed-in-wal')")
    writer.commit()
    assert (state / "catalog.sqlite3-wal").exists()

    if trace:
        import asyncio
        from knowledge_platform.local.trace_store import SqliteTraceSink
        from test_knowledge_platform_trace_store import _event
        sink=SqliteTraceSink(state/'retrieval-traces.sqlite3')
        asyncio.run(sink.emit(_event()))
    authority = tmp_path / "authority"
    enroll(state, authority, "enrollment-1")
    suspend(state, "freeze-1")
    return state, authority, writer


def _export(state: Path, output: Path, operation_id: str = "freeze-1", **kwargs):
    from knowledge_platform.local.frozen_export import export_frozen_workspace

    return export_frozen_workspace(state, output, operation_id, **kwargs)


def _prepared_manifest(path: Path) -> Path:
    mappings = [{"domain": "knowledge_catalog", "source_id": "asset-1",
                 "resource_uri": "knowledge://assets/asset-1"}]
    summaries = []
    coverage = []
    for index, domain in enumerate(("session_harness", "knowledge_catalog", "connector_jobs")):
        domain_mappings = [
            {"source_id": item["source_id"], "resource_uri": item["resource_uri"]}
            for item in mappings if item["domain"] == domain
        ]
        count = len(domain_mappings)
        source_digest = "sha256:" + str(index + 3) * 64
        summaries.append({"domain": domain, "object_count": count,
                          "source_digest": source_digest})
        coverage.append({
            "domain": domain, "source_count": count, "target_count": count,
            "mapped_count": count, "source_ids_sha256": source_digest,
            "target_ids_sha256": "sha256:" + str(index + 6) * 64,
            "mapping_sha256": "sha256:" + hashlib.sha256(json.dumps(
                domain_mappings, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest(),
            "mapping_coverage_bps": 10000, "zero_object_attested": count == 0,
            "source_producer_format": "puddingclaw-cutover-domain-inventory/v1",
            "target_producer_format": (
                "puddingharness-cutover-domain-inventory/v1"
                if domain == "session_harness"
                else "puddingknowledge-cutover-domain-inventory/v1"
            ),
            "source_inventory_receipt_sha256": "sha256:" + "a" * 64,
            "target_inventory_receipt_sha256": "sha256:" + "b" * 64,
            "target_artifact_sha256": "sha256:" + "c" * 64,
            "source_producer": "puddingclaw",
            "target_producer": "puddingharness" if domain == "session_harness" else "puddingknowledge",
        })
    value = {
        "format": "agent-knowledge-platform-installation-migration/v1",
        "source": {"installation_id": "inst-1", "schema_revision": "rev-1",
                   "catalog_revision": "rev-1"},
        "targets": {"puddingknowledge": "puddingknowledge-local@0.1.0"},
        "object_summaries": summaries, "id_resource_mappings": mappings,
        "mapping_coverage": coverage, "credential_rebinds": [],
        "active_writers": {"session_harness": "puddingclaw",
                           "knowledge_catalog": "puddingclaw",
                           "connector_jobs": "puddingclaw"},
        "checkpoint": {
            "stage": "prepared",
            "knowledge_readiness_receipt_digest": "sha256:" + "d" * 64,
            "source_freeze_receipt_sha256": "sha256:" + "e" * 64,
            "source_freeze_evidence_sha256": "sha256:" + "f" * 64,
            "source_admission_capability_sha256": "sha256:" + "1" * 64,
            "source_freeze_operation_id": "freeze-1", "source_home_identity": "2" * 64,
        },
        "rollback_strategy": "no_write_until_finalized", "state": "PREPARED",
        "rollback_window_open": True,
    }
    path.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode())
    path.chmod(0o600)
    return path


def _source_facts(state: Path) -> dict[str, bytes]:
    return {
        relative: (state / relative).read_bytes()
        for relative in (
            "catalog.sqlite3",
            "catalog.sqlite3-wal",
            "catalog.sqlite3-shm",
            "workspace.json",
            "wiki/guide.md",
            ".workspace-freeze-v1.json",
        )
        if (state / relative).exists()
    }


def _regular_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_frozen_export_copies_raw_workspace_and_normalizes_wal_catalog(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        before = _source_facts(state)
        output = tmp_path / "export"
        result = _export(state, output)

        assert result["state"] == "verified_frozen_export"
        databases = result["normalized_databases"]
        assert "catalog" in databases
        report = databases["catalog"]
        assert report["state"] == "verified"
        normalized = output / "normalized/catalog/catalog.sqlite3"
        assert normalized.is_file()
        assert report["normalized_catalog"] == {
            "digest": "sha256:" + hashlib.sha256(normalized.read_bytes()).hexdigest(),
            "size": normalized.stat().st_size,
        }
        with sqlite3.connect(f"file:{normalized}?mode=ro", uri=True) as db:
            assert db.execute(
                "SELECT value FROM frozen_wal_evidence"
            ).fetchone() == ("committed-in-wal",)

        assert (output / "raw/wiki/guide.md").read_text(encoding="utf-8") == "# Frozen guide\n\nimmutable body\n"
        assert _regular_files(output / "raw") == _regular_files(state)
        for relative, data in before.items():
            assert (output / "raw" / relative).read_bytes() == data
        manifest = output / "manifest.json"
        if manifest.exists():
            assert json.loads(manifest.read_text(encoding="utf-8")).get("activation_allowed") is False
        assert _source_facts(state) == before
    finally:
        writer.close()


def test_frozen_export_accepts_actual_post_cutover_resuspension(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        manifest = _prepared_manifest(tmp_path / "prepared.json")
        assign(state, manifest, "freeze-1", "puddingknowledge")
        thaw(state, manifest, "freeze-1")
        journal = suspend(state, "rollback-window-1")
        assert len(journal["events"]) == 4
        assert journal["events"][-1]["state"] == "suspended"
        result = _export(state, tmp_path / "export", "rollback-window-1")
        assert result["state"] == "verified_frozen_export"
        exported = json.loads((tmp_path / "export/manifest.json").read_bytes())
        assert exported["plan"]["journal_sha256"]
    finally:
        writer.close()


def test_frozen_export_manifest_larger_than_authority_read_budget_completes(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        wiki = state / "wiki"
        for index in range(2000):
            note = wiki / f"note-{index:04}.md"
            note.write_text(f"# note {index}\n", encoding="utf-8")
            note.chmod(0o600)
        first = _export(state, tmp_path / "export")
        assert first["state"] == "verified_frozen_export" and first["idempotent"] is False
        assert (tmp_path / "export" / "manifest.json").stat().st_size > 65536
        assert _export(state, tmp_path / "export") == {**first, "idempotent": True}
    finally:
        writer.close()


def test_frozen_export_is_idempotent_and_rejects_completed_raw_tamper(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        output = tmp_path / "export"
        first = _export(state, output)
        second = _export(state, output)
        assert second == {**first, "idempotent": True}
        raw = output / "raw/wiki/guide.md"
        raw.write_text("tampered\n", encoding="utf-8")
        with pytest.raises((ValueError, RuntimeError)):
            _export(state, output)
    finally:
        writer.close()


@pytest.mark.parametrize("setup", ["unenrolled", "missing_freeze", "different_operation"])
def test_frozen_export_requires_enrollment_and_matching_suspension(tmp_path, setup):
    if setup == "unenrolled":
        source = tmp_path / "source.sqlite3"
        _catalog(source)
        wiki = tmp_path / "wiki"
        wiki.mkdir(mode=0o700)
        (wiki / "guide.md").write_text("guide\n", encoding="utf-8")
        state = tmp_path / "state"
        with open_persistent_workspace(state, catalog=source, wiki_root=wiki):
            pass
        writer = None
    else:
        state, _authority, writer = _fixture(tmp_path)
        if setup == "missing_freeze":
            (state / ".workspace-freeze-v1.json").unlink()
        operation = "other-freeze" if setup == "different_operation" else "freeze-1"
    try:
        with pytest.raises((ValueError, RuntimeError)):
            _export(state, tmp_path / "export", operation if setup != "unenrolled" else "freeze-1")
    finally:
        if writer is not None:
            writer.close()


def test_frozen_export_copy_callback_failure_can_retry(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        output = tmp_path / "export"
        calls = []

        def fail_once(relative):
            calls.append(str(relative))
            if len(calls) == 1:
                raise RuntimeError("injected copy interruption")

        with pytest.raises(RuntimeError, match="interruption"):
            _export(state, output, _after_copy=fail_once)
        result = _export(state, output)
        assert result["state"] == "verified_frozen_export"
        assert calls
    finally:
        writer.close()


def test_frozen_export_rejects_source_change_after_copy_callback(tmp_path):
    state, _authority, writer = _fixture(tmp_path)
    try:
        output = tmp_path / "export"
        changed = False

        def mutate_source(relative):
            nonlocal changed
            if not changed and str(relative).endswith("catalog.sqlite3"):
                writer.execute("INSERT INTO frozen_wal_evidence VALUES ('late')")
                writer.commit()
                changed = True

        with pytest.raises((ValueError, RuntimeError), match="change|snapshot|source|copy|WAL"):
            _export(state, output, _after_copy=mutate_source)
        assert changed
        assert json.loads((output / "manifest.json").read_text())["state"] == "copying"
    finally:
        writer.close()


def test_exported_catalog_can_feed_same_schema_reverse_builder(tmp_path):
    import shutil
    from knowledge_platform.distribution.sqlite_reverse_delta import build_rollback_candidate
    state, _authority, writer = _fixture(tmp_path)
    try:
        output=tmp_path/'export';_export(state,output)
        normalized=output/'normalized/catalog/catalog.sqlite3'
        source=tmp_path/'source-before.sqlite3';before=tmp_path/'target-before.sqlite3';after=tmp_path/'target-after.sqlite3'
        for path in (source,before,after):shutil.copyfile(normalized,path);path.chmod(0o600)
        # A private same-schema after-image exercises the existing reverse
        # consumer. This is not a legacy Claw schema conversion or CUTOVER.
        with sqlite3.connect(after) as db:
            db.execute("UPDATE knowledge_spaces SET name='changed-after-baseline'")
        result=build_rollback_candidate(source,before,after,tmp_path/'rollback.sqlite3',('knowledge_spaces',))
        with sqlite3.connect(tmp_path/'rollback.sqlite3') as db:
            assert db.execute('SELECT name FROM knowledge_spaces').fetchone()==('changed-after-baseline',)
            assert db.execute('SELECT value FROM frozen_wal_evidence').fetchone()==('committed-in-wal',)
        assert result['activation_allowed'] is False
    finally:writer.close()


@pytest.mark.parametrize('mode',['raw_missing','normalized_missing','normalized_corrupt','report_missing'])
def test_completed_export_artifacts_are_not_silently_repaired(tmp_path,mode):
    state,_,writer=_fixture(tmp_path)
    try:
        output=tmp_path/'export';_export(state,output);manifest=(output/'manifest.json').read_bytes()
        target=output/({'raw_missing':'raw/wiki/guide.md','normalized_missing':'normalized/catalog/catalog.sqlite3','normalized_corrupt':'normalized/catalog/catalog.sqlite3','report_missing':'normalized/catalog/report.json'}[mode])
        if mode=='normalized_corrupt':target.write_bytes(b'corrupt')
        else:target.unlink()
        with pytest.raises((ValueError,OSError)):_export(state,output)
        assert (output/'manifest.json').read_bytes()==manifest
        if mode!='normalized_corrupt':assert not target.exists()
    finally:writer.close()


def test_freeze_marker_without_suspended_revision_is_not_export_authority(tmp_path):
    from knowledge_platform.local import writer_authority as authority
    state,control,writer=_fixture(tmp_path)
    try:
        journal=authority.read(control/'journal.json');journal['events']=journal['events'][:1]
        (control/'journal.json').write_bytes(authority.encoded(journal))
        with pytest.raises(ValueError):_export(state,tmp_path/'export')
        assert not (tmp_path/'export').exists()
    finally:writer.close()


def test_sigkill_partial_export_releases_workspace_and_output_leases(tmp_path):
    import os,sys,subprocess,time
    state,_,writer=_fixture(tmp_path);output=tmp_path/'export';ready=tmp_path/'copy-ready'
    code='from knowledge_platform.local.frozen_export import export_frozen_workspace;from pathlib import Path;import time\n'
    code+='def pause(name):\n Path('+repr(str(ready))+').write_text(name)\n while True:time.sleep(.02)\n'
    code+='export_frozen_workspace('+repr(str(state))+','+repr(str(output))+',"freeze-1",_after_copy=pause)'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+10
        while not ready.exists():
            assert child.poll() is None and time.monotonic()<deadline;time.sleep(.02)
        child.kill();child.wait(timeout=5)
        assert _export(state,output)['state']=='verified_frozen_export'
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)
        writer.close()


def test_optional_real_trace_store_is_also_normalized(tmp_path):
    state,_,writer=_fixture(tmp_path,trace=True)
    try:
        output=tmp_path/'export';result=_export(state,output)
        assert set(result['normalized_databases'])=={'catalog','retrieval-traces'}
        with sqlite3.connect('file:'+str(output/'normalized/retrieval-traces/catalog.sqlite3')+'?mode=ro',uri=True) as db:
            payload=json.loads(db.execute('SELECT payload FROM trace_events').fetchone()[0])
            assert payload['trace_id']=='trace-1'
    finally:writer.close()
