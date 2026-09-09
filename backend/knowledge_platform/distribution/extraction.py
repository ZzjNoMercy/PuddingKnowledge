"""Fail-closed Phase 10 extraction preflight contracts.

The preflight records what a future ``git filter-repo`` extraction would
contain.  It never creates repositories, rewrites history, or copies files.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_contracts.protocol import HarnessProtocolVersion

from ..catalog.harness_migrations import HARNESS_SCHEMA_VERSIONS
from ..catalog.migrations import CURRENT_SCHEMA_VERSION
from ..readiness.phase_gates import PHASE_DEPENDENCIES, GateStatus, load_phase_gate_manifest
from ..transport.external_mcp import PLATFORM_MCP_PROTOCOL_VERSION
from .boundary import build_phase9_boundary_manifest
from .dependency_sbom import build_dependency_sbom_shadow

_FORMAT = "agent-knowledge-platform-phase10-extraction-manifest/v1"
_SAFE_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._*/-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]{1,256}$")
_MIXED_FILE_RULES = (
    (
        "backend/graph/deepagents_manager.py",
        "retain Harness runner/state wiring; exclude Platform business tool injection and keep REST/MCP as the only Platform boundary",
    ),
    (
        "backend/graph/middlewares/tool_intent_router.py",
        "exclude the entire ToolIntentRouter middleware from puddingharness per specification section 11.18; preserve the original only in PuddingClaw",
    ),
    (
        "backend/config.py",
        "split Harness runtime settings from Platform Catalog/provider settings and credential namespaces; copy no secret values",
    ),
    (
        "backend/pyproject.toml",
        "split project metadata, dependency groups, entry points, and test extras into the two target build graphs",
    ),
    (
        "backend/requirements.txt",
        "derive separate lock inputs; Platform retains knowledge providers while Harness excludes Platform business dependencies",
    ),
    (
        "backend/uv.lock",
        "regenerate target lockfiles from the source tag after dependency split; do not hand-copy or hand-edit the lock history",
    ),
    (
        "backend/prompts/tool_guides/knowledge-retrieval.md",
        "retain only generic Harness guidance; extract Platform capability guidance into the Platform-owned skill bundle",
    ),
    (
        "backend/skills/github-monitor/SKILL.md",
        "split Harness scheduling/orchestration from Platform capture and Catalog writes; preserve the legacy rollback path",
    ),
    (
        "backend/tests/test_knowledge_platform_agent_surface.py",
        "split Platform contract/boundary assertions from Harness tool registration and stop-order assertions",
    ),
    (
        "electron/main.js",
        "retain Harness shell lifecycle; extract Platform infrastructure ownership and IPC only into the Platform distribution",
    ),
    (
        "electron/package.json",
        "split Electron scripts, packaged assets, and dependencies by Harness shell versus Platform Console/infrastructure ownership",
    ),
    (
        "electron/managers/paths.js",
        "retain Harness Home paths and define a separate Platform Home namespace without sharing absolute path state",
    ),
    (
        "electron/managers/platform.js",
        "extract Platform lifecycle manager; retain only an explicit Harness compatibility boundary with no business implementation",
    ),
    (
        "docker-compose.yml",
        "split Harness services from Platform services and keep the legacy compose profile available for rollback",
    ),
    (
        "docker-compose.infra.yml",
        "extract Platform-owned Milvus/PostgreSQL/object infrastructure; retain Harness-only services in the legacy stack",
    ),
    (
        "frontend/src/app/knowledge/page.tsx",
        "extract Platform Console Knowledge UI and retain a legacy Harness route only behind the rollback boundary",
    ),
    (
        "frontend/src/app/analytics/page.tsx",
        "extract Platform Console Analytics UI and retain the legacy page until the release rollback window closes",
    ),
    (
        "frontend/src/lib/api.ts",
        "split legacy Claw API calls from the dependency-free Platform client and public contract types",
    ),
    (
        "packages/puddingclaw-deploy-cli/src/cli.js",
        "retain Harness installation commands; delegate Platform installation to the versioned Platform deploy CLI",
    ),
    (
        "packages/puddingclaw-deploy-cli/src/profile-commands.js",
        "retain legacy profile rollback behavior; emit only a recipe handoff for Platform-owned capabilities",
    ),
    (
        "packages/puddingclaw-deploy-cli/package.json",
        "split Harness package metadata and dependencies from the Platform deploy package without changing the legacy release",
    ),
    (
        "skills/puddingclaw/SKILL.md",
        "retire default Knowledge delegation from the Harness skill while preserving explicit legacy rollback instructions",
    ),
    (
        "scripts/start-local-infra.sh",
        "split local Harness and Platform lifecycle commands; preserve an explicit legacy rollback start path",
    ),
)
_MIXED_FILE_DETAILS = (
    (_MIXED_FILE_RULES[0][0], _MIXED_FILE_RULES[0][1], ("Harness runner/state wiring", "checkpoint persistence", "MCP discovery"), ("Platform business tool injection", "knowledge virtual mounts", "Platform middleware")),
    (_MIXED_FILE_RULES[1][0], _MIXED_FILE_RULES[1][1], (), ("entire ToolIntentRouter middleware",)),
    (_MIXED_FILE_RULES[2][0], _MIXED_FILE_RULES[2][1], ("Harness runtime settings", "MCP server settings", "legacy compatibility flags"), ("Knowledge root", "Milvus/Vanna/MinerU settings", "Platform credential providers")),
    (_MIXED_FILE_RULES[3][0], _MIXED_FILE_RULES[3][1], ("Harness project metadata", "Harness dependency groups", "Harness entry points"), ("knowledge_platform dependencies", "Knowledge/Analytics extras", "Platform test entry points")),
    (_MIXED_FILE_RULES[4][0], _MIXED_FILE_RULES[4][1], ("Harness dependency inputs",), ("Platform providers", "Knowledge/Analytics dependency groups")),
    (_MIXED_FILE_RULES[5][0], _MIXED_FILE_RULES[5][1], ("Harness lock entries",), ("Platform lock entries", "hand-edited cross-target lock history")),
    (_MIXED_FILE_RULES[6][0], _MIXED_FILE_RULES[6][1], ("generic Harness tool-discovery guidance",), ("Knowledge business tool names", "virtual knowledge paths", "Platform skill guidance")),
    (_MIXED_FILE_RULES[7][0], _MIXED_FILE_RULES[7][1], ("GitHub scheduling/orchestration", "legacy rollback behavior"), ("store_kb capture", "Catalog writes", "Knowledge-specific tool names")),
    (_MIXED_FILE_RULES[8][0], _MIXED_FILE_RULES[8][1], ("Harness registration/stop-order assertions",), ("Platform contract assertions", "Knowledge business implementation assertions")),
    (_MIXED_FILE_RULES[9][0], _MIXED_FILE_RULES[9][1], ("Electron shell lifecycle", "Harness IPC",), ("Platform infrastructure lifecycle", "Platform business IPC")),
    (_MIXED_FILE_RULES[10][0], _MIXED_FILE_RULES[10][1], ("Harness package metadata", "shell scripts", "shell dependencies"), ("Platform Console/infrastructure assets", "Platform dependencies")),
    (_MIXED_FILE_RULES[11][0], _MIXED_FILE_RULES[11][1], ("Harness Home paths", "legacy path compatibility"), ("Platform Home singleton", "shared absolute path state")),
    (_MIXED_FILE_RULES[12][0], _MIXED_FILE_RULES[12][1], ("explicit compatibility boundary",), ("Platform lifecycle implementation", "Knowledge business implementation")),
    (_MIXED_FILE_RULES[13][0], _MIXED_FILE_RULES[13][1], ("Harness services", "legacy compose rollback profile"), ("Platform services", "Platform infrastructure volumes")),
    (_MIXED_FILE_RULES[14][0], _MIXED_FILE_RULES[14][1], ("legacy Harness services",), ("Milvus", "PostgreSQL", "object storage", "Platform worker services")),
    (_MIXED_FILE_RULES[15][0], _MIXED_FILE_RULES[15][1], ("legacy Harness route", "rollback boundary"), ("Platform Console route", "Knowledge business UI ownership")),
    (_MIXED_FILE_RULES[16][0], _MIXED_FILE_RULES[16][1], ("legacy Analytics route", "rollback boundary"), ("Platform Console route", "Analytics business UI ownership")),
    (_MIXED_FILE_RULES[17][0], _MIXED_FILE_RULES[17][1], ("legacy Claw API calls", "legacy response adapters"), ("Platform client implementation", "Platform private types")),
    (_MIXED_FILE_RULES[18][0], _MIXED_FILE_RULES[18][1], ("Harness installation commands", "legacy rollback commands"), ("Platform installation implementation", "Platform infrastructure lifecycle")),
    (_MIXED_FILE_RULES[19][0], _MIXED_FILE_RULES[19][1], ("legacy profile selection", "legacy rollback behavior"), ("Platform profile implementation", "Platform-owned capabilities")),
    (_MIXED_FILE_RULES[20][0], _MIXED_FILE_RULES[20][1], ("Harness package metadata", "Harness dependencies", "legacy release scripts"), ("Platform deploy package metadata", "Platform dependencies")),
    (_MIXED_FILE_RULES[21][0], _MIXED_FILE_RULES[21][1], ("generic Harness delegation guidance", "explicit legacy rollback guidance"), ("default Knowledge delegation", "Platform business Tool names")),
    (_MIXED_FILE_RULES[22][0], _MIXED_FILE_RULES[22][1], ("Harness lifecycle commands", "legacy rollback start path"), ("Platform lifecycle commands", "Platform infrastructure ownership")),
)
_MIXED_FILE_PATHS = tuple(path for path, _rule, _preserve, _exclude in _MIXED_FILE_DETAILS)


class ExtractionPreflightError(ValueError):
    """The extraction plan is ambiguous or unsafe."""


def _safe_revision(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not _SAFE_REVISION.fullmatch(value)
        or ".." in value
        or "\\" in value
        or any(marker in value.lower() for marker in ("/users/", "/private/", "file://", "password=", "secret="))
    ):
        raise ExtractionPreflightError(f"extraction {label} is unsafe")


def _safe_text(value: Any, *, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(marker in value.lower() for marker in ("/users/", "/private/", "file://", "password=", "secret="))
    ):
        raise ExtractionPreflightError(f"extraction {label} is unsafe")


@dataclass(frozen=True, slots=True)
class ExtractionPathPlan:
    path: str
    action: str
    target: str
    rule: str
    preserve: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not _SAFE_RELATIVE_PATH.fullmatch(self.path):
            raise ExtractionPreflightError("extraction path must be relative")
        if self.action not in {"extract", "retain", "manual", "preserve-legacy"}:
            raise ExtractionPreflightError("extraction action is unsupported")
        if self.target not in {"puddingknowledge", "puddingharness", "shared-review", "PuddingClaw"}:
            raise ExtractionPreflightError("extraction target is unsupported")
        expected_target = {"extract": "puddingknowledge", "retain": "puddingharness", "manual": "shared-review", "preserve-legacy": "PuddingClaw"}[self.action]
        if self.target != expected_target:
            raise ExtractionPreflightError("extraction action and target disagree")
        if not isinstance(self.rule, str) or not self.rule.strip():
            raise ExtractionPreflightError("extraction rule is required")
        for label, values in (("preserve", self.preserve), ("exclude", self.exclude)):
            if not isinstance(values, tuple) or any(
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 240
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in values
            ):
                raise ExtractionPreflightError(f"extraction {label} symbols are invalid")


@dataclass(frozen=True, slots=True)
class ExtractionManifest:
    format: str
    phase: int
    status: str
    executable: bool
    activation_allowed: bool
    source_repository: str
    source_revision: str
    source_tag: str | None
    source_tag_signed: bool
    source_worktree_clean: bool
    extraction_method: str
    extraction_tool_version: str | None
    component_versions: tuple[tuple[str, str], ...]
    artifact_digests: tuple[tuple[str, str], ...]
    target_repositories: tuple[str, ...]
    path_plans: tuple[ExtractionPathPlan, ...]
    mixed_file_paths: tuple[str, ...]
    mixed_file_plans: tuple[ExtractionPathPlan, ...]
    missing_mixed_file_paths: tuple[str, ...]
    phase_gate_statuses: tuple[tuple[str, str], ...]
    phase_gate_blockers: tuple[str, ...]
    unresolved_gates: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.format != _FORMAT or self.phase != 10:
            raise ExtractionPreflightError("unsupported extraction manifest")
        if self.status != "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE":
            raise ExtractionPreflightError("preflight must remain non-executable")
        if self.executable or self.activation_allowed:
            raise ExtractionPreflightError("extraction preflight cannot execute or activate")
        if self.source_repository != "PuddingClaw":
            raise ExtractionPreflightError("source repository is not explicit")
        _safe_revision(self.source_revision, label="source revision")
        if not isinstance(self.source_tag_signed, bool) or not isinstance(self.source_worktree_clean, bool):
            raise ExtractionPreflightError("source tag/worktree states must be boolean")
        if self.source_tag_signed and not self.source_tag:
            raise ExtractionPreflightError("a signed source tag requires a tag name")
        if self.source_tag is not None:
            _safe_revision(self.source_tag, label="source tag")
        if self.extraction_tool_version is not None:
            _safe_text(self.extraction_tool_version, label="extraction tool version")
        if any(not isinstance(name, str) or not name.strip() or not isinstance(version, str) or not version.strip() for name, version in self.component_versions):
            raise ExtractionPreflightError("component versions must be explicit")
        if len({name for name, _ in self.component_versions}) != len(self.component_versions):
            raise ExtractionPreflightError("component versions must be unique")
        if any(not isinstance(name, str) or not name.strip() or not isinstance(digest, str) or not _SHA256.fullmatch(digest) for name, digest in self.artifact_digests):
            raise ExtractionPreflightError("artifact digests must be explicit SHA-256 values")
        if len({name for name, _ in self.artifact_digests}) != len(self.artifact_digests):
            raise ExtractionPreflightError("artifact digests must be unique")
        if self.target_repositories != ("puddingknowledge", "puddingharness"):
            raise ExtractionPreflightError("target repositories are not explicit")
        if self.extraction_method != "git-filter-repo":
            raise ExtractionPreflightError("history-preserving extraction method is required")
        if not self.path_plans or len({plan.path for plan in self.path_plans}) != len(self.path_plans):
            raise ExtractionPreflightError("extraction path plans must be unique")
        if tuple(self.mixed_file_paths) != _MIXED_FILE_PATHS:
            raise ExtractionPreflightError("mixed file coverage is incomplete or reordered")
        if tuple((plan.path, plan.rule, plan.preserve, plan.exclude) for plan in self.mixed_file_plans) != _MIXED_FILE_DETAILS:
            raise ExtractionPreflightError("mixed file symbol/config rules are incomplete or reordered")
        if len({phase_id for phase_id, _ in self.phase_gate_statuses}) != len(self.phase_gate_statuses):
            raise ExtractionPreflightError("phase-gate statuses must be unique")
        if not self.phase_gate_blockers:
            raise ExtractionPreflightError("current phase-gate blockers must be explicit")
        if not self.unresolved_gates:
            raise ExtractionPreflightError("unresolved extraction gates must be explicit")
        for label, values in (("phase-gate blocker", self.phase_gate_blockers), ("unresolved gate", self.unresolved_gates)):
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ExtractionPreflightError(f"{label} is invalid")
            for value in values:
                _safe_text(value, label=label)

    def plan_for_path(self, path: str) -> ExtractionPathPlan | None:
        """Resolve exact mixed-file overrides before broad inventory rules.

        A retained directory never grants permission to copy a mixed child.
        This method resolves ownership only; all execution gates still apply.
        """
        if not _SAFE_RELATIVE_PATH.fullmatch(path) or any(c in path for c in "*?"):
            raise ExtractionPreflightError("concrete extraction path is invalid")
        exact = [plan for plan in self.mixed_file_plans if plan.path == path]
        if exact:
            return exact[0]
        matches = [plan for plan in self.path_plans if fnmatch.fnmatchcase(path, plan.path)]
        if not matches:
            return None
        specificity = max(len(plan.path) for plan in matches)
        best = [plan for plan in matches if len(plan.path) == specificity]
        if len({(plan.action, plan.target) for plan in best}) != 1:
            raise ExtractionPreflightError("ambiguous extraction path ownership")
        return best[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "phase": self.phase,
            "status": self.status,
            "executable": self.executable,
            "activation_allowed": self.activation_allowed,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "source_tag": self.source_tag,
            "source_tag_signed": self.source_tag_signed,
            "source_worktree_clean": self.source_worktree_clean,
            "extraction_method": self.extraction_method,
            "extraction_tool_version": self.extraction_tool_version,
            "component_versions": dict(self.component_versions),
            "artifact_digests": dict(self.artifact_digests),
            "target_repositories": list(self.target_repositories),
            "path_resolution": "mixed-exact-then-most-specific",
            "path_plans": [
                {
                    "path": plan.path,
                    "action": plan.action,
                    "target": plan.target,
                    "rule": plan.rule,
                    "preserve": list(plan.preserve),
                    "exclude": list(plan.exclude),
                }
                for plan in self.path_plans
            ],
            "mixed_file_paths": list(self.mixed_file_paths),
            "mixed_file_plans": [
                {
                    "path": plan.path,
                    "action": plan.action,
                    "target": plan.target,
                    "rule": plan.rule,
                    "preserve": list(plan.preserve),
                    "exclude": list(plan.exclude),
                }
                for plan in self.mixed_file_plans
            ],
            "missing_mixed_file_paths": list(self.missing_mixed_file_paths),
            "phase_gate_statuses": dict(self.phase_gate_statuses),
            "phase_gate_blockers": list(self.phase_gate_blockers),
            "unresolved_gates": list(self.unresolved_gates),
        }

    def canonical_digest(self) -> str:
        """Digest the path-free manifest projection for replay comparison."""

        return _sha256_json(self.to_dict())


def _git_output(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ExtractionPreflightError("git source state is unavailable") from error
    return result.stdout.strip()


def _git_optional_output(repo_root: Path, *args: str) -> str | None:
    try:
        output = _git_output(repo_root, *args)
    except ExtractionPreflightError:
        return None
    return output or None


def _git_command_succeeded(repo_root: Path, *args: str) -> bool:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _read_json_value(repo_root: Path, relative_path: str, key: str) -> str:
    try:
        document = json.loads((repo_root / relative_path).read_text(encoding="utf-8"))
        value = document[key]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ExtractionPreflightError(f"missing versioned artifact: {relative_path}#{key}") from error
    if not isinstance(value, str) or not value.strip():
        raise ExtractionPreflightError(f"versioned artifact is not a non-empty string: {relative_path}#{key}")
    return value.strip()


def _component_and_artifact_inventory(
    repo_root: Path,
    *,
    source_revision: str,
    path_plans: tuple[ExtractionPathPlan, ...],
    mixed_file_plans: tuple[ExtractionPathPlan, ...],
) -> tuple[tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]]:
    package_sources = {
        "console": "packages/knowledge-platform-console/package.json",
        "console_contracts": "packages/knowledge-platform-console-contracts/package.json",
        "deploy_cli": "packages/knowledge-platform-deploy-cli/package.json",
        "skills": "packages/knowledge-platform-skills/package.json",
    }
    component_versions = [(name, _read_json_value(repo_root, path, "version")) for name, path in package_sources.items()]
    schema_paths = (
        "backend/knowledge_contracts/schemas/query-plan.schema.json",
        "backend/knowledge_contracts/schemas/query-result.schema.json",
        "backend/knowledge_contracts/schemas/notification-event.schema.json",
        "backend/knowledge_contracts/schemas/mcp-resource.schema.json",
    )
    schema_versions: list[tuple[str, str]] = []
    schema_hashes: list[tuple[str, str]] = []
    for relative_path in schema_paths:
        key = Path(relative_path).stem
        schema_id = _read_json_value(repo_root, relative_path, "$id")
        schema_versions.append((f"contract:{key}", schema_id))
        schema_hashes.append((relative_path, _sha256(repo_root / relative_path)))
    component_versions.extend(schema_versions)
    component_versions.extend(
        (
            ("platform_catalog_schema", f"v{CURRENT_SCHEMA_VERSION}"),
            ("harness_catalog_schema", f"v{max(HARNESS_SCHEMA_VERSIONS)}"),
            ("platform_rest_api", "v1"),
            ("platform_mcp_protocol", PLATFORM_MCP_PROTOCOL_VERSION),
            ("harness_protocol", HarnessProtocolVersion.V2.value),
            ("knowledge_package", "agent-knowledge-package/v1"),
            ("knowledge_package_sbom", "CycloneDX/1.5"),
        )
    )
    artifact_digests = [
        ("contract_schema_bundle", _sha256_json(schema_hashes)),
        (
            "dependency_sbom",
            build_dependency_sbom_shadow(repo_root=repo_root, source_revision=source_revision).sbom_digest.removeprefix("sha256:"),
        ),
        (
            "extraction_path_plan",
            _sha256_json([
                {
                    "path": plan.path,
                    "action": plan.action,
                    "target": plan.target,
                    "rule": plan.rule,
                    "preserve": plan.preserve,
                    "exclude": plan.exclude,
                }
                for plan in path_plans
            ]),
        ),
        (
            "mixed_file_plan",
            _sha256_json([
                {
                    "path": plan.path,
                    "action": plan.action,
                    "target": plan.target,
                    "rule": plan.rule,
                    "preserve": plan.preserve,
                    "exclude": plan.exclude,
                }
                for plan in mixed_file_plans
            ]),
        ),
    ]
    return tuple(component_versions), tuple(artifact_digests)


def _phase_gate_projection(repo_root: Path) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    """Project the current gate declaration without running registered checks."""

    manifest_path = repo_root / "docs/knowledge-platform/phase-gate-evidence.yaml"
    try:
        _spec_revision, phases = load_phase_gate_manifest(manifest_path)
    except (OSError, ValueError) as error:
        return (("phase_gate_manifest", "unreadable"),), (f"phase-gate manifest unreadable: {error}",)

    phase_statuses: list[tuple[str, str]] = []
    phase_blockers: list[str] = []
    phase_ready: dict[str, bool] = {}
    for phase_id, requirements in phases.items():
        blockers_before_phase = len(phase_blockers)
        for requirement in requirements:
            if requirement.status is not GateStatus.VERIFIED:
                phase_blockers.append(f"{phase_id}.{requirement.requirement_id} is {requirement.status.value}")
                continue
            evidence_hashes = dict(requirement.evidence_sha256)
            for reference in requirement.evidence_refs:
                actual_digest = _sha256(repo_root / reference)
                if actual_digest != evidence_hashes[reference]:
                    phase_blockers.append(f"{phase_id}.{requirement.requirement_id} evidence hash mismatch: {reference}")
        phase_ready[phase_id] = len(phase_blockers) == blockers_before_phase

    for phase_id, dependencies in PHASE_DEPENDENCIES.items():
        for dependency in dependencies:
            if not phase_ready[dependency]:
                phase_ready[phase_id] = False
                phase_blockers.append(f"{phase_id} depends on {dependency} exit")

    phase_statuses = [(phase_id, "ready" if phase_ready[phase_id] else "blocked") for phase_id in phases]

    return tuple(phase_statuses), tuple(phase_blockers)


def build_phase10_extraction_manifest(*, repo_root: Path) -> ExtractionManifest:
    repo_root = repo_root.expanduser().resolve()
    source_revision = _git_output(repo_root, "rev-parse", "HEAD")
    worktree_clean = not bool(_git_output(repo_root, "status", "--porcelain", "--untracked-files=all"))
    tags = tuple(filter(None, (_git_optional_output(repo_root, "tag", "--points-at", "HEAD", "--format=%(refname:short)") or "").splitlines()))
    source_tag = tags[0] if len(tags) == 1 else None
    source_tag_signed = bool(source_tag and _git_command_succeeded(repo_root, "tag", "-v", source_tag))
    boundary = build_phase9_boundary_manifest()
    plans: list[ExtractionPathPlan] = []
    for rule in boundary.rules:
        target = "puddingknowledge" if rule.action == "extract" else "puddingharness" if rule.action == "retain" else "shared-review"
        if rule.path in {"backend/knowledge/**", "backend/analytics/**", "backend/vanna/**", "frontend/src/app/knowledge/**", "frontend/src/app/analytics/**"}:
            plans.append(ExtractionPathPlan(rule.path, "preserve-legacy", "PuddingClaw", rule.rationale))
        else:
            plans.append(ExtractionPathPlan(rule.path, rule.action, target, rule.rationale))
    mixed_file_plans = tuple(
        ExtractionPathPlan(path, "manual", "shared-review", rule, preserve, exclude)
        for path, rule, preserve, exclude in _MIXED_FILE_DETAILS
    )
    component_versions, artifact_digests = _component_and_artifact_inventory(
        repo_root,
        source_revision=source_revision,
        path_plans=tuple(plans),
        mixed_file_plans=mixed_file_plans,
    )
    extraction_tool_version = _git_optional_output(repo_root, "filter-repo", "--version")
    existing = {
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts
    }
    missing_path_plan_anchors = boundary.validate_paths(existing)
    missing = tuple(path for path in _MIXED_FILE_PATHS if path not in existing)
    phase_gate_statuses, phase_gate_blockers = _phase_gate_projection(repo_root)
    unresolved_items: list[str] = []
    if not worktree_clean:
        unresolved_items.append("source_worktree_not_clean")
    unresolved_items.extend(phase_gate_blockers)
    if missing:
        unresolved_items.append("mixed_file_anchor_missing")
    unresolved_items.extend(f"path_plan_anchor_missing:{path}" for path in missing_path_plan_anchors)
    if not source_tag_signed:
        unresolved_items.append("signed_source_tag_missing_or_unverified")
    if extraction_tool_version is None:
        unresolved_items.append("git_filter_repo_tool_unavailable")
    unresolved_items.extend(
        (
            "phase_8_all_capabilities_stable_and_sidecar_cutover_verified",
            "phase_9_distribution_boundary_and_independent_console_verified",
            "phase_10_signed_source_tag_and_rc_validation",
            "mixed_file_symbol_level_review_complete",
        )
    )
    unresolved = tuple(unresolved_items)
    return ExtractionManifest(
        format=_FORMAT,
        phase=10,
        status="PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE",
        executable=False,
        activation_allowed=False,
        source_repository="PuddingClaw",
        source_revision=source_revision if source_revision else "unresolved",
        source_tag=source_tag,
        source_tag_signed=source_tag_signed,
        source_worktree_clean=worktree_clean,
        extraction_method="git-filter-repo",
        extraction_tool_version=extraction_tool_version,
        component_versions=component_versions,
        artifact_digests=artifact_digests,
        target_repositories=("puddingknowledge", "puddingharness"),
        path_plans=tuple(plans),
        mixed_file_paths=_MIXED_FILE_PATHS,
        mixed_file_plans=mixed_file_plans,
        missing_mixed_file_paths=missing,
        phase_gate_statuses=phase_gate_statuses,
        phase_gate_blockers=phase_gate_blockers,
        unresolved_gates=unresolved,
    )


__all__ = [
    "ExtractionManifest",
    "ExtractionPathPlan",
    "ExtractionPreflightError",
    "build_phase10_extraction_manifest",
]
