"""Evidence-only probes for Phase 9/10 mixed-file ownership seams.

The probes deliberately check concrete source markers, not semantic equivalence.
They are useful to catch a missing migration seam but never prove that a future
repository extraction is safe; every result remains manual-review-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .boundary import build_phase9_boundary_manifest

_FORMAT = "agent-knowledge-platform-phase9-mixed-surface-shadow/v1"
_SYMBOL_ACTIONS = {"retain_harness", "extract_platform", "shared_adapter"}


class MixedSurfaceProbeError(ValueError):
    """Raised when a mixed-file probe definition or source is unsafe."""


def _source_without_full_line_comments(source: str) -> str:
    """Keep code/config lines while preventing comment-only marker evidence."""

    return "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )


@dataclass(frozen=True, slots=True)
class MixedSurfaceProbe:
    probe_id: str
    path: str
    required_markers: tuple[str, ...]
    forbidden_markers: tuple[str, ...] = ()
    expected_owner: str = "shared"
    expected_action: str = "manual"
    symbol_actions: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.probe_id or not self.probe_id.replace("_", "").isalnum():
            raise MixedSurfaceProbeError("mixed surface probe ID is invalid")
        path = Path(self.path)
        if (
            not self.path
            or path.is_absolute()
            or "\\" in self.path
            or ".." in path.parts
            or any(ord(character) < 32 or ord(character) == 127 for character in self.path)
        ):
            raise MixedSurfaceProbeError("mixed surface path is invalid")
        if not self.required_markers or any(not marker for marker in self.required_markers):
            raise MixedSurfaceProbeError("mixed surface required markers are invalid")
        if len(set(self.required_markers)) != len(self.required_markers):
            raise MixedSurfaceProbeError("mixed surface required markers must be unique")
        if len(set(self.forbidden_markers)) != len(self.forbidden_markers):
            raise MixedSurfaceProbeError("mixed surface forbidden markers must be unique")
        if self.expected_owner not in {"platform", "harness", "shared"}:
            raise MixedSurfaceProbeError("mixed surface owner is invalid")
        if self.expected_action not in {"extract", "retain", "manual"}:
            raise MixedSurfaceProbeError("mixed surface action is invalid")
        actions = self.symbol_actions or tuple((marker, "shared_adapter") for marker in self.required_markers)
        if tuple(marker for marker, _action in actions) != self.required_markers:
            raise MixedSurfaceProbeError("mixed surface symbol mapping must cover required markers in order")
        if any(action not in _SYMBOL_ACTIONS for _marker, action in actions):
            raise MixedSurfaceProbeError("mixed surface symbol action is invalid")


MIXED_SURFACE_PROBES = (
    MixedSurfaceProbe(
        "frontend_api_legacy_boundary",
        "frontend/src/lib/api.ts",
        ("const API_BASE", "DIRECT_BACKEND_API_BASE", "export async function"),
        ("knowledge-platform-console",),
        "shared",
        "manual",
        (
            ("const API_BASE", "retain_harness"),
            ("DIRECT_BACKEND_API_BASE", "retain_harness"),
            ("export async function", "retain_harness"),
        ),
    ),
    MixedSurfaceProbe(
        "electron_platform_manager_boundary",
        "electron/managers/platform.js",
        ("resolvePlatformSupervisor", "buildSupervisorArgs", "startInfra: startPlatform", "stopInfra: stopPlatform"),
        ("start-local-infra.sh", "docker compose"),
        "shared",
        "manual",
        (
            ("resolvePlatformSupervisor", "extract_platform"),
            ("buildSupervisorArgs", "extract_platform"),
            ("startInfra: startPlatform", "extract_platform"),
            ("stopInfra: stopPlatform", "extract_platform"),
        ),
    ),
    MixedSurfaceProbe(
        "platform_compose_boundary",
        "packages/knowledge-platform-deploy-cli/assets/compose.platform.yml",
        ("name: puddingknowledge", "PUDDINGKNOWLEDGE_HOME", "puddingknowledge-console"),
        ("PuddingClaw", "puddingclaw"),
        "platform",
        "extract",
        (
            ("name: puddingknowledge", "extract_platform"),
            ("PUDDINGKNOWLEDGE_HOME", "extract_platform"),
            ("puddingknowledge-console", "extract_platform"),
        ),
    ),
    MixedSurfaceProbe(
        "legacy_compose_boundary",
        "docker-compose.infra.yml",
        ("PUDDINGCLAW_HOST_HOME", "puddingclaw-postgres", "POSTGRES_DB=${POSTGRES_DB:-puddingclaw}"),
        (),
        "shared",
        "manual",
        (
            ("PUDDINGCLAW_HOST_HOME", "retain_harness"),
            ("puddingclaw-postgres", "retain_harness"),
            ("POSTGRES_DB=${POSTGRES_DB:-puddingclaw}", "retain_harness"),
        ),
    ),
    MixedSurfaceProbe(
        "platform_supervisor_boundary",
        "packages/knowledge-platform-deploy-cli/assets/platform-infra.sh",
        ('PROJECT_NAME="puddingknowledge"', "PUDDINGKNOWLEDGE_HOME", "docker compose"),
        ("start-local-infra.sh", "PuddingClaw"),
        "platform",
        "extract",
        (
            ('PROJECT_NAME="puddingknowledge"', "extract_platform"),
            ("PUDDINGKNOWLEDGE_HOME", "extract_platform"),
            ("docker compose", "extract_platform"),
        ),
    ),
    MixedSurfaceProbe(
        "legacy_cli_recipe_boundary",
        "packages/puddingclaw-deploy-cli/src/profile-commands.js",
        ("getCompositionRecipe", "legacy_extension_path", "migration_alias"),
        (),
        "shared",
        "manual",
        (
            ("getCompositionRecipe", "extract_platform"),
            ("legacy_extension_path", "retain_harness"),
            ("migration_alias", "shared_adapter"),
        ),
    ),
    MixedSurfaceProbe(
        "legacy_cli_recipe_contract",
        "packages/puddingclaw-deploy-cli/src/composition-recipes.js",
        ("target_product", "execution_allowed", "activation_allowed", "status"),
        (),
        "shared",
        "manual",
        (
            ("target_product", "extract_platform"),
            ("execution_allowed", "extract_platform"),
            ("activation_allowed", "extract_platform"),
            ("status", "extract_platform"),
        ),
    ),
)


def _marker_lines(source: str, marker: str) -> list[int]:
    return [
        line_number
        for line_number, line in enumerate(source.splitlines(), start=1)
        if not line.lstrip().startswith(("#", "//")) and marker in line
    ]


def run_mixed_surface_probes(
    *, repo_root: Path, probes: tuple[MixedSurfaceProbe, ...] = MIXED_SURFACE_PROBES
) -> dict[str, Any]:
    """Run the fixed marker probes without returning source text."""

    root = repo_root.expanduser().resolve()
    boundary = build_phase9_boundary_manifest()
    results: list[dict[str, Any]] = []
    for probe in probes:
        source_path = root / probe.path
        rule = boundary.owner_for(probe.path)
        ownership_matches = bool(
            rule
            and rule.owner == probe.expected_owner
            and rule.action == probe.expected_action
        )
        if not source_path.is_file() or source_path.is_symlink():
            symbol_actions = dict(probe.symbol_actions or tuple(
                (marker, "shared_adapter") for marker in probe.required_markers
            ))
            results.append({
                "probe_id": probe.probe_id,
                "path": probe.path,
                "status": "missing",
                "owner": probe.expected_owner,
                "action": probe.expected_action,
                "ownership_matches": ownership_matches,
                "required_marker_count": len(probe.required_markers),
                "missing_marker_ids": list(range(len(probe.required_markers))),
                "missing_marker_count": len(probe.required_markers),
                "forbidden_marker_ids": [],
                "forbidden_marker_count": 0,
                "required_marker_line_counts": [0 for _marker in probe.required_markers],
                "required_marker_lines": [[] for _marker in probe.required_markers],
                "symbol_evidence": [
                    {"action": symbol_actions[marker], "match_count": 0, "line_samples": []}
                    for marker in probe.required_markers
                ],
            })
            continue
        try:
            source = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise MixedSurfaceProbeError("mixed surface source is unreadable") from error
        executable_source = _source_without_full_line_comments(source)
        missing = [index for index, marker in enumerate(probe.required_markers) if marker not in executable_source]
        forbidden = [index for index, marker in enumerate(probe.forbidden_markers) if marker in executable_source]
        marker_lines = [_marker_lines(source, marker) for marker in probe.required_markers]
        symbol_actions = dict(probe.symbol_actions or tuple(
            (marker, "shared_adapter") for marker in probe.required_markers
        ))
        results.append({
            "probe_id": probe.probe_id,
            "path": probe.path,
            "status": "verified" if ownership_matches and not missing and not forbidden else "blocked",
            "owner": probe.expected_owner,
            "action": probe.expected_action,
            "ownership_matches": ownership_matches,
            "required_marker_count": len(probe.required_markers),
            "missing_marker_ids": missing,
            "missing_marker_count": len(missing),
            "forbidden_marker_ids": forbidden,
            "forbidden_marker_count": len(forbidden),
            "required_marker_line_counts": [len(lines) for lines in marker_lines],
            "required_marker_lines": [lines[:8] for lines in marker_lines],
            "symbol_evidence": [
                {
                    "action": symbol_actions[marker],
                    "match_count": len(lines),
                    "line_samples": lines[:8],
                }
                for marker, lines in zip(probe.required_markers, marker_lines)
            ],
        })
    return {
        "format": _FORMAT,
        "status": (
            "PHASE9_MIXED_SURFACE_MARKER_PASS_NOT_ACTIVATABLE"
            if all(item["status"] == "verified" for item in results)
            else "PHASE9_MIXED_SURFACE_MARKER_BLOCKED"
        ),
        "activation_allowed": False,
        "execution_allowed": False,
        "manual_review_required": True,
        "semantic_equivalence_proven": False,
        "probes": results,
    }


__all__ = ["MIXED_SURFACE_PROBES", "MixedSurfaceProbe", "MixedSurfaceProbeError", "run_mixed_surface_probes"]
