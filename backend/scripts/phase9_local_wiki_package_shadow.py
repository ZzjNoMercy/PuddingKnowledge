"""Build a portable local-Wiki Package/Workspace shadow from the current local data."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.package import (
    KnowledgePackageBuilder,
    PackageBuildError,
    WorkspaceMaterializer,
    export_package_zip,
    import_package_zip,
    validate_package,
)

_DEFAULT_WIKI_ROOT = Path("/Users/pet/Documents/knowledge/llm-wiki/wiki")
_DEFAULT_PACKAGE = Path("artifacts/phase9-local-wiki-package")
_DEFAULT_WORKSPACE = Path("artifacts/phase9-local-wiki-workspace")
_SPACE_ID = "space_local_wiki"
_DATASET_ID = "collection_local_wiki"
_MAX_PAGES = 5_000
_MAX_PAGE_BYTES = 8 * 1024 * 1024
_TITLE_RE = re.compile(r"(?ms)^---\n(?P<frontmatter>.*?)(?:\n---\n|\Z)")


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode("utf-8")).hexdigest()


def _stable_output_path(path: Path) -> Path:
    requested = path.expanduser().absolute()
    if requested.is_symlink():
        raise OSError("shadow output must not be a symlink")
    return requested.resolve()


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _pages(root: Path) -> list[Path]:
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise OSError("local Wiki root must be a regular directory")
    result = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if relative in {"index.md", "log.md"}:
            continue
        if path.is_symlink() or not path.is_file():
            raise OSError("local Wiki page must be a regular file")
        cursor = path.parent
        while True:
            if cursor.is_symlink():
                raise OSError("local Wiki page contains a symlinked parent")
            if cursor == root:
                break
            if cursor.parent == cursor:
                raise OSError("local Wiki page escaped its root")
            cursor = cursor.parent
        if path.stat().st_size > _MAX_PAGE_BYTES:
            raise ValueError("local Wiki page exceeds the package shadow size limit")
        result.append(path)
    if not result:
        raise ValueError("local Wiki root contains no package pages")
    if len(result) > _MAX_PAGES:
        raise ValueError("local Wiki page count exceeds the package shadow limit")
    return result


def _page_title(path: Path, fallback: str) -> str:
    content = path.read_text(encoding="utf-8")
    match = _TITLE_RE.match(content[:64 * 1024])
    if match:
        for line in match.group("frontmatter").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "title" and value.strip():
                return value.strip().strip("'\"")[:500]
    return fallback[:500]


def _catalog_revision(records: list[tuple[str, str, int]]) -> str:
    digest = hashlib.sha256()
    for slug, content_digest, size in records:
        digest.update(slug.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content_digest.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _write_report(path: Path, result: dict[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(path)
    return result


def run_shadow(
    *,
    wiki_root: Path = _DEFAULT_WIKI_ROOT,
    package_dir: Path = _DEFAULT_PACKAGE,
    workspace_dir: Path | None = None,
    package_zip: Path | None = None,
    report_path: Path | None = None,
) -> dict[str, Any]:
    package_dir = _stable_output_path(package_dir)
    report_path = _stable_output_path(report_path or package_dir.parent / "phase9-local-wiki-package-shadow-report.json")
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-wiki-package-shadow/v1",
        "status": "PHASE9_LOCAL_WIKI_PACKAGE_SHADOW_FAILED",
        "activation_allowed": False,
        "catalog_unchanged": True,
        "source_root_path_digest": _path_digest(wiki_root),
        "package": {"created": False, "asset_count": 0, "package_revision": None},
        "zip": {"created": False, "imported": False, "package_revision": None, "asset_count": 0},
        "workspace": {"created": False},
    }
    try:
        pages = _pages(wiki_root)
        assets: list[dict[str, Any]] = []
        asset_files: dict[str, Path] = {}
        revision_records: list[tuple[str, str, int]] = []
        for page in pages:
            slug = page.relative_to(wiki_root.expanduser().absolute()).with_suffix("").as_posix()
            content_digest, size = _file_digest(page)
            asset_id = "asset_wiki_" + hashlib.sha256(f"{_SPACE_ID}:{slug}".encode()).hexdigest()[:32]
            assets.append(
                {
                    "id": asset_id,
                    "space_id": _SPACE_ID,
                    "kind": "wiki_page",
                    "title": _page_title(page, slug.rsplit("/", 1)[-1]),
                    "description": "Local published Wiki snapshot page",
                    "mime_type": "text/markdown",
                    "source_type": "local-published-wiki",
                    "source_uri": f"knowledge://spaces/{_SPACE_ID}/assets/{asset_id}",
                    "revision": content_digest,
                    "content_digest": content_digest,
                }
            )
            asset_files[asset_id] = page
            revision_records.append((slug, content_digest, size))
        revision = _catalog_revision(revision_records)
        build = KnowledgePackageBuilder().build(
            output_dir=package_dir,
            package_id="package_local_wiki",
            version="local-snapshot-v1",
            spaces=[{"id": _SPACE_ID, "name": "Local Wiki", "description": "Local published Wiki snapshot"}],
            collections=[
                {
                    "id": _DATASET_ID,
                    "space_id": _SPACE_ID,
                    "name": "Local Wiki Collection",
                    "version": "snapshot-v1",
                    "kind": "wiki",
                    "asset_ids": [item["id"] for item in assets],
                    "capabilities": ["knowledge_list", "knowledge_search", "knowledge_read", "knowledge_query", "wiki_query"],
                }
            ],
            assets=assets,
            asset_files=asset_files,
            capabilities=["knowledge_list", "knowledge_search", "knowledge_read", "knowledge_query", "wiki_query"],
            catalog_revision=revision,
        )
        validation = validate_package(build.package_root)
        result["status"] = "PHASE9_LOCAL_WIKI_PACKAGE_SHADOW_PASS_NOT_ACTIVATABLE"
        result["package"] = {
            "created": True,
            "asset_count": validation.asset_count,
            "package_revision": validation.package_revision,
            "file_count": validation.file_count,
        }
        if package_zip is not None:
            package_zip = _stable_output_path(package_zip)
            with tempfile.TemporaryDirectory(prefix="phase9-local-wiki-package-import-") as import_dir:
                zip_result = export_package_zip(build.package_root, package_zip)
                imported = import_package_zip(zip_result, Path(import_dir).resolve() / "package")
            result["zip"] = {
                "created": True,
                "imported": True,
                "package_revision": imported.package_revision,
                "asset_count": imported.asset_count,
                "file_count": imported.file_count,
            }
        if workspace_dir is not None:
            workspace = WorkspaceMaterializer().materialize(
                package_root=build.package_root,
                workspace_root=_stable_output_path(workspace_dir),
            )
            result["workspace"] = {
                "created": True,
                "package_revision": workspace.package_revision,
                "file_count": workspace.file_count,
            }
    except (OSError, UnicodeError, ValueError, PackageBuildError) as error:
        result["error"] = str(error)
    return _write_report(report_path, result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--package-dir", type=Path, default=_DEFAULT_PACKAGE)
    parser.add_argument("--package-zip", type=Path)
    parser.add_argument("--workspace-dir", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run_shadow(
        wiki_root=args.wiki_root,
        package_dir=args.package_dir,
        workspace_dir=args.workspace_dir,
        package_zip=args.package_zip,
        report_path=args.report,
    )
    print(json.dumps({"status": result["status"], "package": result["package"], "zip": result["zip"], "workspace": result["workspace"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
