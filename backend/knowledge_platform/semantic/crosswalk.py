"""Deterministic composition of generated Crosswalks and manual overrides."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
_ENTITY_KEY_RE = re.compile(r"^[^\s/\\]{1,240}$")
_ALLOWED_STATUSES = {"canonical_only", "auto_matched", "accepted", "candidate", "rejected", "unmatched", "inactive"}


class CrosswalkCompositionError(ValueError):
    """A generated Crosswalk or manual override is unsafe to activate."""


@dataclass(frozen=True, slots=True)
class CrosswalkPublication:
    resource_uri: str
    content_digest: str
    canonical_entity_count: int
    overrides_applied: int


class LocalCrosswalkPublisher:
    """Publish an active Crosswalk into an isolated content-addressed root."""

    def __init__(self, *, root: Path) -> None:
        self._root = root.expanduser().absolute()
        self.publish_count = 0

    def _mkdir_checked(self, path: Path) -> None:
        try:
            relative = path.relative_to(self._root)
        except ValueError as exc:
            raise OSError("Crosswalk publication path escaped root") from exc
        current = self._root
        if current.is_symlink():
            raise OSError("Crosswalk publication root must not be a symlink")
        current.mkdir(parents=True, exist_ok=True)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise OSError("Crosswalk publication directories must not be symlinks")
            current.mkdir(exist_ok=True)

    def _append_manifest(self, record: Mapping[str, object]) -> None:
        self._mkdir_checked(self._root)
        manifest = self._root / "publication-manifest.jsonl"
        lock_path = self._root / ".publication-manifest.lock"
        if manifest.is_symlink() or lock_path.is_symlink():
            raise OSError("Crosswalk publication manifest paths must not be symlinks")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                prior = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
                manifest.write_text(prior + json.dumps(dict(record), sort_keys=True) + "\n", encoding="utf-8")
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def publish(
        self,
        *,
        space_id: str,
        dimension_id: str,
        generated: Mapping[str, object],
        overrides: Sequence[Mapping[str, object]] = (),
    ) -> CrosswalkPublication:
        if not _ID_RE.fullmatch(space_id) or not _ID_RE.fullmatch(dimension_id):
            raise CrosswalkCompositionError("Crosswalk publication identity is invalid")
        active = compose_active_crosswalk(generated, overrides)
        encoded = (json.dumps(active, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        content_digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        target = self._root / "crosswalks" / space_id / dimension_id / f"{content_digest.removeprefix('sha256:')}.json"
        self._mkdir_checked(target.parent)
        if target.is_symlink():
            raise OSError("Crosswalk publication destination must not be a symlink")
        if target.exists():
            if target.read_bytes() != encoded:
                raise CrosswalkCompositionError("Crosswalk content-addressed destination mismatch")
            return CrosswalkPublication(
                resource_uri=f"knowledge://spaces/{space_id}/semantic-dimensions/{dimension_id}/crosswalk",
                content_digest=content_digest,
                canonical_entity_count=int(active["summary"]["canonical_entity_count"]),
                overrides_applied=int(active["summary"]["overrides_applied"]),
            )
        temporary = target.with_name(f".{target.name}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise FileExistsError("Crosswalk publication temporary output already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
        self._append_manifest(
            {
                "status": "published",
                "resource_uri": f"knowledge://spaces/{space_id}/semantic-dimensions/{dimension_id}/crosswalk",
                "content_digest": content_digest,
                "canonical_entity_count": active["summary"]["canonical_entity_count"],
                "overrides_applied": active["summary"]["overrides_applied"],
                "published_at": datetime.now(UTC).isoformat(),
            }
        )
        self.publish_count += 1
        return CrosswalkPublication(
            resource_uri=f"knowledge://spaces/{space_id}/semantic-dimensions/{dimension_id}/crosswalk",
            content_digest=content_digest,
            canonical_entity_count=int(active["summary"]["canonical_entity_count"]),
            overrides_applied=int(active["summary"]["overrides_applied"]),
        )


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _key(value: Mapping[str, object]) -> str:
    return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _binding_key(binding: Mapping[str, object]) -> tuple[str, str]:
    source_ref = str(binding.get("source_ref") or "")
    fields = binding.get("key_fields")
    if not _ID_RE.fullmatch(source_ref) or not isinstance(fields, Mapping) or not fields:
        raise CrosswalkCompositionError("source binding identity is invalid")
    return source_ref, _key(fields)


def _record_bindings(record: Mapping[str, object]) -> list[dict[str, object]]:
    bindings = record.get("bindings")
    if not isinstance(bindings, list):
        raise CrosswalkCompositionError("Crosswalk record bindings must be a list")
    normalized: list[dict[str, object]] = []
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise CrosswalkCompositionError("Crosswalk binding must be an object")
        _binding_key(binding)
        normalized.append(dict(binding))
    return normalized


def _validate_generated(generated: Mapping[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    records = generated.get("records")
    diagnostics = generated.get("source_diagnostics", [])
    if not isinstance(records, list) or not isinstance(diagnostics, list):
        raise CrosswalkCompositionError("Crosswalk records and diagnostics must be lists")
    seen: set[str] = set()
    canonical: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, Mapping) or record.get("record_kind") != "canonical_entity":
            raise CrosswalkCompositionError("records must contain canonical entities only")
        entity = record.get("entity")
        if not isinstance(entity, Mapping):
            raise CrosswalkCompositionError("canonical entity is missing")
        entity_key = str(entity.get("entity_key") or "")
        if not _ENTITY_KEY_RE.fullmatch(entity_key) or entity_key in seen:
            raise CrosswalkCompositionError("canonical entity keys must be valid and unique")
        seen.add(entity_key)
        resolution = record.get("resolution")
        if not isinstance(resolution, Mapping) or str(resolution.get("status") or "") not in _ALLOWED_STATUSES:
            raise CrosswalkCompositionError("canonical resolution status is invalid")
        copied = dict(record)
        copied["bindings"] = _record_bindings(record)
        canonical.append(copied)
    normalized_diagnostics: list[dict[str, object]] = []
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, Mapping) or diagnostic.get("record_kind") != "source_diagnostic":
            raise CrosswalkCompositionError("source diagnostics are invalid")
        copied = dict(diagnostic)
        copied["bindings"] = _record_bindings(diagnostic)
        resolution = copied.get("resolution")
        if not isinstance(resolution, Mapping) or str(resolution.get("status") or "") not in _ALLOWED_STATUSES:
            raise CrosswalkCompositionError("diagnostic resolution status is invalid")
        normalized_diagnostics.append(copied)
    return canonical, normalized_diagnostics


def _override_key(override: Mapping[str, object]) -> tuple[str, str]:
    source_ref = str(override.get("source_ref") or "")
    source_key = override.get("source_key")
    if not _ID_RE.fullmatch(source_ref) or not isinstance(source_key, Mapping) or not source_key:
        raise CrosswalkCompositionError("manual override source identity is invalid")
    return source_ref, _key(source_key)


def _set_resolution(record: dict[str, object], *, status: str, method: str, join_eligible: bool, evidence: str) -> None:
    prior = record.get("resolution")
    resolution = dict(prior) if isinstance(prior, Mapping) else {}
    prior_evidence = resolution.get("evidence")
    evidence_list = list(prior_evidence) if isinstance(prior_evidence, list) else []
    if evidence not in evidence_list:
        evidence_list.append(evidence)
    resolution.update(
        {"status": status, "join_eligible": join_eligible, "method": method, "evidence": evidence_list}
    )
    record["resolution"] = resolution


def _find_and_remove(
    canonical: list[dict[str, object]], diagnostics: list[dict[str, object]], binding_key: tuple[str, str]
) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for record in [*canonical, *diagnostics]:
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        keep: list[dict[str, object]] = []
        for binding in bindings:
            if _binding_key(binding) == binding_key:
                found.append(dict(binding))
            else:
                keep.append(binding)
        record["bindings"] = keep
    return found


def _append_diagnostic(
    diagnostics: list[dict[str, object]], *, bindings: list[dict[str, object]], status: str, evidence: str
) -> None:
    for record in diagnostics:
        current = record["bindings"]
        assert isinstance(current, list)
        if any(_binding_key(binding) in {_binding_key(item) for item in bindings} for binding in current):
            _set_resolution(record, status=status, method="manual_override", join_eligible=False, evidence=evidence)
            return
    diagnostics.append(
        {
            "record_kind": "source_diagnostic",
            "entity": None,
            "bindings": bindings,
            "resolution": {
                "status": status,
                "join_eligible": False,
                "method": "manual_override",
                "confidence": 1.0 if status == "rejected" else 0.0,
                "candidate_series": [],
                "evidence": [evidence],
            },
        }
    )


def compose_active_crosswalk(
    generated: Mapping[str, object], overrides: Sequence[Mapping[str, object]] = ()
) -> dict[str, object]:
    """Replay explicit ``bind``/``exclude`` operations over a generated baseline.

    A bind can only target an existing canonical entity.  An exclude removes a
    source binding from any canonical entity but preserves it as a rejected
    diagnostic, so no source-side value disappears silently.
    """

    if not isinstance(generated, Mapping):
        raise CrosswalkCompositionError("generated Crosswalk must be an object")
    canonical, diagnostics = _validate_generated(generated)
    entity_by_key = {str(record["entity"]["entity_key"]): record for record in canonical}
    normalized_overrides: list[dict[str, object]] = []
    for raw_override in overrides:
        if not isinstance(raw_override, Mapping):
            raise CrosswalkCompositionError("manual override must be an object")
        operation = str(raw_override.get("operation") or "")
        if operation not in {"bind", "exclude"}:
            raise CrosswalkCompositionError("manual override operation is invalid")
        source_ref, source_key = _override_key(raw_override)
        target = str(raw_override.get("entity_key") or "")
        if operation == "bind" and target not in entity_by_key:
            raise CrosswalkCompositionError("manual bind target is not a canonical entity")
        normalized_overrides.append(
            {"operation": operation, "source_ref": source_ref, "source_key": json.loads(source_key), "entity_key": target}
        )

    for override in normalized_overrides:
        operation = str(override["operation"])
        source_ref, source_key = _override_key(override)
        binding_key = (source_ref, source_key)
        removed = _find_and_remove(canonical, diagnostics, binding_key)
        if not removed:
            raise CrosswalkCompositionError("manual override source binding was not found")
        if any(
            bool(binding.get("canonical")) or str(binding.get("source_kind") or "") == "canonical_reference"
            for binding in removed
        ):
            raise CrosswalkCompositionError("manual overrides cannot mutate the canonical source of truth")
        diagnostics[:] = [record for record in diagnostics if record["bindings"]]
        evidence = f"manual override {operation} applied to source binding digest {_digest(binding_key)}"
        if operation == "bind":
            target = entity_by_key[str(override["entity_key"])]
            target_bindings = target["bindings"]
            assert isinstance(target_bindings, list)
            for binding in removed:
                if _binding_key(binding) not in {_binding_key(item) for item in target_bindings}:
                    target_bindings.append(binding)
            _set_resolution(target, status="accepted", method="manual_override", join_eligible=True, evidence=evidence)
        else:
            _append_diagnostic(diagnostics, bindings=removed, status="rejected", evidence=evidence)

    for record in canonical:
        bindings = record["bindings"]
        assert isinstance(bindings, list)
        has_noncanonical = any(
            not (bool(binding.get("canonical")) or str(binding.get("source_kind") or "") == "canonical_reference")
            for binding in bindings
        )
        if not has_noncanonical and str(record["resolution"].get("status")) == "accepted":
            _set_resolution(record, status="canonical_only", method="manual_override", join_eligible=False, evidence="all source bindings were excluded")

    payload: dict[str, object] = copy.deepcopy(dict(generated))
    payload.update(
        {
            "composition": "generated_crosswalk+manual_overrides",
            "generated_digest": _digest(generated),
            "override_digest": _digest(normalized_overrides),
            "records": canonical,
            "source_diagnostics": diagnostics,
            "summary": {
                "canonical_entity_count": len(canonical),
                "source_diagnostic_count": len(diagnostics),
                "canonical_source_binding_count": sum(len(record["bindings"]) for record in canonical),
                "overrides_applied": len(normalized_overrides),
            },
        }
    )
    payload["active_digest"] = _digest(payload)
    return payload


def validate_active_crosswalk(active: Mapping[str, object]) -> dict[str, object]:
    """Validate a persisted active Crosswalk before it is consumed."""

    if not isinstance(active, Mapping):
        raise CrosswalkCompositionError("active Crosswalk must be an object")
    declared = str(active.get("active_digest") or "")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", declared):
        raise CrosswalkCompositionError("active Crosswalk digest is missing or invalid")
    body = copy.deepcopy(dict(active))
    body.pop("active_digest", None)
    if _digest(body) != declared:
        raise CrosswalkCompositionError("active Crosswalk digest does not match content")
    _validate_generated(body)
    if body.get("composition") != "generated_crosswalk+manual_overrides":
        raise CrosswalkCompositionError("active Crosswalk composition marker is invalid")
    return dict(active)


__all__ = [
    "CrosswalkCompositionError",
    "CrosswalkPublication",
    "LocalCrosswalkPublisher",
    "compose_active_crosswalk",
    "validate_active_crosswalk",
]
