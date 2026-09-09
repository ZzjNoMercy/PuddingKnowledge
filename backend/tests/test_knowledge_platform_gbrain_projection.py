from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from knowledge_platform.gbrain import GbrainProjectionRequest, LocalGbrainProjectionService


def _request(markdown: str = "# Local Wiki\n\nbody") -> GbrainProjectionRequest:
    digest = "sha256:" + hashlib.sha256(markdown.encode()).hexdigest()
    return GbrainProjectionRequest(
        space_id="space_local",
        asset_id="asset_wiki",
        source_uri="knowledge://spaces/space_local/assets/asset_wiki",
        published_uri="knowledge://spaces/space_local/wiki/asset_wiki",
        source_revision=digest,
        published_digest=digest,
        published_markdown=markdown,
        idempotency_key="projection-local-1",
    )


def test_local_projection_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    service = LocalGbrainProjectionService(root=tmp_path / "gbrain")
    request = _request()
    first = service.project(request)
    second = service.project(request)

    assert first == second
    assert service.projection_count == 1
    assert first.projection_uri.startswith("knowledge://spaces/space_local/gbrain/")
    assert list((tmp_path / "gbrain" / "sources" / "space_local").glob("*.md"))[0].read_text() == request.published_markdown
    assert len((tmp_path / "gbrain" / "projection-manifest.jsonl").read_text().splitlines()) == 1


def test_projection_rejects_identity_digest_and_schema_pack_mismatch() -> None:
    with pytest.raises(ValueError, match="identity"):
        GbrainProjectionRequest(
            **asdict(replace(_request(), published_uri="knowledge://spaces/space_other/wiki/asset_wiki"))
        )
    with pytest.raises(ValueError, match="digest"):
        GbrainProjectionRequest(
            space_id="space_local",
            asset_id="asset_wiki",
            source_uri="knowledge://spaces/space_local/assets/asset_wiki",
            published_uri="knowledge://spaces/space_local/wiki/asset_wiki",
            source_revision="rev-1",
            published_digest="sha256:" + "0" * 64,
            published_markdown="# local",
            idempotency_key="projection-local-1",
        )


def test_projection_rejects_manifest_and_destination_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "gbrain-manifest"
    service = LocalGbrainProjectionService(root=root)
    request = _request()
    root.mkdir()
    (root / "projection-manifest.jsonl").symlink_to(tmp_path / "manifest-target")
    with pytest.raises(OSError, match="manifest"):
        service.project(request)

    destination_root = tmp_path / "gbrain-destination"
    destination_service = LocalGbrainProjectionService(root=destination_root)
    sources = destination_root / "sources" / "space_local"
    sources.mkdir(parents=True)
    digest = hashlib.sha256(request.published_markdown.encode()).hexdigest()
    (sources / f"{request.asset_id}-{digest[:16]}.md").symlink_to(tmp_path / "source-target")
    with pytest.raises(OSError, match="destination"):
        destination_service.project(request)

    directory_root = tmp_path / "gbrain-directory"
    directory_root.mkdir()
    (directory_root / "sources").symlink_to(tmp_path / "outside-directory", target_is_directory=True)
    with pytest.raises(OSError, match="directory"):
        LocalGbrainProjectionService(root=directory_root).project(request)


def test_concurrent_first_projection_is_idempotent(tmp_path: Path) -> None:
    request = _request()

    def project() -> object:
        return LocalGbrainProjectionService(root=tmp_path / "gbrain").project(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: project(), range(2)))

    assert results[0] == results[1]
    assert len((tmp_path / "gbrain" / "projection-manifest.jsonl").read_text().splitlines()) == 1


def test_projection_does_not_require_gbrain_binary_or_legacy_runtime(tmp_path: Path) -> None:
    result = LocalGbrainProjectionService(root=tmp_path / "gbrain").project(_request())
    assert result.schema_pack == "puddingclaw-wiki"
    assert result.bytes > 0
    raw = (tmp_path / "gbrain" / "projection-manifest.jsonl").read_text()
    assert "gbrain-schema-pack-v1" in raw
    assert "GBRAIN_HOME" not in raw
