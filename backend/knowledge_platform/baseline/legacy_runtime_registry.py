"""Fail-closed validation for the supplemental legacy runtime probe registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from .runtime_registry import (
    _REFERENCE_RE,
    _required_digest,
    _safe_repo_path,
    _UniqueKeyLoader,
    _validate_observation_output,
)

_FORMAT = "agent-native-knowledge-platform-legacy-runtime-call-probes/v1"
_SPEC_REVISION = "v0.7"
_COVERAGE_SCOPE = "sanitized-legacy-observer-boundary-only"
_LEGACY_COVERAGE = "partial-observer-only-not-claimed-complete"
_STATUS = "local-observer-coverage-captured-boundary-review-pending"


@dataclass(frozen=True, slots=True)
class LegacyRuntimeProbeRegistryValidation:
    status: str
    required_capabilities: tuple[str, ...]
    executed_probe_ids: tuple[str, ...]
    pending_boundary_review_ids: tuple[str, ...]

    @property
    def reviewed(self) -> bool:
        return not self.pending_boundary_review_ids


def _required_string(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip() or raw != raw.strip():
        raise ValueError(f"{field} must be a trimmed non-empty string")
    return raw


def _required_list(raw: object, *, field: str) -> list[object]:
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{field} must be a non-empty list")
    return raw


def _validate_replay(raw: object, *, probe_id: str) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{probe_id}.replay must be a mapping")
    runs = raw.get("runs")
    if not isinstance(runs, int) or isinstance(runs, bool) or runs < 2:
        raise ValueError(f"{probe_id}.replay.runs must be at least 2")
    if raw.get("byte_equal") is not True:
        raise ValueError(f"{probe_id}.replay must record byte_equal=true")


def _validate_replay_evidence(
    raw_replay: Mapping[str, object] | None,
    *,
    probe_ids: list[str],
) -> None:
    """Require a current all-probe replay report for the strict review gate.

    The checked-in ``replay`` field is capture metadata. It documents how the
    artifact was produced, but cannot prove that the current checkout still
    replays to the same bytes. The CLI supplies this evidence only after
    running every observer in a child process.
    """

    if not isinstance(raw_replay, Mapping):
        raise ValueError("reviewed legacy runtime validation requires replay evidence")
    if raw_replay.get("status") != "matched":
        raise ValueError("reviewed legacy runtime validation requires a matched replay")
    replayed = raw_replay.get("replayed_probe_ids")
    mismatched = raw_replay.get("mismatched_probe_ids")
    skipped = raw_replay.get("skipped_probe_ids")
    expected = set(probe_ids)
    if (
        not isinstance(replayed, list)
        or any(not isinstance(probe_id, str) for probe_id in replayed)
        or set(replayed) != expected
        or len(replayed) != len(expected)
    ):
        raise ValueError("reviewed legacy runtime validation requires every probe to be replayed")
    if mismatched != [] or skipped != []:
        raise ValueError("reviewed legacy runtime validation requires no mismatched or skipped probes")


def validate_legacy_runtime_probe_registry(
    document: object,
    *,
    repo_root: Path | None = None,
    require_reviewed: bool = False,
    replay_evidence: Mapping[str, object] | None = None,
) -> LegacyRuntimeProbeRegistryValidation:
    if not isinstance(document, Mapping) or document.get("format") != _FORMAT:
        raise ValueError("invalid legacy runtime probe registry format")
    if document.get("spec_revision") != _SPEC_REVISION:
        raise ValueError("unsupported legacy runtime probe registry spec revision")
    if document.get("status") != _STATUS:
        raise ValueError("legacy runtime probe registry has an unsupported status")
    if document.get("coverage_scope") != _COVERAGE_SCOPE:
        raise ValueError("legacy runtime probe registry has an invalid coverage scope")
    if document.get("legacy_runtime_coverage") != _LEGACY_COVERAGE:
        raise ValueError("legacy runtime probe registry must declare partial coverage")

    contract = document.get("probe_contract")
    if not isinstance(contract, Mapping):
        raise ValueError("probe_contract must be a mapping")
    raw_prefixes = _required_list(contract.get("module_scope"), field="probe_contract.module_scope")
    prefixes: list[str] = []
    for raw_prefix in raw_prefixes:
        prefix = _required_string(raw_prefix, field="probe_contract.module_scope entry")
        if prefix in prefixes:
            raise ValueError(f"probe_contract.module_scope contains duplicate: {prefix!r}")
        prefixes.append(prefix)

    raw_capabilities = _required_list(document.get("required_capabilities"), field="required_capabilities")
    capabilities: list[str] = []
    for raw_capability in raw_capabilities:
        capability = _required_string(raw_capability, field="required_capabilities entry")
        if capability in capabilities:
            raise ValueError(f"required_capabilities contains duplicate: {capability!r}")
        capabilities.append(capability)

    raw_probes = _required_list(document.get("executed_probes"), field="executed_probes")
    probe_ids: list[str] = []
    covered_capabilities: set[str] = set()
    pending_review: list[str] = []
    for raw_probe in raw_probes:
        if not isinstance(raw_probe, Mapping):
            raise ValueError("executed legacy runtime probe must be a mapping")
        probe_id = _required_string(raw_probe.get("id"), field="executed probe id")
        if probe_id in probe_ids:
            raise ValueError(f"duplicate legacy runtime probe id: {probe_id!r}")
        capability = _required_string(raw_probe.get("capability_id"), field=f"{probe_id}.capability_id")
        if capability not in capabilities:
            raise ValueError(f"{probe_id}.capability_id is not required")
        raw_required_callees = _required_list(
            raw_probe.get("required_callee_modules"), field=f"{probe_id}.required_callee_modules"
        )
        required_callees: list[str] = []
        for raw_required_callee in raw_required_callees:
            required_callee = _required_string(
                raw_required_callee, field=f"{probe_id}.required_callee_modules entry"
            )
            if required_callee in required_callees:
                raise ValueError(f"{probe_id}.required_callee_modules contains duplicate: {required_callee!r}")
            required_callees.append(required_callee)
        raw_probe_prefixes = _required_list(
            raw_probe.get("module_scope", prefixes), field=f"{probe_id}.module_scope"
        )
        probe_prefixes: list[str] = []
        for raw_probe_prefix in raw_probe_prefixes:
            probe_prefix = _required_string(raw_probe_prefix, field=f"{probe_id}.module_scope entry")
            if probe_prefix in probe_prefixes:
                raise ValueError(f"{probe_id}.module_scope contains duplicate: {probe_prefix!r}")
            probe_prefixes.append(probe_prefix)
        reference = _required_string(raw_probe.get("reference"), field=f"{probe_id}.reference")
        if _REFERENCE_RE.fullmatch(reference) is None:
            raise ValueError(f"{probe_id}.reference must use module:function syntax")
        raw_output = raw_probe.get("output")
        if not isinstance(raw_output, Mapping):
            raise ValueError(f"{probe_id}.output must be a mapping")
        if repo_root is None:
            _safe_repo_path(raw_output.get("path"), field=f"{probe_id}.output.path")
            byte_count = raw_output.get("bytes")
            if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
                raise ValueError(f"{probe_id}.output.bytes must be a non-negative integer")
            _required_digest(raw_output.get("sha256"), field=f"{probe_id}.output.sha256")
        else:
            _validate_observation_output(
                raw_output,
                probe_id=probe_id,
                reference=reference,
                prefixes=probe_prefixes,
                repo_root=repo_root,
            )
            output_path = repo_root / str(raw_output["path"])
            report = yaml.safe_load(output_path.read_text(encoding="utf-8"))
            observed_edges = report["probes"][0]["edges"]
            missing_callees = [
                required_callee
                for required_callee in required_callees
                if not any(
                    edge["callee_module"] == required_callee
                    or edge["callee_module"].startswith(f"{required_callee}.")
                    for edge in observed_edges
                )
            ]
            if missing_callees:
                raise ValueError(f"{probe_id}.output is missing required callee modules: {missing_callees}")
        review = raw_probe.get("boundary_review")
        if not isinstance(review, Mapping) or review.get("status") not in {"pending", "reviewed"}:
            raise ValueError(f"{probe_id}.boundary_review must be pending or reviewed")
        if review.get("status") == "pending":
            pending_review.append(probe_id)
        _validate_replay(raw_probe.get("replay"), probe_id=probe_id)
        probe_ids.append(probe_id)
        covered_capabilities.add(capability)

    if set(capabilities) != covered_capabilities:
        raise ValueError(
            "legacy runtime probe registry does not cover every required capability: "
            f"missing={sorted(set(capabilities) - covered_capabilities)}"
        )
    result = LegacyRuntimeProbeRegistryValidation(
        str(document["status"]), tuple(capabilities), tuple(probe_ids), tuple(pending_review)
    )
    if require_reviewed and not result.reviewed:
        raise ValueError(
            "legacy runtime probe registry has pending boundary reviews: "
            f"{list(result.pending_boundary_review_ids)}"
        )
    if require_reviewed:
        _validate_replay_evidence(replay_evidence, probe_ids=probe_ids)
    return result


def load_legacy_runtime_probe_registry(
    path: Path,
    *,
    repo_root: Path | None = None,
    require_reviewed: bool = False,
    replay_evidence: Mapping[str, object] | None = None,
) -> tuple[dict[object, object], LegacyRuntimeProbeRegistryValidation]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    validation = validate_legacy_runtime_probe_registry(
        document,
        repo_root=repo_root,
        require_reviewed=require_reviewed,
        replay_evidence=replay_evidence,
    )
    return document, validation
