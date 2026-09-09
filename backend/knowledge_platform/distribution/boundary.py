"""Path-free, auditable ownership rules for the Phase 9 distribution split.

This is an inventory contract, not an extraction tool.  A path marked
``manual`` contains mixed Harness/Platform concerns and must be split by
symbol or configuration before a repository extraction is allowed.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Final


class DistributionBoundaryError(ValueError):
    """The distribution boundary is ambiguous or unsafe to consume."""


_OWNERS: Final[frozenset[str]] = frozenset({"platform", "harness", "shared"})
_ACTIONS: Final[frozenset[str]] = frozenset({"extract", "retain", "manual"})
_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._*/-]+$")
_REPOSITORY_PATH = re.compile(r"^(?!/)(?!~)(?!.*(?:^|/)\.\.(?:/|$))[^\\\x00-\x1f\x7f]+$")


@dataclass(frozen=True, slots=True)
class DistributionPathRule:
    path: str
    owner: str
    action: str
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _RELATIVE_PATH.fullmatch(self.path):
            raise DistributionBoundaryError("distribution path must be relative and path-safe")
        if self.path.endswith("/") or "//" in self.path:
            raise DistributionBoundaryError("distribution path has an invalid separator")
        if self.owner not in _OWNERS:
            raise DistributionBoundaryError("distribution owner is unsupported")
        if self.action not in _ACTIONS:
            raise DistributionBoundaryError("distribution action is unsupported")
        if not isinstance(self.rationale, str) or not self.rationale.strip() or len(self.rationale) > 500:
            raise DistributionBoundaryError("distribution rationale is invalid")
        if self.action == "extract" and self.owner != "platform":
            raise DistributionBoundaryError("only Platform paths may be extracted by this manifest")
        if self.action == "retain" and self.owner != "harness":
            raise DistributionBoundaryError("only Harness paths may be retained by this manifest")
        if self.action == "manual" and self.owner != "shared":
            raise DistributionBoundaryError("manual rules must be shared paths")


@dataclass(frozen=True, slots=True)
class DistributionBoundaryManifest:
    format: str
    phase: int
    status: str
    activation_allowed: bool
    source_repository: str
    target_repositories: tuple[str, ...]
    rules: tuple[DistributionPathRule, ...]
    unresolved_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.format != "agent-knowledge-platform-phase9-distribution-boundary/v1":
            raise DistributionBoundaryError("unsupported distribution boundary format")
        if self.phase != 9:
            raise DistributionBoundaryError("distribution boundary must be Phase 9")
        if self.status != "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE":
            raise DistributionBoundaryError("distribution inventory cannot claim activation")
        if self.activation_allowed:
            raise DistributionBoundaryError("distribution inventory must not allow activation")
        if self.source_repository != "PuddingClaw" or self.target_repositories != (
            "puddingknowledge",
            "puddingharness",
        ):
            raise DistributionBoundaryError("three-repository topology is not explicit")
        if not self.rules or len({rule.path for rule in self.rules}) != len(self.rules):
            raise DistributionBoundaryError("distribution rules must be non-empty and unique")
        if not self.unresolved_gates or any(not isinstance(gate, str) or not gate.strip() for gate in self.unresolved_gates):
            raise DistributionBoundaryError("unresolved distribution gates must be explicit")
        owners = {rule.owner for rule in self.rules}
        if owners != _OWNERS:
            raise DistributionBoundaryError("manifest must cover Platform, Harness, and shared paths")

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["target_repositories"] = list(self.target_repositories)
        payload["rules"] = [asdict(rule) for rule in self.rules]
        payload["unresolved_gates"] = list(self.unresolved_gates)
        return payload

    def owner_for(self, repository_path: str) -> DistributionPathRule | None:
        """Return the most-specific rule matching a repository-relative path."""

        if not isinstance(repository_path, str) or not _REPOSITORY_PATH.fullmatch(repository_path):
            raise DistributionBoundaryError("repository path is invalid")
        matches = [rule for rule in self.rules if fnmatch.fnmatchcase(repository_path, rule.path)]
        if not matches:
            return None
        most_specific_length = max(len(rule.path) for rule in matches)
        most_specific = [rule for rule in matches if len(rule.path) == most_specific_length]
        if len({(rule.owner, rule.action, rule.rationale) for rule in most_specific}) > 1:
            raise DistributionBoundaryError("distribution rules overlap at the same specificity")
        return most_specific[0]

    def validate_paths(self, existing_paths: set[str]) -> tuple[str, ...]:
        """Check that concrete boundary anchors exist without enumerating source files."""

        if not isinstance(existing_paths, set) or any(not isinstance(path, str) for path in existing_paths):
            raise DistributionBoundaryError("existing paths must be a set of relative strings")
        missing: list[str] = []
        for rule in self.rules:
            if rule.path.endswith("/**"):
                prefix = rule.path[:-3].rstrip("/")
                present = any(path == prefix or path.startswith(prefix + "/") for path in existing_paths)
            elif "*" in rule.path:
                present = any(fnmatch.fnmatchcase(path, rule.path) for path in existing_paths)
            else:
                present = rule.path in existing_paths
            if not present:
                missing.append(rule.path)
        return tuple(sorted(missing))

    def audit_scoped_paths(self, existing_paths: set[str], scope_patterns: tuple[str, ...]) -> dict[str, Any]:
        """Audit every file in explicit migration scopes for an unambiguous owner."""

        if not isinstance(existing_paths, set) or any(
            not isinstance(path, str) or not _REPOSITORY_PATH.fullmatch(path) for path in existing_paths
        ):
            raise DistributionBoundaryError("existing paths must be a set of relative strings")
        if not isinstance(scope_patterns, tuple) or not scope_patterns or any(
            not isinstance(pattern, str) or not _RELATIVE_PATH.fullmatch(pattern) for pattern in scope_patterns
        ):
            raise DistributionBoundaryError("distribution scope patterns are invalid")
        scoped = sorted(
            path for path in existing_paths
            if any(fnmatch.fnmatchcase(path, pattern) for pattern in scope_patterns)
        )
        unclassified: list[str] = []
        ambiguous: list[str] = []
        for path in scoped:
            try:
                owner = self.owner_for(path)
            except DistributionBoundaryError:
                ambiguous.append(path)
                continue
            if owner is None:
                unclassified.append(path)
        return {
            "scoped_file_count": len(scoped),
            "unclassified_paths": tuple(unclassified),
            "ambiguous_paths": tuple(ambiguous),
        }


def build_phase9_boundary_manifest() -> DistributionBoundaryManifest:
    """Build the current migration inventory from first principles."""

    rules = (
        DistributionPathRule(
            "backend/knowledge_platform/**",
            "platform",
            "extract",
            "Platform runtime, catalog, query, processing, transport, and continuity implementation.",
        ),
        DistributionPathRule(
            "backend/knowledge_contracts/**",
            "platform",
            "extract",
            "Versioned public REST/MCP contract definitions owned by the Platform.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-skills/**",
            "platform",
            "extract",
            "Portable Platform skill bundle with no Harness-private runtime dependency.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-console-contracts/**",
            "platform",
            "extract",
            "Dependency-free Console REST client and public query result boundary.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-deploy-cli/**",
            "platform",
            "extract",
            "Independent Platform Home and staged deployment control plane.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-console/**",
            "platform",
            "extract",
            "Platform contract diagnostics Console retained as a non-product support surface.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-web/**",
            "platform",
            "extract",
            "Independent PuddingKnowledge product UI consuming only public Platform contracts.",
        ),
        DistributionPathRule(
            "packages/knowledge-platform-runtime/**",
            "platform",
            "extract",
            "Platform local runtime build metadata, lock and isolated staging tool",
        ),
        DistributionPathRule(
            "scripts/start-knowledge-local.sh",
            "platform",
            "extract",
            "Local-only Platform API and product Web composition launcher with no Harness runtime dependency.",
        ),
        DistributionPathRule(
            "backend/knowledge/**",
            "harness",
            "retain",
            "Legacy Knowledge implementation remains in PuddingClaw as the rollback source.",
        ),
        DistributionPathRule(
            "backend/analytics/**",
            "harness",
            "retain",
            "Legacy Analytics/Vanna integration remains available during and after extraction.",
        ),
        DistributionPathRule(
            "backend/vanna/**",
            "harness",
            "retain",
            "Vendored Vanna fork is preserved until an independently verified Platform fork exists.",
        ),
        DistributionPathRule(
            "electron/**",
            "harness",
            "retain",
            "Electron remains the legacy product shell until Platform infrastructure ownership is split.",
        ),
        DistributionPathRule(
            "electron/managers/platform.js",
            "shared",
            "manual",
            "Platform lifecycle seam is mixed into the legacy Electron shell and requires symbol-level extraction.",
        ),
        DistributionPathRule(
            "packages/puddingclaw-deploy-cli/**",
            "harness",
            "retain",
            "Legacy PuddingClaw installer remains the Harness rollback surface during Platform migration.",
        ),
        DistributionPathRule(
            "packages/puddingclaw-deploy-cli/src/profile-commands.js",
            "shared",
            "manual",
            "Legacy profile facade needs symbol-level split between Harness extensions and Platform recipes.",
        ),
        DistributionPathRule(
            "packages/puddingclaw-deploy-cli/src/composition-recipes.js",
            "shared",
            "manual",
            "Platform composition recipe contract is embedded in the legacy CLI and requires symbol-level extraction.",
        ),
        DistributionPathRule(
            "frontend/src/app/knowledge/**",
            "harness",
            "retain",
            "Existing Knowledge UI remains legacy until an independent Platform Console is delivered.",
        ),
        DistributionPathRule(
            "frontend/src/app/analytics/**",
            "harness",
            "retain",
            "Existing Analytics UI remains legacy and must not be silently removed from PuddingClaw.",
        ),
        DistributionPathRule(
            "backend/config.py",
            "shared",
            "manual",
            "Mixed Harness and Platform settings require symbol-level ownership and credential namespace split.",
        ),
        DistributionPathRule(
            "backend/api/knowledge.py",
            "shared",
            "manual",
            "Legacy routes and Platform adapters coexist; extraction requires route-level mapping.",
        ),
        DistributionPathRule(
            "backend/api/mcp.py",
            "shared",
            "manual",
            "MCP discovery is shared while legacy business registration must be removed from Harness RC.",
        ),
        DistributionPathRule(
            "frontend/src/lib/api.ts",
            "shared",
            "manual",
            "Large API client contains legacy endpoints and future shared Platform contract types.",
        ),
        DistributionPathRule(
            "frontend/src/components/citations/SourcesPanel.tsx",
            "shared",
            "manual",
            "Mixed Harness inspector UI and legacy Knowledge citation decoding require component-level extraction.",
        ),
        DistributionPathRule(
            "docker-compose.infra.yml",
            "shared",
            "manual",
            "Milvus/PostgreSQL infrastructure must be split into Platform stack and Harness dependencies.",
        ),
        DistributionPathRule(
            "scripts/start-local-infra.sh",
            "shared",
            "manual",
            "Local infrastructure lifecycle currently mixes Harness and Platform services.",
        ),
    )
    return DistributionBoundaryManifest(
        format="agent-knowledge-platform-phase9-distribution-boundary/v1",
        phase=9,
        status="PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE",
        activation_allowed=False,
        source_repository="PuddingClaw",
        target_repositories=("puddingknowledge", "puddingharness"),
        rules=rules,
        unresolved_gates=(
            "phase_8_all_capabilities_stable_and_sidecar_cutover_verified",
            "phase_0a_golden_source_runtime_evidence_frozen",
            "phase_0b_catalog_vault_drain_production_evidence",
            "phase_7_crosswalk_collision_decision",
            "phase_9_independent_console_and_platform_distribution",
        ),
    )


def dump_manifest(manifest: DistributionBoundaryManifest) -> str:
    return json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


__all__ = [
    "DistributionBoundaryError",
    "DistributionBoundaryManifest",
    "DistributionPathRule",
    "build_phase9_boundary_manifest",
    "dump_manifest",
]
