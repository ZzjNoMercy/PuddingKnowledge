"""Source inventory checks requiring the original PuddingClaw tree.

The inventory intentionally describes the pre-extraction source universe. It
is migration evidence and therefore cannot be asserted against the reduced
Knowledge target tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.migration


def _legacy_root() -> Path:
    value = os.environ.get("PUDDINGKNOWLEDGE_LEGACY_SOURCE", "").strip()
    if not value:
        raise RuntimeError("PUDDINGKNOWLEDGE_LEGACY_SOURCE is required for migration inventory checks")
    return Path(value).expanduser().resolve()


def test_inventory_paths_and_catalog_tables_are_real_current_facts() -> None:
    root = _legacy_root()
    inventory_path = root / "docs" / "knowledge-platform" / "phase-0a-inventory.yaml"
    inventory = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    missing = [
        source_path
        for item in inventory["subdomains"]
        for source_path in item["source_paths"]
        if not (root / source_path).exists()
    ]
    assert not missing, f"Inventory points at missing paths: {missing}"

    model_source = (root / "backend" / "knowledge" / "models.py").read_text(encoding="utf-8")
    current_tables = set()
    for line in model_source.splitlines():
        if "__tablename__" in line and "=" in line:
            current_tables.add(line.split("=", 1)[1].strip().strip("\"'"))
    catalog = yaml.safe_load(
        (root / "docs" / "knowledge-platform" / "catalog-ownership.yaml").read_text(encoding="utf-8")
    )
    listed_tables = {item["table"] for item in catalog["tables"]}
    assert listed_tables <= current_tables


def test_skill_inventory_covers_all_phase_0a_required_skills_and_files() -> None:
    root = _legacy_root()
    inventory_path = root / "docs" / "knowledge-platform" / "skills-inventory.yaml"
    payload = yaml.safe_load(inventory_path.read_text(encoding="utf-8"))
    skills = {item["id"]: item for item in payload["skills"]}
    required = set(payload["coverage"]["required_skill_ids"])
    assert set(skills) == required
    accepted_statuses = {
        "classified",
        "classified_platform_rewrite_available",
        "classified_workspace_boundary_verified",
    }
    for item in skills.values():
        assert (root / item["skill_file"]).is_file()
        for auxiliary_file in item["auxiliary_files"]:
            auxiliary_path = root / auxiliary_file
            assert auxiliary_path.exists(), auxiliary_file
        assert item["target_owner"] and item["migration"] and item["status"] in accepted_statuses
