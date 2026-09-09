"""Fail-closed validation for the Phase 0A runtime probe registry."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

_FORMAT = "agent-native-knowledge-platform-runtime-call-probes/v1"
_SPEC_REVISION = "v0.7"
_OBSERVATION_SCOPE = "platform-contract-observation-only"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


@dataclass(frozen=True, slots=True)
class RuntimeProbeRegistryValidation:
    status: str
    required_families: tuple[str, ...]
    executed_probe_ids: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.status == "complete"


def _safe_repo_path(raw: object, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    path = Path(raw.strip())
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must not escape the repository")
    return path


def _required_digest(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or _DIGEST_RE.fullmatch(raw) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return raw


def _validate_output(raw_output: object, *, probe_id: str, repo_root: Path) -> None:
    if not isinstance(raw_output, Mapping):
        raise ValueError(f"{probe_id}.output must be a mapping")
    relative = _safe_repo_path(raw_output.get("path"), field=f"{probe_id}.output.path")
    byte_count = raw_output.get("bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        raise ValueError(f"{probe_id}.output.bytes must be a non-negative integer")
    expected = _required_digest(raw_output.get("sha256"), field=f"{probe_id}.output.sha256")
    candidate = repo_root / relative
    target = candidate.resolve()
    root = repo_root.resolve()
    if candidate.is_symlink() or not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"{probe_id}.output is missing, escaping, or symlinked")
    if target.stat().st_size != byte_count:
        raise ValueError(f"{probe_id}.output byte count drift")
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f"{probe_id}.output digest drift")


def _validate_observation_output(
    raw_output: object,
    *,
    probe_id: str,
    reference: str,
    prefixes: list[str],
    repo_root: Path,
) -> None:
    """Validate that a captured artifact is the declared probe's report."""

    _validate_output(raw_output, probe_id=probe_id, repo_root=repo_root)
    assert isinstance(raw_output, Mapping)
    path = repo_root / _safe_repo_path(raw_output["path"], field=f"{probe_id}.output.path")

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{probe_id}.output contains duplicate JSON key: {key!r}")
            result[key] = value
        return result

    try:
        report = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{probe_id}.output is not valid JSON: {exc}") from exc
    if not isinstance(report, Mapping) or report.get("format") != "agent-native-knowledge-platform-runtime-call-graph-report/v1":
        raise ValueError(f"{probe_id}.output has an invalid runtime graph report format")
    if report.get("observation_only") is not True:
        raise ValueError(f"{probe_id}.output must be observation-only")
    reports = report.get("probes")
    if not isinstance(reports, list) or len(reports) != 1 or not isinstance(reports[0], Mapping):
        raise ValueError(f"{probe_id}.output must contain exactly one probe report")
    observed = reports[0]
    if observed.get("format") != "agent-knowledge-platform-runtime-call-graph/v1":
        raise ValueError(f"{probe_id}.output has an invalid observed graph format")
    if observed.get("observation_only") is not True:
        raise ValueError(f"{probe_id}.output observed graph must be observation-only")
    if observed.get("probe_reference") != reference:
        raise ValueError(f"{probe_id}.output probe reference does not match registry")
    if observed.get("module_prefixes") != sorted(set(prefixes)):
        raise ValueError(f"{probe_id}.output module prefixes do not match registry")
    loaded_modules = observed.get("loaded_modules")
    if not isinstance(loaded_modules, list) or any(
        not isinstance(module_name, str) or not module_name.strip() for module_name in loaded_modules
    ):
        raise ValueError(f"{probe_id}.output loaded_modules must be a list of module names")
    if loaded_modules != sorted(set(loaded_modules)):
        raise ValueError(f"{probe_id}.output loaded_modules must be sorted and unique")
    raw_edges = observed.get("edges")
    if not isinstance(raw_edges, list) or not raw_edges:
        raise ValueError(f"{probe_id}.output edges must be a non-empty list")
    edge_fields = ("caller_module", "caller_function", "callee_module", "callee_function")
    canonical_edges: list[dict[str, str]] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, Mapping) or set(raw_edge) != set(edge_fields):
            raise ValueError(f"{probe_id}.output edges must contain exactly the four call fields")
        edge = {field: raw_edge[field] for field in edge_fields}
        if any(not isinstance(value, str) or not value.strip() for value in edge.values()):
            raise ValueError(f"{probe_id}.output edge fields must be non-empty strings")
        canonical_edges.append(edge)
    if canonical_edges != sorted(canonical_edges, key=lambda edge: tuple(edge[field] for field in edge_fields)):
        raise ValueError(f"{probe_id}.output edges must be sorted")
    if len({tuple(edge[field] for field in edge_fields) for edge in canonical_edges}) != len(canonical_edges):
        raise ValueError(f"{probe_id}.output edges must be unique")
    graph_digest = observed.get("graph_digest")
    if not isinstance(graph_digest, str) or not graph_digest.startswith("sha256:"):
        raise ValueError(f"{probe_id}.output graph_digest must be a SHA-256 digest")
    digest_payload = {
        "edges": canonical_edges,
        "loaded_modules": loaded_modules,
    }
    encoded = json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    actual_digest = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    if graph_digest != actual_digest:
        raise ValueError(f"{probe_id}.output graph_digest drift")


