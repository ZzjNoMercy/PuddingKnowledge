"""Static Platform dependency-closure preflight for the Phase 10 boundary.

This is an AST-only inventory.  It resolves imports within the two owned
Python packages and records external import roots, but it never imports the
application, resolves installed versions, contacts a package index, or
claims that an independent repository exists.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMAT = "agent-knowledge-platform-phase10-platform-dependency-closure/v1"
_PASS_STATUS = "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_PASS_NOT_ACTIVATABLE"
_BLOCKED_STATUS = "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_BLOCKED"
_SOURCE_ROOTS = (
    ("knowledge_contracts", "backend/knowledge_contracts"),
    ("knowledge_platform", "backend/knowledge_platform"),
)
_LOCAL_ROOTS = frozenset(name for name, _path in _SOURCE_ROOTS)
_FORBIDDEN_ROOTS = frozenset({"analytics", "graph", "harness", "knowledge", "runtime_control", "tools", "vanna", "scripts"})
_STDLIB_ROOTS = frozenset(sys.stdlib_module_names) | frozenset(sys.builtin_module_names) | {"__future__"}


class DependencyClosureError(ValueError):
    """Raised when static dependency evidence is malformed or unsafe."""


def _safe_relative(path: str) -> str:
    parts = Path(path).parts
    if not path or path.startswith(("/", "~")) or ".." in parts or "\\" in path:
        raise DependencyClosureError("dependency path must be relative")
    return path


def _safe_module(value: str) -> str:
    if not value or any(part not in {"_"} and not part.replace("_", "").isalnum() for part in value.split(".")):
        raise DependencyClosureError("dependency module is unsafe")
    return value


def _source_files(repo_root: Path) -> tuple[tuple[str, Path], ...]:
    files: list[tuple[str, Path]] = []
    for _name, relative_root in _SOURCE_ROOTS:
        root = repo_root / relative_root
        if not root.is_dir() or root.is_symlink():
            raise DependencyClosureError("dependency source root is unavailable")
        for path in sorted(root.rglob("*.py")):
            if path.is_symlink() or not path.is_file():
                raise DependencyClosureError("dependency source contains a symlink or unsupported file")
            files.append((path.relative_to(repo_root).as_posix(), path))
    return tuple(files)


def _module_for_path(relative_path: str) -> tuple[str, bool]:
    for root_name, root_path in _SOURCE_ROOTS:
        if relative_path == root_path + "/__init__.py":
            return root_name, True
        prefix = root_path + "/"
        if relative_path.startswith(prefix):
            relative = relative_path[len(prefix) :]
            if relative.endswith("/__init__.py"):
                return f"{root_name}.{relative[:-12].replace('/', '.')}", True
            return f"{root_name}.{relative[:-3].replace('/', '.')}", False
    raise DependencyClosureError("dependency file is outside declared roots")


def _relative_target(module: str, is_package: bool, level: int, imported: str | None) -> str:
    if level == 0:
        return _safe_module(imported or "")
    package = module if is_package else module.rsplit(".", 1)[0]
    parts = package.split(".")
    if level > len(parts):
        return _safe_module(imported or "")
    base = parts[: len(parts) - level + 1]
    if imported:
        base.append(imported)
    return _safe_module(".".join(base))


@dataclass(frozen=True, slots=True)
class DependencyClosureFinding:
    path: str
    line: int
    rule_id: str
    target: str

    def __post_init__(self) -> None:
        _safe_relative(self.path)
        if not isinstance(self.line, int) or self.line < 1:
            raise DependencyClosureError("dependency finding line is invalid")
        if self.rule_id not in {"forbidden_import", "unresolved_local_import"}:
            raise DependencyClosureError("dependency finding rule is unknown")
        _safe_module(self.target)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "rule_id": self.rule_id, "target": self.target}


@dataclass(frozen=True, slots=True)
class DependencyClosureResult:
    source_roots: tuple[tuple[str, str], ...]
    scanned_file_count: int
    import_edge_count: int
    local_edge_count: int
    external_import_roots: tuple[tuple[str, int], ...]
    findings: tuple[DependencyClosureFinding, ...]
    replay_consistent: bool
    dependency_resolution: str = "static_imports_only"
    network_contacted: bool = False
    independent_repository_verified: bool = False
    runtime_dependencies_verified: bool = False

    def __post_init__(self) -> None:
        if self.source_roots != _SOURCE_ROOTS:
            raise DependencyClosureError("dependency source roots are incomplete")
        if not isinstance(self.scanned_file_count, int) or self.scanned_file_count < 1:
            raise DependencyClosureError("scanned file count is invalid")
        if not isinstance(self.import_edge_count, int) or self.import_edge_count < 0:
            raise DependencyClosureError("import edge count is invalid")
        if not isinstance(self.local_edge_count, int) or not 0 <= self.local_edge_count <= self.import_edge_count:
            raise DependencyClosureError("local edge count is invalid")
        if self.dependency_resolution != "static_imports_only":
            raise DependencyClosureError("dependency resolution claim is too broad")
        if self.network_contacted or self.independent_repository_verified or self.runtime_dependencies_verified:
            raise DependencyClosureError("dependency shadow cannot claim runtime or release evidence")
        if tuple(root for root, _count in self.external_import_roots) != tuple(sorted(root for root, _count in self.external_import_roots)):
            raise DependencyClosureError("external imports must be sorted")

    @property
    def status(self) -> str:
        return _PASS_STATUS if self.replay_consistent and not self.findings else _BLOCKED_STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "execution_allowed": False,
            "source_roots": [{"name": name, "path": path} for name, path in self.source_roots],
            "scanned_file_count": self.scanned_file_count,
            "import_edge_count": self.import_edge_count,
            "local_edge_count": self.local_edge_count,
            "external_import_roots": [{"root": root, "edge_count": count} for root, count in self.external_import_roots],
            "findings": [finding.to_dict() for finding in self.findings],
            "finding_count": len(self.findings),
            "replay_consistent": self.replay_consistent,
            "dependency_resolution": self.dependency_resolution,
            "network_contacted": self.network_contacted,
            "independent_repository_verified": self.independent_repository_verified,
            "runtime_dependencies_verified": self.runtime_dependencies_verified,
            "scope": "same-checkout AST import closure only; versions, lockfiles, runtime loading, and release proof are pending",
        }


def _scan_once(repo_root: Path) -> tuple[int, int, int, tuple[tuple[str, int], ...], tuple[DependencyClosureFinding, ...]]:
    files = _source_files(repo_root)
    module_paths = {(_module_for_path(relative)[0], relative) for relative, _path in files}
    modules = {module for module, _relative in module_paths}
    external_counts: dict[str, int] = {}
    findings: list[DependencyClosureFinding] = []
    import_edges = 0
    local_edges = 0
    for relative, path in files:
        module, is_package = _module_for_path(relative)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, UnicodeError, SyntaxError) as error:
            raise DependencyClosureError("dependency source cannot be parsed") from error
        imports: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend((node.lineno, alias.name) for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append((node.lineno, _relative_target(module, is_package, node.level, node.module)))
        for line, target in imports:
            import_edges += 1
            root = target.split(".", 1)[0]
            if root in _LOCAL_ROOTS:
                local_edges += 1
                if target not in modules:
                    findings.append(DependencyClosureFinding(relative, line, "unresolved_local_import", target))
            elif root in _FORBIDDEN_ROOTS:
                findings.append(DependencyClosureFinding(relative, line, "forbidden_import", target))
            elif root not in _STDLIB_ROOTS:
                external_counts[root] = external_counts.get(root, 0) + 1
    return len(files), import_edges, local_edges, tuple(sorted(external_counts.items())), tuple(findings)


def build_dependency_closure_shadow(*, repo_root: Path) -> DependencyClosureResult:
    repo_root = repo_root.expanduser().resolve()
    first = _scan_once(repo_root)
    second = _scan_once(repo_root)
    return DependencyClosureResult(
        source_roots=_SOURCE_ROOTS,
        scanned_file_count=first[0],
        import_edge_count=first[1],
        local_edge_count=first[2],
        external_import_roots=first[3],
        findings=first[4],
        replay_consistent=first == second,
    )


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "DependencyClosureError",
    "DependencyClosureResult",
    "DependencyClosureFinding",
    "build_dependency_closure_shadow",
    "stable_digest",
]
