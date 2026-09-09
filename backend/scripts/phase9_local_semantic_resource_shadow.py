"""Replay an active local semantic Markdown definition through MCP resources/read.

The registry is created in an isolated temporary SQLite database from a
digest-only observation of a real local Markdown fixture.  The shadow proves
that only an explicitly active definition with an exact Space scope is
readable; it never writes the canonical Catalog or any Vanna collection.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.semantic import (
    SemanticMarkdownAdminService,
    SemanticMarkdownDefinition,
    SqliteSemanticMarkdownRepository,
)
from scripts.phase8_local_platform_http_shadow import _build_app

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CATALOG = _ROOT / "artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"
_DEFAULT_WIKI_ROOT = Path("/Users/pet/Documents/knowledge/llm-wiki/wiki")
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase9-local-semantic-resource-shadow-report.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _fixture(wiki_root: Path) -> tuple[str, int]:
    candidates = sorted(
        path
        for path in wiki_root.rglob("*.md")
        if path.is_file() and not path.is_symlink() and ".obsidian" not in path.parts
    )
    if not candidates:
        raise FileNotFoundError("local Wiki has no regular Markdown fixture")
    source = candidates[0]
    return _sha256(source), source.stat().st_size


def _read_summary(response: object) -> dict[str, object]:
    payload = response.json()  # type: ignore[attr-defined]
    result = payload.get("result") if isinstance(payload, Mapping) else None
    structured = result.get("structuredContent") if isinstance(result, Mapping) else None
    contents = result.get("contents") if isinstance(result, Mapping) else None
    content = contents[0] if isinstance(contents, list) and contents else None
    provenance = structured.get("provenance") if isinstance(structured, Mapping) else None
    return {
        "http_status": response.status_code,  # type: ignore[attr-defined]
        "status": structured.get("status") if isinstance(structured, Mapping) else "invalid",
        "error_code": (structured.get("error") or {}).get("code")
        if isinstance(structured, Mapping) and isinstance(structured.get("error"), Mapping)
        else None,
        "space_id": provenance.get("space_id") if isinstance(provenance, Mapping) else None,
        "content_present": isinstance(content, Mapping) and bool(content.get("text")),
        "content_mime_type": content.get("mimeType") if isinstance(content, Mapping) else None,
    }


def run_shadow(*, catalog_path: Path, wiki_root: Path, output_path: Path) -> dict[str, object]:
    catalog_path = catalog_path.expanduser().absolute()
    wiki_root = wiki_root.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    canonical_before = _sha256(catalog_path)
    report: dict[str, object] = {
        "format": "agent-knowledge-platform-local-semantic-resource-shadow/v1",
        "status": "PHASE9_LOCAL_SEMANTIC_RESOURCE_SHADOW_FAILED",
        "activation": "not-activated",
        "activation_allowed": False,
        "mode": "active-semantic-markdown-mcp-read-only",
        "catalog": {"canonical_sha256_before": canonical_before, "canonical_unchanged": False},
        "local_fixture": {},
        "registry": {"isolated": True, "active_created": False},
        "resources_list": {},
        "authorized_read": {},
        "wrong_space_read": {},
        "registry_unbound_read": {},
    }

    with tempfile.TemporaryDirectory(prefix="phase9-local-semantic-resource-shadow-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        temporary_catalog = temp_root / "knowledge-platform.sqlite3"
        shutil.copy2(catalog_path, temporary_catalog)
        with sqlite3.connect(temporary_catalog) as connection:
            row = connection.execute("SELECT id FROM knowledge_spaces ORDER BY id LIMIT 1").fetchone()
        if row is None:
            raise RuntimeError("local Catalog has no Knowledge Space")
        space_id = str(row[0])
        source_digest, source_size = _fixture(wiki_root)
        report["local_fixture"] = {"source_digest": source_digest, "source_size": source_size}

        definition = SemanticMarkdownDefinition(
            id="dimension:local-phase9",
            space_id=space_id,
            semantic_type="dimension",
            name="Local phase 9 dimension",
            description="A local shadow definition derived from a Markdown fixture.",
            tags=("local", "shadow"),
            frontmatter={"source_digest": source_digest, "source_size": source_size},
            body=f"# Local semantic shadow\n\nLocal fixture digest: {source_digest}\n",
        )
        repository = SqliteSemanticMarkdownRepository(temp_root / "semantic-markdown.sqlite3")
        service = SemanticMarkdownAdminService(repository=repository)
        principal = Principal(
            "phase9-local-semantic-resource-shadow",
            scopes=("knowledge.read", "knowledge.admin", f"knowledge.space:{space_id}"),
        )
        prepared = service.prepare(principal=principal, correlation=Correlation("phase9-semantic-prepare"), definition=definition)
        if prepared.status != "ok":
            raise RuntimeError("semantic Markdown prepare failed")
        decided = service.decide(
            principal=principal,
            correlation=Correlation("phase9-semantic-confirm"),
            asset_id=definition.id,
            space_id=space_id,
            decision="confirm",
            expected_status="waiting_for_confirmation",
        )
        if decided.status != "ok":
            raise RuntimeError("semantic Markdown activation failed")
        report["registry"] = {"isolated": True, "active_created": len(service.active(space_id=space_id)) == 1}

        repository_catalog = SqliteCatalogQueryRepository(temporary_catalog)
        uri = f"knowledge://spaces/{space_id}/semantics/dimension-local-phase9"
        app = _build_app(repository_catalog, {}, principal, semantic_markdown=service)
        with TestClient(app) as client:
            listed = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": "phase9-semantic-list", "method": "resources/list", "params": {}},
            ).json()
            result = listed.get("result") if isinstance(listed, Mapping) else None
            listed_templates = result.get("resourceTemplates", []) if isinstance(result, Mapping) else []
            report["resources_list"] = {
                "semantic_template_advertised": any(
                    isinstance(item, Mapping) and "/semantics/" in str(item.get("uriTemplate") or "")
                    for item in listed_templates
                )
            }
            report["authorized_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-semantic-authorized",
                        "method": "resources/read",
                        "params": {"uri": uri, "start": 0, "end": 4096},
                    },
                )
            )

        wrong_principal = Principal(
            "phase9-local-semantic-resource-shadow-wrong-space",
            scopes=("knowledge.read", "knowledge.space:space_not_allowed"),
        )
        wrong_app = _build_app(repository_catalog, {}, wrong_principal, semantic_markdown=service)
        with TestClient(wrong_app) as client:
            report["wrong_space_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-semantic-wrong-space",
                        "method": "resources/read",
                        "params": {"uri": uri, "start": 0, "end": 4096},
                    },
                )
            )

        unbound_app = _build_app(repository_catalog, {}, principal)
        with TestClient(unbound_app) as client:
            report["registry_unbound_read"] = _read_summary(
                client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-semantic-unbound",
                        "method": "resources/read",
                        "params": {"uri": uri, "start": 0, "end": 4096},
                    },
                )
            )

    canonical_after = _sha256(catalog_path)
    catalog_report = report["catalog"]
    assert isinstance(catalog_report, dict)
    catalog_report.update({"canonical_sha256_after": canonical_after, "canonical_unchanged": canonical_before == canonical_after})
    authorized = report["authorized_read"]
    wrong = report["wrong_space_read"]
    unbound = report["registry_unbound_read"]
    if (
        catalog_report["canonical_unchanged"]
        and isinstance(report["registry"], Mapping) and report["registry"].get("active_created") is True
        and isinstance(report["resources_list"], Mapping) and report["resources_list"].get("semantic_template_advertised") is True
        and isinstance(authorized, Mapping) and authorized.get("status") == "ok" and authorized.get("content_present") is True
        and authorized.get("space_id") == space_id and authorized.get("content_mime_type") == "text/markdown"
        and isinstance(wrong, Mapping) and wrong.get("error_code") == "permission_denied"
        and isinstance(unbound, Mapping) and unbound.get("error_code") == "binding_unavailable"
    ):
        report["status"] = "PHASE9_LOCAL_SEMANTIC_RESOURCE_SHADOW_PASS_NOT_ACTIVATABLE"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = run_shadow(catalog_path=args.catalog, wiki_root=args.wiki_root, output_path=args.output)
    print(json.dumps({"status": report["status"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if str(report["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
