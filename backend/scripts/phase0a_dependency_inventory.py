"""Emit a deterministic Phase 0A source dependency/call-surface inventory.

This is an inventory aid, not a semantic proof of runtime behavior. Python
files expose AST imports and call targets; JS/TS files expose import/require
references; declarative and text files are still accounted for as opaque
surfaces so they cannot silently disappear from the migration register.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

_JS_IMPORT_RE = re.compile(
    r"(?:from\s+|import\s+|require\(\s*)[\"']([^\"']+)[\"']"
)
_PATH_REFERENCE_RE = re.compile(r"(?:backend|frontend|electron|skills|packages)/[A-Za-z0-9_./-]+")
def _git_revision(repo_root: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _first_rule(path: str, rules: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((rule for rule in rules if fnmatch.fnmatch(path, rule["glob"])), None)


def _python_surface(path: Path) -> tuple[list[str], list[str], str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [], [], f"parse-error:{type(exc).__name__}"
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))
    calls: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
        if parts:
            calls.add(".".join(reversed(parts)))
    return sorted(imports), sorted(calls), "parsed"


def _text_surface(path: Path) -> tuple[list[str], list[str], str]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [], f"read-error:{type(exc).__name__}"
    imports = sorted(set(_JS_IMPORT_RE.findall(content))) if path.suffix in {".js", ".mjs", ".ts", ".tsx"} else []
    references = sorted(set(_PATH_REFERENCE_RE.findall(content)))
    return imports, references, "opaque"


def build_inventory(repo_root: Path) -> dict[str, Any]:
    classification_path = repo_root / "docs" / "knowledge-platform" / "file-classification.yaml"
    classification = yaml.safe_load(classification_path.read_text(encoding="utf-8"))
    extensions = set(classification["scan_extensions"])
    rules = classification["rules"]
    paths: set[Path] = set()
    for raw_root in classification["scan_roots"]:
        root = repo_root / raw_root
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = list(root.rglob("*"))
        else:
            candidates = []
        paths.update(path for path in candidates if path.is_file() and path.suffix in extensions and "__pycache__" not in path.parts)

    records: list[dict[str, Any]] = []
    for path in sorted(paths):
        relative = str(path.relative_to(repo_root))
        rule = _first_rule(relative, rules)
        if path.suffix == ".py":
            imports, calls, parse_status = _python_surface(path)
        else:
            imports, calls, parse_status = _text_surface(path)
        records.append(
            {
                "path": relative,
                "extension": path.suffix,
                "target_owner": rule["target_owner"] if rule else None,
                "migration": rule["migration"] if rule else None,
                "imports": imports,
                "call_targets": calls,
                "parse_status": parse_status,
            }
        )
    digest_input = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "format": "agent-knowledge-platform-source-dependency-inventory/v1",
        "spec_revision": classification["spec_revision"],
        "status": "inventory-only-not-frozen",
        "repository_revision": _git_revision(repo_root),
        "file_count": len(records),
        "unclassified_files": [record["path"] for record in records if record["target_owner"] is None],
        "parse_errors": [record["path"] for record in records if record["parse_status"].startswith(("parse-error", "read-error"))],
        "graph_digest": f"sha256:{hashlib.sha256(digest_input).hexdigest()}",
        "files": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    print(json.dumps(build_inventory(args.repo_root.resolve()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
