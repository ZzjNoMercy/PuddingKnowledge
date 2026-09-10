from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_platform.local.package_tables import PackageTableCatalog, PackageTableProvider


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


class _Publisher:
    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects

    def read_published(self, asset_id: str) -> bytes:
        return self.objects[asset_id]


class _Repository:
    catalog_revision = "sha256:" + "a" * 64

    def __init__(self, content: bytes):
        self.content = content
        digest = _digest(content)
        self.asset = {
            "id": "sales",
            "space_id": "space_1",
            "kind": "table",
            "title": "Sales",
            "mime_type": "text/csv",
            "source_type": "package",
            "source_uri": "knowledge://spaces/space_1/assets/sales",
            "content_digest": digest,
            "metadata": {"package_revision": "sha256:" + "b" * 64, "package_path": "assets/originals/sales.csv", "sheet_name": None},
        }
        self.structured = {
            "id": "sales",
            "space_id": "space_1",
            "kind": "structured_asset",
            "source_type": "package",
            "source_uri": "knowledge://spaces/space_1/structured-assets/sales/source",
            "content_digest": digest,
            "sheet_name": None,
            "capabilities": ["table_query"],
        }

    def get_asset(self, *, asset_id: str):
        return self.asset if asset_id == "sales" else None

    def get_structured_asset(self, *, asset_id: str):
        return self.structured if asset_id == "sales" else None

    def list_structured_assets(self, *, space_id: str | None = None):
        return [self.structured] if space_id in (None, "space_1") else []


@pytest.mark.asyncio
async def test_package_provider_reads_published_csv_and_returns_contract_payload() -> None:
    content = b"brand,sales\nPudding,12\nClaw,8\n"
    repository = _Repository(content)
    provider = PackageTableProvider(_Publisher({"sales": content}), repository)

    payloads = await provider.query(
        query="sales",
        asset_id="sales",
        space_id="space_1",
        limit=5,
        semantic_context=None,
    )

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload.asset_id == "sales"
    assert payload.resource_uri == "knowledge://spaces/space_1/structured-assets/sales/source"
    assert payload.content_digest == _digest(content)
    assert payload.columns == ("brand", "sales")
    assert payload.row_count == 2


@pytest.mark.asyncio
async def test_package_provider_preserves_published_excel_sheet_binding(tmp_path: Path) -> None:
    from openpyxl import Workbook

    source = tmp_path / "sales.xlsx"
    workbook = Workbook()
    workbook.active.title = "Jan"
    workbook.active.append(["brand", "sales"])
    workbook.active.append(["Pudding", 12])
    workbook.create_sheet("Feb").append(["brand", "sales"])
    workbook.save(source)
    content = source.read_bytes()
    repository = _Repository(content)
    repository.asset.update({"mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
    repository.asset["metadata"]["sheet_name"] = "Jan"
    repository.structured["sheet_name"] = "Jan"
    provider = PackageTableProvider(_Publisher({"sales": content}), repository)

    payloads = await provider.query(query="Pudding", asset_id="sales", space_id="space_1", limit=5, semantic_context=None)

    assert payloads[0].preview_rows[0]["brand"] == "Pudding"


@pytest.mark.asyncio
async def test_package_provider_rejects_published_digest_drift() -> None:
    content = b"brand,sales\nPudding,12\n"
    repository = _Repository(content)
    provider = PackageTableProvider(_Publisher({"sales": content + b"tampered"}), repository)

    with pytest.raises(RuntimeError, match="digest mismatch"):
        await provider.query(query="sales", asset_id="sales", space_id="space_1", limit=5, semantic_context=None)


def test_package_catalog_excludes_nonpackage_and_keeps_catalog_revision() -> None:
    content = b"brand,sales\nPudding,12\n"
    repository = _Repository(content)
    repository.asset["source_type"] = "local-file"
    catalog = PackageTableCatalog(repository, _Publisher({"sales": content}))

    assert catalog.catalog_revision == repository.catalog_revision
    assert catalog.get_structured_asset(asset_id="sales") is None
