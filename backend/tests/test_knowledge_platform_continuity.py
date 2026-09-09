from __future__ import annotations

import pytest

from knowledge_platform.continuity import (
    CONTINUITY_CAPABILITIES,
    ContinuityRequest,
    LocalLegacyWorkerControl,
    LocalPlatformSidecar,
)
from knowledge_platform.continuity.local import ContinuityError


def _request(capability: str, *, revision: str = "platform-local-v1") -> ContinuityRequest:
    return ContinuityRequest(
        capability=capability,
        space_id="space_kb_default",
        resource_key=f"asset-{capability}",
        idempotency_key=f"continuity-{capability}",
        deployment_revision=revision,
    )


def test_sidecar_runs_all_supported_capabilities_once_and_replays() -> None:
    legacy = LocalLegacyWorkerControl()
    calls: list[str] = []
    handlers = {
        capability: lambda request, capability=capability: calls.append(capability)
        or f"knowledge://spaces/{request.space_id}/continuity/{capability}"
        for capability in CONTINUITY_CAPABILITIES
    }
    sidecar = LocalPlatformSidecar(legacy=legacy, handlers=handlers)

    with pytest.raises(ContinuityError):
        sidecar.activate(deployment_revision="platform-local-v1")
    legacy.stop()
    sidecar.activate(deployment_revision="platform-local-v1")

    first = [sidecar.process(_request(capability)) for capability in CONTINUITY_CAPABILITIES]
    second = [sidecar.process(_request(capability)) for capability in CONTINUITY_CAPABILITIES]

    assert len(calls) == len(CONTINUITY_CAPABILITIES)
    assert all(not result.replayed for result in first)
    assert all(result.replayed for result in second)
    assert {event.event_type for event in sidecar.events} == {
        "sidecar_activated",
        "capability_completed",
    }


def test_failed_capability_can_be_rolled_back_to_legacy() -> None:
    legacy = LocalLegacyWorkerControl()

    def fail(_request: ContinuityRequest) -> str:
        raise RuntimeError("simulated sidecar failure")

    sidecar = LocalPlatformSidecar(legacy=legacy, handlers={"wiki_compile": fail})
    legacy.stop()
    sidecar.activate(deployment_revision="platform-local-v1")

    with pytest.raises(RuntimeError):
        sidecar.process(_request("wiki_compile"))
    sidecar.rollback(reason="local failure drill")

    assert legacy.enabled is True
    assert sidecar.active is False
    assert sidecar.active_revision == ""
    assert [event.event_type for event in sidecar.events] == [
        "sidecar_activated",
        "capability_failed",
        "rolled_back_to_legacy",
        "sidecar_revision_closed",
    ]
    assert all("local failure drill" not in repr(event) for event in sidecar.events)


def test_revision_and_space_fences_prevent_mixed_results() -> None:
    legacy = LocalLegacyWorkerControl()
    sidecar = LocalPlatformSidecar(
        legacy=legacy,
        handlers={
            "wiki_compile": lambda request: "knowledge://spaces/other-space/wiki/a",
        },
    )
    legacy.stop()
    sidecar.activate(deployment_revision="platform-local-v1")

    with pytest.raises(ContinuityError):
        sidecar.process(_request("wiki_compile", revision="platform-local-v2"))
    with pytest.raises(ValueError):
        sidecar.process(_request("wiki_compile"))