def _validate_replay_evidence(
    raw_replay: Mapping[str, object] | None,
    *,
    probe_ids: list[str],
) -> None:
    """Require an all-probe, byte-matching replay report for a strict gate.

    Static output validation proves that the checked-in artifact is internally
    consistent.  It cannot prove that the declared probe was executed.  The
    CLI supplies this report only after running every probe and comparing its
    stdout byte-for-byte with the artifact.
    """

    if not isinstance(raw_replay, Mapping):
        raise ValueError("complete runtime probe validation requires replay evidence")
    if raw_replay.get("status") != "matched":
        raise ValueError("complete runtime probe validation requires a matched replay")
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
        raise ValueError("complete runtime probe validation requires every probe to be replayed")
    if mismatched != [] or skipped != []:
        raise ValueError("complete runtime probe validation requires no mismatched or skipped probes")


def validate_runtime_probe_registry(
    document: object,
    *,
    repo_root: Path | None = None,
    require_complete: bool = False,
    replay_evidence: Mapping[str, object] | None = None,
) -> RuntimeProbeRegistryValidation:
    if not isinstance(document, Mapping) or document.get("format") != _FORMAT:
        raise ValueError("invalid runtime probe registry format")
    if document.get("spec_revision") != _SPEC_REVISION:
        raise ValueError("unsupported runtime probe registry spec revision")
    status = document.get("status")
    if status not in {"observation-primitive-ready-coverage-not-complete", "complete"}:
        raise ValueError("runtime probe registry has an unsupported status")
    if document.get("coverage_scope") != _OBSERVATION_SCOPE:
        raise ValueError("runtime probe registry must declare platform-contract observation scope")
    if document.get("legacy_runtime_coverage") != "not-claimed":
        raise ValueError("runtime probe registry must not claim legacy runtime coverage")
    raw_families = document.get("required_probe_families")
    if not isinstance(raw_families, list) or not raw_families:
        raise ValueError("required_probe_families must be a non-empty list")
    families: list[str] = []
    for family in raw_families:
        if not isinstance(family, str) or not family.strip() or family != family.strip() or family in families:
            raise ValueError(f"invalid or duplicate runtime probe family: {family!r}")
        families.append(family)

    raw_probes = document.get("executed_probes")
    if not isinstance(raw_probes, list):
        raise ValueError("executed_probes must be a list")
    probe_ids: list[str] = []
    covered_families: set[str] = set()
    for raw_probe in raw_probes:
        if not isinstance(raw_probe, Mapping):
            raise ValueError("executed runtime probe must be a mapping")
        probe_id = raw_probe.get("id")
        family = raw_probe.get("family")
        reference = raw_probe.get("reference")
        prefixes = raw_probe.get("module_prefixes")
        scope = raw_probe.get("scope")
        if not isinstance(probe_id, str) or not probe_id.strip() or probe_id != probe_id.strip() or probe_id in probe_ids:
            raise ValueError(f"invalid or duplicate runtime probe id: {probe_id!r}")
        if not isinstance(family, str) or family not in families:
            raise ValueError(f"{probe_id}.family is not required")
        if not isinstance(reference, str) or _REFERENCE_RE.fullmatch(reference) is None:
            raise ValueError(f"{probe_id}.reference must use module:function syntax")
        if scope != _OBSERVATION_SCOPE:
            raise ValueError(f"{probe_id}.scope must remain platform-contract observation-only")
        if not isinstance(prefixes, list) or not prefixes or any(
            not isinstance(prefix, str) or not prefix.strip() or prefix != prefix.strip() for prefix in prefixes
        ):
            raise ValueError(f"{probe_id}.module_prefixes must be a non-empty list of trimmed strings")
        if len(set(prefixes)) != len(prefixes):
            raise ValueError(f"{probe_id}.module_prefixes contains duplicates")
        probe_ids.append(probe_id)
        covered_families.add(family)
        if status == "complete":
            if repo_root is None:
                raise ValueError("complete runtime probe validation requires repo_root")
            _validate_observation_output(
                raw_probe.get("output"),
                probe_id=probe_id,
                reference=reference,
                prefixes=prefixes,
                repo_root=repo_root,
            )
            review = raw_probe.get("boundary_review")
            if not isinstance(review, Mapping) or review.get("status") != "reviewed":
                raise ValueError(f"{probe_id}.boundary_review must be reviewed")
        elif raw_probe.get("output") is not None:
            if repo_root is None:
                raise ValueError("runtime probe output validation requires repo_root")
            _validate_observation_output(
                raw_probe["output"],
                probe_id=probe_id,
                reference=reference,
                prefixes=prefixes,
                repo_root=repo_root,
            )

    if status == "complete" and set(families) != covered_families:
        raise ValueError(
            "complete runtime probe registry does not cover every required family: "
            f"missing={sorted(set(families) - covered_families)}"
        )
    result = RuntimeProbeRegistryValidation(status, tuple(families), tuple(probe_ids))
    if require_complete and not result.complete:
        raise ValueError("runtime probe registry is not complete")
    if require_complete:
        _validate_replay_evidence(replay_evidence, probe_ids=probe_ids)
    return result


def load_runtime_probe_registry(
    path: Path,
    *,
    repo_root: Path | None = None,
    require_complete: bool = False,
    replay_evidence: Mapping[str, object] | None = None,
) -> tuple[dict[object, object], RuntimeProbeRegistryValidation]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    validation = validate_runtime_probe_registry(
        document,
        repo_root=repo_root,
        require_complete=require_complete,
        replay_evidence=replay_evidence,
    )
    return document, validation
