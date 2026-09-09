from __future__ import annotations

import asyncio

import pytest

from knowledge_contracts import (
    BlobReadRequest,
    BlobReadResult,
    CitationCandidate,
    Correlation,
    Principal,
    TraceDimension,
    TraceEvent,
    validate_blob_read_result,
)
from knowledge_platform.evidence import DeterministicCitationNormalizer, VerifiedBlobReader


def test_citation_normalizer_deduplicates_and_bounds_portable_evidence() -> None:
    candidates = [
        CitationCandidate("asset-1", "knowledge://asset/1", "quote", {"page": "2"}, 0.9),
        CitationCandidate("asset-1", "knowledge://asset/1", "quote", {"page": "2"}, 0.9),
        CitationCandidate("asset-2", "knowledge://asset/2"),
    ]

    result = DeterministicCitationNormalizer().normalize(candidates, max_items=1)

    assert len(result) == 1
    assert result[0].asset_id == "asset-1"
    assert result[0].resource_uri == "knowledge://asset/1"


def test_citation_candidate_rejects_non_portable_uri_and_oversized_quote() -> None:
    with pytest.raises(ValueError):
        CitationCandidate("asset-1", "/private/raw.md")
    with pytest.raises(ValueError):
        CitationCandidate("asset-1", "knowledge://asset/1", "x" * 1201)
    with pytest.raises(ValueError):
        CitationCandidate("a", "knowledge://")
    with pytest.raises(ValueError):
        CitationCandidate("a", "knowledge://../secret")
    with pytest.raises(ValueError):
        CitationCandidate("a", "knowledge://file:///etc/passwd")
    with pytest.raises(ValueError):
        CitationCandidate("a", "knowledge://asset/1", "password=supersecret")
    with pytest.raises(ValueError):
        CitationCandidate("a", "knowledge://asset/1", locator={"path": "/private/raw.md"})


def test_blob_read_contract_requires_stable_uri_and_bounded_ranges() -> None:
    principal = Principal("user-1")
    correlation = Correlation("trace-1")
    with pytest.raises(ValueError):
        BlobReadRequest("/private/raw.md", principal, correlation)
    with pytest.raises(ValueError):
        BlobReadRequest("knowledge://blob/1", principal, correlation)
    with pytest.raises(ValueError):
        BlobReadRequest("knowledge://blob/1", principal, correlation, start=3, end=3)
    with pytest.raises(ValueError):
        BlobReadRequest("knowledge://blob/1", principal, correlation, end=8 * 1024 * 1024 + 1)

    result = BlobReadResult(
        "knowledge://blob/1",
        b"abc",
        "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        4,
        7,
    )
    assert result.end - result.start == len(result.content)
    request = BlobReadRequest(
        "knowledge://blob/1",
        principal,
        correlation,
        start=4,
        end=7,
        expected_digest="sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    )
    validate_blob_read_result(request, result)
    with pytest.raises(ValueError):
        validate_blob_read_result(
            request,
            BlobReadResult(
                "knowledge://other/1",
                b"abc",
                result.content_digest,
                4,
                7,
            ),
        )


def test_trace_contract_carries_digests_not_raw_payloads() -> None:
    event = TraceEvent(
        trace_id="trace-1",
        span_id="span-1",
        name="retrieval",
        phase="end",
        timestamp="2026-09-03T00:00:00Z",
        correlation=Correlation("trace-1"),
        status="ok",
        input_digest="sha256:0000000000000000000000000000000000000000000000000000000000000000",
        output_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111",
        dimensions=(TraceDimension("provider", "provider-x"),),
    )

    assert event.input_digest.startswith("sha256:")
    assert not hasattr(event, "input")
    assert not hasattr(event, "output")


def test_trace_dimension_rejects_paths_and_unbounded_values() -> None:
    with pytest.raises(ValueError):
        TraceDimension("path", "/private/raw.md")
    with pytest.raises(ValueError):
        TraceDimension("provider", "x" * 161)
    with pytest.raises(ValueError):
        TraceDimension("token", "opaque")
    with pytest.raises(ValueError):
        TraceDimension("path", "C:secret.md")
    with pytest.raises(ValueError):
        TraceDimension("provider", "sk-verysecret")


def test_trace_event_rejects_non_digest_and_non_timestamp_payloads() -> None:
    with pytest.raises(ValueError):
        TraceEvent("trace", "span", "retrieval", "input", "password=secret", Correlation("trace"))
    with pytest.raises(ValueError):
        TraceEvent(
            "trace",
            "span",
            "retrieval",
            "input",
            "2026-09-03T00:00:00Z",
            Correlation("trace"),
            input_digest="raw",
        )


def test_frozen_citation_locator_cannot_be_mutated_after_validation() -> None:
    candidate = CitationCandidate("asset-1", "knowledge://asset/1", locator={"section": "intro"})

    with pytest.raises(TypeError):
        candidate.locator["section"] = "C:\\private\\secret.md"


def test_verified_blob_reader_fences_provider_resource_range_and_digest() -> None:
    principal = Principal("user-1")
    correlation = Correlation("trace-1")
    request = BlobReadRequest("knowledge://blob/1", principal, correlation, start=0, end=3)
    digest = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    class Provider:
        async def read(self, _request):
            return BlobReadResult("knowledge://other/1", b"abc", digest, 0, 3)

    with pytest.raises(ValueError, match="resource"):
        asyncio.run(VerifiedBlobReader(Provider()).read(request))


def test_blob_result_requires_actual_content_digest() -> None:
    principal = Principal("user-1")
    correlation = Correlation("trace-1")
    request = BlobReadRequest("knowledge://blob/1", principal, correlation, start=0, end=3)
    wrong_digest = "sha256:0000000000000000000000000000000000000000000000000000000000000000"

    class Provider:
        async def read(self, _request):
            return BlobReadResult("knowledge://blob/1", b"abc", wrong_digest, 0, 3)

    with pytest.raises(ValueError, match="content digest"):
        asyncio.run(VerifiedBlobReader(Provider()).read(request))
