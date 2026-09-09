"""Portable Golden baseline capture primitives."""

from .fixtures import (
    FixtureManifestValidation,
    fixture_manifest_digest,
    load_fixture_manifest,
    validate_fixture_manifest,
)
from .normalizer import (
    GoldenBaselineRecord,
    capture_baseline,
    capture_baseline_record_document,
    compare_baseline,
    normalize_for_baseline,
    normalized_digest,
)
from .runtime_graph import RuntimeCallEdge, RuntimeCallGraph, capture_runtime_call_graph
from .runtime_registry import (
    RuntimeProbeRegistryValidation,
    load_runtime_probe_registry,
    validate_runtime_probe_registry,
)
from .snapshot import CapabilitySourceSnapshot, SourceFileSnapshot, SourceSnapshot, build_source_snapshot

__all__ = [
    "GoldenBaselineRecord",
    "capture_baseline",
    "capture_baseline_record_document",
    "compare_baseline",
    "normalized_digest",
    "normalize_for_baseline",
    "RuntimeCallEdge",
    "RuntimeCallGraph",
    "capture_runtime_call_graph",
    "RuntimeProbeRegistryValidation",
    "load_runtime_probe_registry",
    "validate_runtime_probe_registry",
    "CapabilitySourceSnapshot",
    "SourceFileSnapshot",
    "SourceSnapshot",
    "build_source_snapshot",
    "FixtureManifestValidation",
    "fixture_manifest_digest",
    "load_fixture_manifest",
    "validate_fixture_manifest",
]
