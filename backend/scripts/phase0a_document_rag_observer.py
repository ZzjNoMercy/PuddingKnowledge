"""Replay the legacy local document-RAG query and citation boundary."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import tools.search_knowledge_tool as search_module
from graph.citations import parse_tool_result
from knowledge.indexer import build_markdown_chunk_manifest
from knowledge.paths import get_knowledge_root
from tools.search_knowledge_tool import LlamaIndexKnowledgeQueryTool

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/document_rag.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/document-rag/v1":
        raise ValueError("document RAG fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("document RAG fixture must be explicitly sanitized")
    scenario = document["scenario"]
    relative_path = Path(str(scenario.get("relative_path") or ""))
    if relative_path.is_absolute() or ".." in relative_path.parts or relative_path.suffix.lower() not in {".md", ".markdown"}:
        raise ValueError("document RAG relative_path must be a safe Markdown path")
    if scenario.get("virtual_path") != f"/knowledge/{relative_path.as_posix()}":
        raise ValueError("document RAG virtual_path must bind to relative_path")
    if not str(scenario.get("content") or "").strip():
        raise ValueError("document RAG content must not be empty")
    return document


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _file_snapshot(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "content_digest": _digest(path.read_bytes()),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]


def _implementation_dependencies() -> list[dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "backend/scripts/phase0a_document_rag_observer.py",
        "backend/knowledge/indexer.py",
        "backend/tools/search_knowledge_tool.py",
        "backend/graph/citations.py",
    )
    return [{"path": path, "content_digest": _digest((repo_root / path).read_bytes())} for path in relative_paths]


class _Node:
    def __init__(self, *, content: str, metadata: dict[str, Any], node_id: str, ref_doc_id: str) -> None:
        self._content = content
        self.metadata = metadata
        self.node_id = node_id
        self.ref_doc_id = ref_doc_id

    def get_content(self) -> str:
        return self._content


class _ScoredNode:
    def __init__(self, node: _Node, score: float) -> None:
        self.node = node
        self.score = score


class _Retriever:
    def __init__(self, hit: _ScoredNode) -> None:
        self.hit = hit

    def retrieve(self, query: str) -> list[_ScoredNode]:
        return [self.hit] if "retention" in query.lower() else []


class _FixedLocalIndex:
    """Fix only the index provider; legacy query/citation formatting stays real."""

    def __init__(self, hit: _ScoredNode) -> None:
        self.hit = hit

    def as_retriever(self, *, similarity_top_k: int) -> _Retriever:
        if similarity_top_k < 1:
            raise AssertionError("legacy query must request a positive top_k")
        return _Retriever(self.hit)


def _stable_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            key: source.get(key)
            for key in ("source_id", "title", "uri", "document_id", "chunk_id", "source_type", "quote", "metadata")
        }
        for source in sources
    ]


def _stable_tool_result(raw_output: str) -> dict[str, Any]:
    answer_context, sources = parse_tool_result(raw_output, "golden-rag-call")
    return {
        "answer_context": answer_context,
        "answer_context_digest": _digest(answer_context.encode("utf-8")),
        "source_count": len(sources),
        "sources": _stable_sources(sources),
    }


def _stable_chunks(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "parser": manifest.get("parser"),
        "chunk_count": manifest.get("chunk_count"),
        "chunks": [
            {
                key: chunk.get(key)
                for key in ("index", "title", "level", "preview", "header_path", "virtual_path", "linked_images")
            }
            for chunk in manifest.get("chunks", [])
        ],
    }


def observe() -> dict[str, Any]:
    fixture = _fixture()
    scenario = fixture["scenario"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-rag-") as directory:
        root = Path(directory)
        previous_root = os.environ.get("PUDDINGCLAW_KNOWLEDGE_DIR")
        previous_rag_config = search_module.get_rag_config
        previous_multimodal_config = search_module.get_knowledge_multimodal_index_config
        os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = str(root / "knowledge")
        search_module.get_rag_config = lambda: {"top_k": int(scenario["top_k"])}
        search_module.get_knowledge_multimodal_index_config = lambda: {"enabled": False, "vector_store": "local"}
        try:
            knowledge_root = get_knowledge_root(Path(__file__).resolve().parents[2])
            document_path = knowledge_root / str(scenario["relative_path"])
            document_path.parent.mkdir(parents=True, exist_ok=True)
            document_path.write_text(str(scenario["content"]), encoding="utf-8")
            files_before = _file_snapshot(root)
            chunks = build_markdown_chunk_manifest(knowledge_root, [document_path])
            hit_content = "## Retention Policy\n\nThe retention policy keeps published evidence for 30 days."
            node = _Node(
                content=hit_content,
                metadata={
                    "file_path": str(scenario["virtual_path"]),
                    "file_name": Path(str(scenario["relative_path"])).name,
                    "virtual_path": str(scenario["virtual_path"]),
                    "document_id": str(scenario["document_id"]),
                    "chunk_id": str(scenario["chunk_id"]),
                    "header_path": "Operating Policy > Retention Policy",
                    "modality": "text",
                },
                node_id=str(scenario["chunk_id"]),
                ref_doc_id=str(scenario["document_id"]),
            )
            tool = LlamaIndexKnowledgeQueryTool(base_dir=str(Path(__file__).resolve().parents[2]))
            tool._index = _FixedLocalIndex(_ScoredNode(node, 0.91))
            query_result = _stable_tool_result(tool._run(str(scenario["query"])))
            missing_result = _stable_tool_result(tool._run(str(scenario["missing_query"])))
            files_after = _file_snapshot(root)
        finally:
            search_module.get_rag_config = previous_rag_config
            search_module.get_knowledge_multimodal_index_config = previous_multimodal_config
            if previous_root is None:
                os.environ.pop("PUDDINGCLAW_KNOWLEDGE_DIR", None)
            else:
                os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = previous_root

    if query_result["source_count"] != 1 or "retention policy" not in query_result["answer_context"].lower():
        raise AssertionError("legacy document RAG did not return the expected citation-backed hit")
    if missing_result["source_count"] != 0 or missing_result["answer_context"] != "未找到相关内容。":
        raise AssertionError("legacy document RAG no-match semantics are not stable")
    if files_before != files_after:
        raise AssertionError("document RAG query modified the knowledge root")
    return {
        "result": {
            "chunks": _stable_chunks(chunks),
            "query": query_result,
            "missing_query": missing_result,
        },
        "evidence": {
            "provider_boundary": "fixed_in_memory_local_retriever",
            "provider_revision": "legacy-local-retriever-v1",
            "virtual_path": scenario["virtual_path"],
            "citation_source_type": query_result["sources"][0]["source_type"],
            "citation_uri": query_result["sources"][0]["uri"],
            "raw_embeddings_recorded": False,
            "filesystem_unchanged": files_before == files_after,
            "implementation_dependencies": _implementation_dependencies(),
        },
        "database_side_effects": {"business_rows_written": 0, "index_database_used": False},
        "filesystem_side_effects": {
            "business_writes": [],
            "before": files_before,
            "after": files_after,
            "unchanged": files_before == files_after,
        },
        "provider_revision": "legacy-document-rag-query-v1",
        "failure_semantics": "no_match->empty_sources; valid_query->citation_envelope",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/document_rag.json"]},
    }


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
