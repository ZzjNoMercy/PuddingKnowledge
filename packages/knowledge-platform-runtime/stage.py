"""Create a new local install tree, containing only Platform-owned packages."""
from pathlib import Path
import argparse
import shutil


def stage(output: Path) -> None:
    package = Path(__file__).resolve().parent
    repo = package.parents[1]
    output = output.expanduser().absolute()
    if any(p.is_symlink() for p in (output, *output.parents)):
        raise ValueError("staging path contains a symlink")
    if output.exists():
        raise FileExistsError("staging output must be new")
    sources = [repo / "backend" / name for name in ("knowledge_platform", "knowledge_contracts")]
    if any(output.is_relative_to(source) for source in sources):
        raise ValueError("staging output must be outside source trees")
    for name in ("pyproject.toml", "uv.lock"):
        if (package / name).is_symlink() or not (package / name).is_file():
            raise ValueError("build metadata must be a regular file")
    # Validate before writing; never follow source symlinks into host data.
    for source in sources:
        if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
            raise ValueError("source tree contains a symlink")
    output.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(package / name, output / name)
    for source in sources:
        shutil.copytree(source, output / "src" / source.name,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    stage(parser.parse_args().output)
