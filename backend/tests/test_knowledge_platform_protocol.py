from __future__ import annotations

import pytest

from knowledge_contracts import (
    AgentProtocolEnvelope,
    Correlation,
    HarnessProtocolVersion,
    Principal,
    project_legacy_payload,
)


def test_v2_protocol_rejects_analytics_model_id_even_when_nested() -> None:
    with pytest.raises(ValueError, match="analytics_model_id"):
        AgentProtocolEnvelope(
            HarnessProtocolVersion.V2,
            Principal("user-1"),
            Correlation("trace-1"),
            {"request": {"analytics_model_id": "legacy"}},
        )


def test_legacy_projection_drops_removed_field_without_mutating_input() -> None:
    payload = {"question": "q", "analytics_model_id": "legacy", "nested": {"analytics_model_id": "old"}}

    projected, dropped = project_legacy_payload(payload)

    assert projected == {"question": "q", "nested": {}}
    assert dropped == ("analytics_model_id", "nested.analytics_model_id")
    assert payload["analytics_model_id"] == "legacy"


def test_legacy_payload_can_be_read_and_emitted_as_v2_without_removed_field() -> None:
    envelope, dropped = AgentProtocolEnvelope.from_legacy(
        principal=Principal("user-1"),
        correlation=Correlation("trace-1"),
        payload={"question": "q", "analytics_model_id": "legacy"},
    )

    assert envelope.protocol_version is HarnessProtocolVersion.V2
    assert envelope.payload == {"question": "q"}
    assert dropped == ("analytics_model_id",)
