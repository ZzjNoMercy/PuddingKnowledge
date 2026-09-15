"""Create a new local install tree, containing only Platform-owned packages."""
from pathlib import Path
import argparse
import shutil
import hashlib
import json
import tomllib


def stage(output: Path) -> None:
    package = Path(__file__).resolve().parent
    repo = package.parents[1]
    output = output.expanduser().absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("staging path contains a symlink")
    if output.exists():
        raise FileExistsError("staging output must be new")
    metadata = repo / "backend"
    sources = [repo / "backend" / name for name in ("knowledge_platform", "knowledge_contracts")]
    if any(output.is_relative_to(source) for source in sources):
        raise ValueError("staging output must be outside source trees")
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        if (metadata / name).is_symlink() or not (metadata / name).is_file():
            raise ValueError("build metadata must be a regular file")
    # Validate before writing; never follow source symlinks into host data.
    for source in sources:
        if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("source tree contains a symlink")
    output.mkdir()
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        shutil.copyfile(metadata / name, output / name)
    for source in sources:
        shutil.copytree(source, output / source.name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    files = {str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(output.rglob("*")) if p.is_file()}
    version = tomllib.loads((output / "pyproject.toml").read_text())["project"]["version"]
    (output / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "release_version": version,
        "owner": "puddingknowledge", "runtime_kind": "local-catalog-wiki",
        "production_activation_allowed": False, "files": files,
    }, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    stage(parser.parse_args().output)
