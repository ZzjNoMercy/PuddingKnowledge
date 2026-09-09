"""Fail-closed phase readiness evaluation for the Knowledge Platform."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .phase_gates import GateRequirement, GateStatus, PhaseReadinessReport


def __getattr__(name: str):
    # Lazy exports keep ``python -m knowledge_platform.readiness.phase_gates``
    # free of runpy's already-imported-module warning.
    if name in {
        "GateRequirement",
        "GateStatus",
        "PhaseReadinessReport",
        "evaluate_phase_gates",
        "load_phase_gate_manifest",
    }:
        from . import phase_gates

        return getattr(phase_gates, name)
    raise AttributeError(name)

__all__ = [
    "GateRequirement",
    "GateStatus",
    "PhaseReadinessReport",
    "evaluate_phase_gates",
    "load_phase_gate_manifest",
]
