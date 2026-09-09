"""Build a deterministic, non-releaseable dependency SBOM candidate.

The candidate is deliberately limited to the current checkout's committed
Python lock and Platform package manifests.  It is useful evidence for a
future Phase 10 extraction, but it is not an independent repository SBOM and
never installs or resolves dependencies.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

_FORMAT = "agent-knowledge-platform-phase10-dependency-sbom-shadow/v1"
_STATUS = "PHASE10_DEPENDENCY_SBOM_SHADOW_PASS_NOT_ACTIVATABLE"
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]{1,256}$")
_SAFE_NAME = re.compile(r"^(?:@[A-Za-z0-9._-]+/)?[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_PLATFORM_NODE_MANIFESTS = (
    "packages/knowledge-platform-console/package.json",
    "packages/knowledge-platform-console-contracts/package.json",
    "packages/knowledge-platform-deploy-cli/package.json",
    "packages/knowledge-platform-skills/package.json",
)


class DependencySbomError(ValueError):
    """Raised when the local dependency evidence is incomplete or unsafe."""


def _safe_text(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or any(marker in text.lower() for marker in ("/users/", "/private/", "file://", "password=", "secret="))
    ):
        raise DependencySbomError(f"dependency SBOM {label} is unsafe")
    return text


def _read_file(repo_root: Path, relative: str) -> bytes:
    path = repo_root / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise DependencySbomError(f"dependency SBOM input is not a regular file: {relative}")
        return path.read_bytes()
    except OSError as error:
        raise DependencySbomError(f"dependency SBOM input is unreadable: {relative}") from error


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _component_name(value: Any, *, label: str) -> str:
    name = _safe_text(value, label=label)
    if not _SAFE_NAME.fullmatch(name):
        raise DependencySbomError(f"dependency SBOM {label} has an invalid component name")
    return name


def _component_version(value: Any, *, label: str) -> str:
    version = _safe_text(value, label=label)
    if any(character.isspace() for character in version):
        raise DependencySbomError(f"dependency SBOM {label} has an invalid component version")
    return version


def _purl(ecosystem: str, name: str, version: str) -> str:
    return f"pkg:{ecosystem}/{name}@{version}"


def _cyclonedx(
    *,
    source_revision: str,
    components: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
) -> dict[str, Any]:
    material = json.dumps(
        {"source_revision": source_revision, "components": components, "dependencies": dependencies},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "bomFormat": "CycloneDX",
        "components": components,
        "dependencies": dependencies,
        "metadata": {
            "component": {
                "name": "puddingclaw-platform-source-shadow",
                "type": "application",
                "version": source_revision,
            },
            "tools": [{"name": "puddingai-dependency-sbom-shadow", "version": "v1"}],
        },
        "serialNumber": "urn:uuid:" + hashlib.sha256(material).hexdigest()[:32],
        "specVersion": "1.5",
        "version": 1,
    }


@dataclass(frozen=True, slots=True)
class DependencySbomShadow:
    source_revision: str
    input_digests: tuple[tuple[str, str], ...]
    python_component_count: int
    node_component_count: int
    node_manifests_dependency_free: bool
    sbom: dict[str, Any]
    replay_consistent: bool = True
    network_contacted: bool = False
    dependency_install_performed: bool = False
    independent_repository_verified: bool = False
    release_artifact_generated: bool = False

    def __post_init__(self) -> None:
        if not _SAFE_REVISION.fullmatch(self.source_revision):
            raise DependencySbomError("dependency SBOM source revision is unsafe")
        if tuple(path for path, _digest_value in self.input_digests) != tuple(sorted(path for path, _ in self.input_digests)):
            raise DependencySbomError("dependency SBOM inputs must be sorted")
        if len({path for path, _ in self.input_digests}) != len(self.input_digests):
            raise DependencySbomError("dependency SBOM inputs must be unique")
        if any(not _DIGEST.fullmatch(digest_value) for _path, digest_value in self.input_digests):
            raise DependencySbomError("dependency SBOM input digest is invalid")
        if any(value < 0 for value in (self.python_component_count, self.node_component_count)):
            raise DependencySbomError("dependency SBOM component counts are invalid")
        if not isinstance(self.node_manifests_dependency_free, bool):
            raise DependencySbomError("dependency SBOM manifest state is invalid")
        if self.network_contacted or self.dependency_install_performed or self.independent_repository_verified or self.release_artifact_generated:
            raise DependencySbomError("dependency SBOM shadow cannot claim install or release evidence")
        if self.sbom.get("bomFormat") != "CycloneDX" or self.sbom.get("specVersion") != "1.5":
            raise DependencySbomError("dependency SBOM format is invalid")
        components = self.sbom.get("components")
        dependencies = self.sbom.get("dependencies")
        if not isinstance(components, list) or not isinstance(dependencies, list):
            raise DependencySbomError("dependency SBOM dependency graph is invalid")
        refs = {item.get("bom-ref") for item in components if isinstance(item, dict)}
        if len(refs) != len(components) or None in refs:
            raise DependencySbomError("dependency SBOM component references are invalid")
        for item in dependencies:
            if (
                not isinstance(item, dict)
                or item.get("ref") not in refs
                or not isinstance(item.get("dependsOn"), list)
                or any(value not in refs for value in item["dependsOn"])
            ):
                raise DependencySbomError("dependency SBOM dependency references are invalid")

    @property
    def status(self) -> str:
        return _STATUS

    @property
    def sbom_digest(self) -> str:
        payload = json.dumps(self.sbom, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _digest(payload)

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "execution_allowed": False,
            "source_repository": "PuddingClaw",
            "source_revision": self.source_revision,
            "input_digests": dict(self.input_digests),
            "python_component_count": self.python_component_count,
            "node_component_count": self.node_component_count,
            "node_manifests_dependency_free": self.node_manifests_dependency_free,
            "dependency_resolution": "python-uv-lock-plus-dependency-free-platform-node-manifests",
            "sbom_format": "CycloneDX/1.5",
            "sbom_digest": self.sbom_digest,
            "sbom": self.sbom,
            "replay_consistent": self.replay_consistent,
            "network_contacted": self.network_contacted,
            "dependency_install_performed": self.dependency_install_performed,
            "independent_repository_verified": self.independent_repository_verified,
            "release_artifact_generated": self.release_artifact_generated,
            "scope": "same-checkout locked dependency projection only; independent target SBOM and release proof are pending",
        }


def _build_once(repo_root: Path, *, source_revision: str) -> DependencySbomShadow:
    lock_relative = "backend/uv.lock"
    lock_bytes = _read_file(repo_root, lock_relative)
    try:
        lock_document = tomllib.loads(lock_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise DependencySbomError("backend/uv.lock cannot be parsed") from error
    packages = lock_document.get("package")
    if not isinstance(packages, list):
        raise DependencySbomError("backend/uv.lock package records are missing")

    components: list[dict[str, Any]] = []
    python_packages: list[dict[str, Any]] = []
    python_refs: dict[tuple[str, str], str] = {}
    python_refs_by_name: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for package in packages:
        if not isinstance(package, dict):
            raise DependencySbomError("uv lock package record is invalid")
        name = _component_name(package.get("name"), label="Python component name")
        version = _component_version(package.get("version"), label=f"Python component {name} version")
        key = (name.casefold(), version)
        if key in seen:
            continue
        seen.add(key)
        purl = _purl("pypi", name.casefold().replace("_", "-"), version)
        component = {
            "bom-ref": purl,
            "name": name,
            "purl": purl,
            "scope": "required",
            "type": "library",
            "version": version,
        }
        hashes: list[dict[str, str]] = []
        for artifact_key in ("sdist", "wheels"):
            artifacts = package.get(artifact_key, [])
            artifacts = artifacts if isinstance(artifacts, list) else [artifacts]
            for artifact in artifacts:
                if not isinstance(artifact, dict) or artifact.get("hash") is None:
                    continue
                artifact_hash = _safe_text(artifact.get("hash"), label=f"Python component {name} artifact hash")
                if not artifact_hash.startswith("sha256:") or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact_hash):
                    raise DependencySbomError(f"dependency SBOM {name} artifact hash is invalid")
                hashes.append({"alg": "SHA-256", "content": artifact_hash.removeprefix("sha256:")})
        if hashes:
            component["hashes"] = sorted(hashes, key=lambda item: item["content"])
        components.append(component)
        python_packages.append(package)
        python_refs[(name.casefold().replace("_", "-"), version)] = purl
        python_refs_by_name.setdefault(name.casefold().replace("_", "-"), set()).add(purl)

    dependency_records: list[dict[str, Any]] = []
    for package in python_packages:
        name = _component_name(package.get("name"), label="Python dependency owner name")
        version = _component_version(package.get("version"), label=f"Python dependency owner {name} version")
        ref = python_refs[(name.casefold().replace("_", "-"), version)]
        depends_on: set[str] = set()
        raw_dependencies = package.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise DependencySbomError(f"dependency SBOM {name} dependency records are invalid")
        for dependency in raw_dependencies:
            if not isinstance(dependency, dict):
                raise DependencySbomError(f"dependency SBOM {name} dependency record is invalid")
            dependency_name = _component_name(dependency.get("name"), label=f"{name} dependency name")
            normalized_name = dependency_name.casefold().replace("_", "-")
            if "version" in dependency:
                dependency_version = _component_version(
                    dependency.get("version"), label=f"{name} dependency {dependency_name} version"
                )
                dependency_refs = {python_refs.get((normalized_name, dependency_version))}
            else:
                dependency_refs = python_refs_by_name.get(normalized_name, set())
            dependency_refs.discard(None)
            if not dependency_refs:
                raise DependencySbomError(f"dependency SBOM cannot resolve {name} dependency {dependency_name}")
            depends_on.update(dependency_refs)
        dependency_records.append({"dependsOn": sorted(depends_on), "ref": ref})

    node_component_count = 0
    input_digests: list[tuple[str, str]] = [(lock_relative, _digest(lock_bytes))]
    node_manifests_dependency_free = True
    for relative in _PLATFORM_NODE_MANIFESTS:
        raw = _read_file(repo_root, relative)
        input_digests.append((relative, _digest(raw)))
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise DependencySbomError(f"Platform Node manifest cannot be parsed: {relative}") from error
        if not isinstance(document, dict):
            raise DependencySbomError(f"Platform Node manifest is not an object: {relative}")
        dependency_sections = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")
        if any(document.get(section) for section in dependency_sections):
            node_manifests_dependency_free = False
            raise DependencySbomError("Platform Node manifests contain unresolved dependency declarations")
        name = _component_name(document.get("name"), label=f"Node component name {relative}")
        version = _component_version(document.get("version"), label=f"Node component {name} version")
        purl = _purl("npm", name, version)
        components.append(
            {
                "bom-ref": purl,
                "name": name,
                "purl": purl,
                "scope": "required",
                "type": "application",
                "version": version,
                "properties": [{"name": "puddingai:source", "value": "platform-package-manifest"}],
            }
        )
        node_component_count += 1
        dependency_records.append({"dependsOn": [], "ref": purl})

    components.sort(key=lambda item: str(item["bom-ref"]))
    dependency_records.sort(key=lambda item: str(item["ref"]))
    return DependencySbomShadow(
        source_revision=source_revision,
        input_digests=tuple(sorted(input_digests)),
        python_component_count=len(seen),
        node_component_count=node_component_count,
        node_manifests_dependency_free=node_manifests_dependency_free,
        sbom=_cyclonedx(source_revision=source_revision, components=components, dependencies=dependency_records),
    )


def build_dependency_sbom_shadow(*, repo_root: Path, source_revision: str) -> DependencySbomShadow:
    repo_root = repo_root.expanduser().resolve()
    source_revision = _safe_text(source_revision, label="source revision")
    first = _build_once(repo_root, source_revision=source_revision)
    second = _build_once(repo_root, source_revision=source_revision)
    if first.to_dict() != second.to_dict():
        return DependencySbomShadow(
            source_revision=first.source_revision,
            input_digests=first.input_digests,
            python_component_count=first.python_component_count,
            node_component_count=first.node_component_count,
            node_manifests_dependency_free=first.node_manifests_dependency_free,
            sbom=first.sbom,
            replay_consistent=False,
        )
    return first


__all__ = ["DependencySbomError", "DependencySbomShadow", "build_dependency_sbom_shadow"]
