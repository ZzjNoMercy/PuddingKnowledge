from __future__ import annotations

import pytest

from knowledge_platform.continuity import ContinuityRequest, SqlitePlatformSidecar
from knowledge_platform.continuity.sqlite import SqliteContinuityError


def _request(*, capability: str = "wiki_compile", revision: str = "platform-local-v1") -> ContinuityRequest:
    return ContinuityRequest(
        capability=capability,
        space_id="space_kb_default",
        resource_key="asset_demo",
        idempotency_key="run_demo",
        deployment_revision=revision,
    )


def test_sqlite_sidecar_replays_after_process_restart_without_handler_call(tmp_path) -> None:
    calls: list[str] = []
    first = SqlitePlatformSidecar(
        database_path=tmp_path / "continuity.sqlite3",
        handlers={"wiki_compile": lambda request: calls.append(request.resource_key) or "knowledge://spaces/space_kb_default/wiki/asset_demo"},
    )
    first.stop_legacy()
    first.activate(deployment_revision="platform-local-v1")
    original = first.process(_request())

    def should_not_run(_request: ContinuityRequest) -> str:
        raise AssertionError("completed continuity run was executed again")

    restarted = SqlitePlatformSidecar(database_path=tmp_path / "continuity.sqlite3", handlers={"wiki_compile": should_not_run})
    replay = restarted.process(_request())

    assert calls == ["asset_demo"]
    assert replay.replayed is True
    assert replay.resource_uri == original.resource_uri
    assert restarted.active_revision == "platform-local-v1"
    assert restarted.legacy_enabled is False


def test_sqlite_sidecar_rejects_mixed_revision_and_restores_legacy_on_failure(tmp_path) -> None:
    def fail(_request: ContinuityRequest) -> str:
        raise RuntimeError("controlled failure")

    sidecar = SqlitePlatformSidecar(
        database_path=tmp_path / "continuity.sqlite3",
        handlers={"wiki_compile": fail},
    )
    sidecar.stop_legacy()
    sidecar.activate(deployment_revision="platform-local-v1")
    with pytest.raises(SqliteContinuityError):
        sidecar.process(_request(revision="platform-local-v2"))
    with pytest.raises(RuntimeError):
        sidecar.process(_request())

    sidecar.rollback(reason="controlled failure with secret-like context omitted")

    assert sidecar.active is False
    assert sidecar.legacy_enabled is True
    events = sidecar.audit_events()
    assert [event.event_type for event in events] == [
        "legacy_stopped",
        "sidecar_activated",
        "capability_claimed",
        "rolled_back_to_legacy",
    ]
    assert all("controlled failure" not in repr(event) for event in events)


def test_completed_idempotency_key_cannot_replay_across_revision(tmp_path) -> None:
    sidecar = SqlitePlatformSidecar(
        database_path=tmp_path / "continuity.sqlite3",
        handlers={"wiki_compile": lambda request: "knowledge://spaces/space_kb_default/wiki/asset_demo"},
    )
    sidecar.stop_legacy()
    sidecar.activate(deployment_revision="platform-local-v1")
    sidecar.process(_request())
    sidecar.rollback(reason="revision change drill")
    sidecar.stop_legacy()
    sidecar.activate(deployment_revision="platform-local-v2")

    with pytest.raises(SqliteContinuityError, match="another deployment revision"):
        sidecar.process(_request(revision="platform-local-v2"))


def test_sqlite_sidecar_rejects_cross_space_result_and_does_not_complete(tmp_path) -> None:
    sidecar = SqlitePlatformSidecar(
        database_path=tmp_path / "continuity.sqlite3",
        handlers={"wiki_compile": lambda _request: "knowledge://spaces/other-space/wiki/asset_demo"},
    )
    sidecar.stop_legacy()
    sidecar.activate(deployment_revision="platform-local-v1")

    with pytest.raises(SqliteContinuityError):
        sidecar.process(_request())

    assert [event.event_type for event in sidecar.audit_events()] == [
        "legacy_stopped",
        "sidecar_activated",
        "capability_claimed",
    ]
