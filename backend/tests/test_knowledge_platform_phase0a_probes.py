from __future__ import annotations

import pytest

from knowledge_platform.baseline import phase0a_probes


@pytest.mark.parametrize(
    ("family", "probe"),
    [
        ("knowledge_catalog_and_processing", phase0a_probes.probe_knowledge_catalog_and_processing),
        ("retrieval_and_evidence", phase0a_probes.probe_retrieval_and_evidence),
        ("wiki_compilation", phase0a_probes.probe_wiki_compilation),
        ("structured_query_and_vanna_boundary", phase0a_probes.probe_structured_query_and_vanna_boundary),
        ("semantic_authoring", phase0a_probes.probe_semantic_authoring),
        ("harness_wiring_boundary", phase0a_probes.probe_harness_wiring_boundary),
    ],
)
def test_phase0a_probe_is_deterministic_and_returns_observation(family: str, probe) -> None:
    first = probe()
    second = probe()

    assert first == second
    assert first
    assert family != ""
