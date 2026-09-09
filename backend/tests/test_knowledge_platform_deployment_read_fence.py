from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from knowledge_contracts import Correlation, Principal, QueryResult
from knowledge_platform.catalog.deployment import DeploymentActivationController, DeploymentArtifact, DeploymentManifest
from knowledge_platform.catalog.deployment_sqlite import SqliteDeploymentActivationStore
from knowledge_platform.transport.query_adapters import McpQueryAdapter, RestQueryAdapter


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _manifest(revision: str) -> DeploymentManifest:
    return DeploymentManifest(
        revision,
        tuple(
            DeploymentArtifact(kind, revision, f"local-{kind}", _digest(f"{revision}:{kind}"))
            for kind in ("catalog", "blob", "vector_index", "wiki_root")
        ),
    )


def _activated_controller(tmp_path: Path) -> DeploymentActivationController:
    controller = DeploymentActivationController(store=SqliteDeploymentActivationStore(tmp_path / "activation.sqlite3"))
    legacy = _manifest("legacy-v1")
    candidate = _manifest("platform-v1")
    controller.prepare(installation_id="install-1", legacy_manifest=legacy, candidate_manifest=candidate)
    controller.mark_drained(proof_digest=_digest("drain"))
    controller.verify(
        legacy_before_digest=legacy.manifest_digest(),
        legacy_after_digest=legacy.manifest_digest(),
        candidate_manifest_digest=candidate.manifest_digest(),
        checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
    )
    controller.activate(deployment_revision="platform-v1")
    return controller


class _SwitchingDocument:
    def __init__(self, controller: DeploymentActivationController) -> None:
        self._controller = controller

    async def query(self, **kwargs):
        del kwargs
        self._controller.rollback(reason="local read-fence rehearsal")
        return QueryResult(status="ok", trace_id="trace-fence", answer="should be fenced")


def _rest(controller: DeploymentActivationController) -> RestQueryAdapter:
    return RestQueryAdapter(
        catalog=None,
        search=None,
        asset_read=None,
        document=_SwitchingDocument(controller),
        wiki=None,
        deployment=controller,
    )


@pytest.mark.asyncio
async def test_rest_read_is_rejected_when_active_pointer_changes_mid_request(tmp_path: Path) -> None:
    controller = _activated_controller(tmp_path)
    result = await _rest(controller).handle(
        method="POST",
        path="/v1/document-rag/query",
        principal=Principal("fence-test"),
        correlation=Correlation("trace-fence"),
        body={"query": "alpha"},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "stale_deployment_revision"
    assert result["error"]["retryable"] is True
    assert "should be fenced" not in str(result)


@pytest.mark.asyncio
async def test_mcp_uses_the_same_read_fence_and_pending_pointer_fails_closed(tmp_path: Path) -> None:
    controller = _activated_controller(tmp_path)
    result = await McpQueryAdapter(_rest(controller)).call_tool(
        name="document_rag_query",
        arguments={"query": "alpha"},
        principal=Principal("fence-test"),
        correlation=Correlation("trace-fence"),
    )
    assert result["structuredContent"]["error"]["code"] == "stale_deployment_revision"
    assert result["structuredContent"]["error"]["retryable"] is True

    pending = DeploymentActivationController(
        store=SqliteDeploymentActivationStore(tmp_path / "pending.sqlite3")
    )
    unavailable = await RestQueryAdapter(
        catalog=None,
        search=None,
        asset_read=None,
        document=_SwitchingDocument(pending),
        wiki=None,
        deployment=pending,
    ).handle(
        method="POST",
        path="/v1/document-rag/query",
        principal=Principal("fence-test"),
        correlation=Correlation("trace-fence"),
        body={"query": "alpha"},
    )
    assert unavailable["error"]["code"] == "capability_unavailable"
