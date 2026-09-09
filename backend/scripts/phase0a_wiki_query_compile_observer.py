"""Replay the legacy published-Markdown Wiki query and compile boundary."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import config as config_module
from knowledge.brain_schema import BrainSchemaError, BrainSchemaService
from knowledge.llm_wiki import LlmWikiError, LlmWikiService

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/wiki_query_and_compile.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/wiki-query-compile/v1":
        raise ValueError("Wiki query/compile fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("Wiki query/compile fixture must be explicitly sanitized")
    scenario = document["scenario"]
    if not str(scenario.get("page_slug") or "").startswith("concepts/"):
        raise ValueError("Wiki fixture must use a typed concepts slug")
    if "/" not in str(scenario.get("source_path") or "") or not str(scenario["raw_content"]).strip():
        raise ValueError("Wiki fixture source/content is invalid")
    return document


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _implementation_dependencies() -> list[dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "backend/scripts/phase0a_wiki_query_compile_observer.py",
        "backend/knowledge/llm_wiki.py",
        "backend/knowledge/brain_schema.py",
        "backend/tools/llm_wiki_tools.py",
        "backend/knowledge/llm_wiki_compiler_agent.py",
    )
    return [{"path": path, "content_digest": _digest((repo_root / path).read_bytes())} for path in relative_paths]


def _page_content(scenario: dict[str, Any], raw_path: str) -> str:
    return (
        "---\n"
        f"title: {scenario['page_title']}\n"
        f"type: {scenario['page_type']}\n"
        "sources:\n"
        f"  - {raw_path}\n"
        "created: '2026-09-03'\n"
        "updated: '2026-09-03'\n"
        "schema_version: 0.1.0\n"
        "---\n\n"
        f"# {scenario['page_title']}\n\n"
        "Hybrid retrieval combines keyword evidence with semantic evidence.\n"
    )


def _stable_workspace_snapshot(wiki: LlmWikiService) -> dict[str, Any]:
    raw_records = [
        {
            key: record.get(key)
            for key in ("source_id", "asset_id", "title", "source_path", "snapshot_path", "sha256", "size_bytes", "bundle_hash")
        }
        for record in wiki._manifest_records()
    ]
    raw_files = [
        {"path": path.relative_to(wiki.raw_dir).as_posix(), "bytes": path.stat().st_size, "content_digest": _digest(path.read_bytes())}
        for path in sorted(wiki.raw_dir.rglob("*"))
        if path.is_file() and path.name != "manifest.jsonl"
    ]
    wiki_pages = [
        {"path": path.relative_to(wiki.wiki_dir).as_posix(), "bytes": path.stat().st_size, "content_digest": _digest(path.read_bytes())}
        for path in sorted(wiki.wiki_dir.rglob("*.md"))
        if path.name not in {"index.md", "log.md"}
    ]
    return {
        "raw_records": raw_records,
        "raw_files": raw_files,
        "wiki_pages": wiki_pages,
        "index_digest": _digest((wiki.wiki_dir / "index.md").read_bytes()),
    }


def _stable_query(query: dict[str, Any]) -> dict[str, Any]:
    return {
        "question": query.get("question"),
        "pages": [
            {
                "slug": page.get("slug"),
                "score": page.get("score"),
                "content_digest": _digest(str(page.get("content") or "").encode("utf-8")),
            }
            for page in query.get("pages", [])
            if isinstance(page, dict)
        ],
        "references": [
            {
                key: reference.get(key)
                for key in ("slug", "title", "type", "uri", "score", "excerpt", "sources")
            }
            for reference in query.get("references", [])
            if isinstance(reference, dict)
        ],
        "knowledge_gap": bool(query.get("knowledge_gap")),
        "source_policy": query.get("source_policy"),
        "retrieval": query.get("retrieval"),
    }


def _failure_case(wiki: LlmWikiService, raw_path: str, bundle_hash: str) -> dict[str, str]:
    try:
        wiki.publish(
            pages=[{"slug": "concepts/rejected", "content": _page_content(_fixture()["scenario"], raw_path)}],
            expected_bundle_hash="0" * 64,
            summary="rejected fixture publish",
            model="golden-model",
            raw_paths=[raw_path],
        )
    except LlmWikiError as exc:
        return {"status": "rejected", "error": str(exc)}
    raise AssertionError(f"invalid bundle hash was accepted: {bundle_hash}")


def observe() -> dict[str, Any]:
    fixture = _fixture()
    scenario = fixture["scenario"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-wiki-") as directory:
        root = Path(directory)
        previous_root = os.environ.get("PUDDINGCLAW_KNOWLEDGE_DIR")
        previous_retrieval = config_module.get_llm_wiki_retrieval_config
        os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = str(root / "knowledge")
        config_module.get_llm_wiki_retrieval_config = lambda: {"hybrid_enabled": False}
        try:
            schema = BrainSchemaService(Path(__file__).resolve().parents[2])
            try:
                schema.initialize()
            except BrainSchemaError as exc:
                raise RuntimeError(f"Wiki schema catalog unavailable: {exc}") from exc
            wiki = LlmWikiService(Path(__file__).resolve().parents[2])
            bundle = wiki.schema.bundle()
            before = _stable_workspace_snapshot(wiki)
            raw = wiki.snapshot_raw(
                source_id=str(scenario["source_id"]),
                asset_id=str(scenario["asset_id"]),
                title=str(scenario["title"]),
                content=str(scenario["raw_content"]),
                source_path=str(scenario["source_path"]),
            )
            raw_state = _stable_workspace_snapshot(wiki)
            published = wiki.publish(
                pages=[{"slug": str(scenario["page_slug"]), "content": _page_content(scenario, raw["snapshot_path"])}],
                expected_bundle_hash=bundle["bundle_hash"],
                summary="golden Wiki compile",
                model="golden-model",
                raw_paths=[raw["snapshot_path"]],
            )
            after_compile = _stable_workspace_snapshot(wiki)
            query_before = _stable_workspace_snapshot(wiki)
            query = wiki.query(str(scenario["query"]), limit=int(scenario["limit"]))
            missing = wiki.query(str(scenario["missing_query"]), limit=int(scenario["limit"]))
            query_after = _stable_workspace_snapshot(wiki)
            rejection = _failure_case(wiki, raw["snapshot_path"], bundle["bundle_hash"])
            after_failure = _stable_workspace_snapshot(wiki)
        finally:
            config_module.get_llm_wiki_retrieval_config = previous_retrieval
            if previous_root is None:
                os.environ.pop("PUDDINGCLAW_KNOWLEDGE_DIR", None)
            else:
                os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = previous_root

    if not published.get("published") or not query.get("pages"):
        raise AssertionError("Wiki compile/query did not produce a published matching page")
    if not missing.get("knowledge_gap") or missing.get("pages"):
        raise AssertionError(f"Wiki missing-query semantics are not stable: {missing}")
    if query_before != query_after or after_compile != after_failure:
        raise AssertionError("read-only query or rejected publish changed stable Wiki state")
    page_path = f"{scenario['page_slug']}.md"
    return {
        "result": {
            "compile": {
                "published": bool(published["published"]),
                "lint_ok": bool((published.get("lint") or {}).get("ok")),
                "lint_counts": (published.get("lint") or {}).get("counts"),
                "pages": [str(item) for item in published.get("pages", [])],
            },
            "query": _stable_query(query),
            "missing_query": _stable_query(missing),
        },
        "evidence": {
            "schema_bundle_hash": bundle["bundle_hash"],
            "raw_snapshot_path": raw["snapshot_path"],
            "raw_snapshot_digest": raw["sha256"],
            "published_page_path": page_path,
            "source_policy": query["source_policy"],
            "retrieval_mode": query["retrieval"]["mode"],
            "query_state_unchanged": query_before == query_after,
            "rejected_publish_state_unchanged": after_compile == after_failure,
            "failure_checks": {"bundle_mismatch": rejection},
            "implementation_dependencies": _implementation_dependencies(),
        },
        "database_side_effects": {"business_rows_written": 0, "catalog_used": False},
        "filesystem_side_effects": {
            "business_writes": [f"raw/{raw['snapshot_path']}", f"wiki/{page_path}"],
            "before": before,
            "after_raw_snapshot": raw_state,
            "after_compile": after_compile,
            "query_before": query_before,
            "query_after": query_after,
            "after_rejected_publish": after_failure,
        },
        "provider_revision": "legacy-llm-wiki-query-publish-v1",
        "failure_semantics": "missing_query->knowledge_gap; bundle_mismatch->publish_rejected; valid_raw->published_and_queryable",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/wiki_query_and_compile.json"]},
    }


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
