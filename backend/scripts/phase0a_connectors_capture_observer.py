"""Replay the legacy connector, OAuth, incremental-sync, and web-capture boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from knowledge.connectors.feishu import (
    OAUTH_TOKEN_PATH,
    USER_INFO_PATH,
    FeishuConnectorError,
    FeishuHttpClient,
    cleanup_oauth_credentials,
    complete_user_oauth,
    normalize_api_base,
    start_user_oauth,
)
from knowledge.connectors.feishu_sync import process_feishu_sync_run
from knowledge.models import (
    Base,
    FeishuAppCredential,
    FeishuOAuthSession,
    FeishuUserGrant,
    KnowledgeDocument,
    KnowledgeSourceConnection,
    KnowledgeSourceItem,
    KnowledgeSyncRun,
    ReadLaterItem,
)
from knowledge.read_later import (
    create_read_later_item,
    process_read_later_capture_job,
    retry_read_later_item,
)
from knowledge.sources import create_source_connection, create_sync_run
from provider_registry import LocalCredentialStore
from tools.fetch_url_tool import FetchURLTool, _FetchedResponse

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/connectors_and_capture.json"
_RUNTIME_ID_RE = re.compile(r"(?:kb|src|sitem|doc|job|sync|later|evt|fapp|foauth|fgrant)_[0-9a-f]{12,}")


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/connectors-and-capture/v1":
        raise ValueError("connector/capture fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("connector/capture fixture must be explicitly sanitized")
    scenario = document["scenario"]
    web = scenario.get("web_capture")
    oauth = scenario.get("oauth")
    sync = scenario.get("feishu_sync")
    if not isinstance(web, dict) or not str(web.get("original_url") or "").startswith("https://"):
        raise ValueError("connector/capture fixture must provide an HTTPS web URL")
    if not str(web.get("html") or "").strip():
        raise ValueError("connector/capture fixture HTML must not be empty")
    if not isinstance(oauth, dict) or not isinstance(sync, dict):
        raise ValueError("connector/capture fixture must provide OAuth and sync scenarios")
    if not str(oauth.get("app_secret") or "").startswith("sanitized-"):
        raise ValueError("connector/capture fixture app secret must be a sanitized placeholder")
    return document


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _canonical_file_bytes(path: Path) -> bytes:
    """Remove intentionally volatile capture timestamps before hashing an artifact."""

    text = path.read_text(encoding="utf-8")
    stable_lines = [
        line
        for line in text.splitlines()
        if not line.startswith(("captured_at:", "synced_at:"))
    ]
    return ("\n".join(stable_lines) + ("\n" if text.endswith("\n") else "")).encode("utf-8")


def _stable_path(value: str) -> str:
    value = re.sub(r"(?:^|/)20\d{6}(?=/|$)", "/<capture-date>", value)
    return _RUNTIME_ID_RE.sub("<runtime-id>", value)


def _file_snapshot(root: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": _stable_path(path.relative_to(root).as_posix()),
            "bytes": path.stat().st_size,
            "content_digest": _digest(_canonical_file_bytes(path)),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt"}
    ]


def _implementation_dependencies() -> list[dict[str, str]]:
    repo_root = Path(__file__).resolve().parents[2]
    relative_paths = (
        "backend/scripts/phase0a_connectors_capture_observer.py",
        "backend/knowledge/read_later.py",
        "backend/knowledge/connectors/feishu.py",
        "backend/knowledge/connectors/feishu_sync.py",
        "backend/knowledge/connectors/feishu_blocks.py",
        "backend/knowledge/sources.py",
    )
    return [{"path": path, "content_digest": _digest((repo_root / path).read_bytes())} for path in relative_paths]


def _stable_document(document: KnowledgeDocument, *, root: Path) -> dict[str, Any]:
    return {
        "title": document.title,
        "source_type": document.source_type,
        "virtual_path": _stable_path(document.virtual_path),
        "status": document.status,
        "source_revision": document.source_revision,
        "canonical_content_digest": _digest(_canonical_file_bytes(Path(document.storage_path))),
        "storage_exists": Path(document.storage_path).is_file(),
        "storage_under_knowledge_root": Path(document.storage_path).resolve().is_relative_to(root.resolve()),
    }


class _FakeFeishuApi:
    def __init__(self, scenario: dict[str, Any]) -> None:
        self.scenario = scenario
        self.block_calls = 0

    async def list_nodes(self, _session, _source, *, space_id: str, parent_node_token: str | None = None):
        if space_id != self.scenario["space_id"] or parent_node_token is not None:
            raise AssertionError("sync must stay within the selected Feishu space root")
        return [
            {
                "space_id": space_id,
                "node_token": self.scenario["node_token"],
                "obj_token": self.scenario["document_token"],
                "obj_type": "docx",
                "title": self.scenario["title"],
                "has_child": False,
                "obj_edit_time": "1700000000",
                "url": self.scenario["source_url"],
            }
        ]

    async def get_docx_document(self, _session, _source, *, document_id: str):
        if document_id != self.scenario["document_token"]:
            raise AssertionError("sync requested an unbound document")
        return {"title": self.scenario["title"], "revision_id": self.scenario["revision"]}

    async def list_docx_blocks(self, _session, _source, *, document_id: str, document_revision_id: int):
        if document_id != self.scenario["document_token"] or document_revision_id != int(self.scenario["revision"]):
            raise AssertionError("sync requested the wrong document revision")
        self.block_calls += 1
        return [
            {"block_id": "page", "block_type": 1, "children": ["heading", "body"]},
            {
                "block_id": "heading",
                "block_type": 3,
                "heading1": {"elements": [{"text_run": {"content": self.scenario["title"]}}]},
            },
            {
                "block_id": "body",
                "block_type": 2,
                "text": {"elements": [{"text_run": {"content": self.scenario["body"]}}]},
            },
        ]

    async def get_doc_meta(self, _session, _source, *, doc_token: str, doc_type: str = "docx"):
        if doc_token != self.scenario["document_token"] or doc_type != "docx":
            raise AssertionError("sync requested the wrong document metadata")
        return {}

    async def get_user_display_names(self, _session, _source, *, open_ids: list[str]):
        if open_ids:
            raise AssertionError("fixture does not authorize arbitrary user identity reads")
        return {}


def _stable_rows(session_rows: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "title": row.title,
            "source_type": getattr(row, "source_type", None),
            "status": row.status,
            "revision": getattr(row, "revision", None),
        }
        for row in session_rows
    ]


async def _observe_async(root: Path, fixture: dict[str, Any]) -> dict[str, Any]:
    scenario = fixture["scenario"]
    web = scenario["web_capture"]
    oauth = scenario["oauth"]
    sync_scenario = scenario["feishu_sync"]
    database_path = root / "connector-capture.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    original_request_once = FetchURLTool._request_once
    os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = str(root / "knowledge")
    files_before: list[dict[str, Any]] = []
    credential_store = LocalCredentialStore()
    app_secret_ref = credential_store.put("golden-feishu-app", json.dumps({"app_id": oauth["app_id"], "app_secret": oauth["app_secret"]}))

    async def oauth_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == OAUTH_TOKEN_PATH:
            payload = json.loads(request.content.decode("utf-8"))
            if payload.get("grant_type") != "authorization_code" or payload.get("code") != oauth["code"]:
                raise AssertionError("OAuth token exchange did not use the fixture code")
            if not payload.get("code_verifier"):
                raise AssertionError("OAuth token exchange omitted PKCE verifier")
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "access_token": "sanitized-user-access",
                    "refresh_token": "sanitized-user-refresh",
                    "expires_in": 3600,
                    "refresh_token_expires_in": 7200,
                    "scope": "wiki:wiki:readonly offline_access",
                },
            )
        if request.url.path == USER_INFO_PATH:
            if request.headers.get("authorization") != "Bearer sanitized-user-access":
                raise AssertionError("OAuth user info request omitted the exchanged access token")
            return httpx.Response(200, json={"code": 0, "data": {"open_id": "ou_golden", "tenant_key": "tenant_golden"}})
        raise AssertionError(f"unexpected OAuth path: {request.url.path}")

    FetchURLTool._request_once = classmethod(
        lambda _cls, url: _FetchedResponse(
            200,
            {"content-type": "text/html; charset=utf-8"},
            str(web["html"]).encode("utf-8"),
        )
    )
    try:
        async with sessions() as session:
            app = FeishuAppCredential(
                id="fapp-golden",
                app_id_masked="cli_••••nectors",
                credential_ref=app_secret_ref,
                api_base_url="https://open.feishu.cn",
            )
            session.add(app)
            source = await create_source_connection(
                session,
                connector_key="feishu_wiki",
                name="Golden Feishu Source",
                auth_type="user",
                config={"space_id": sync_scenario["space_id"], "publish_vector": False},
            )
            started = await start_user_oauth(
                session,
                app=app,
                source=source,
                redirect_uri=oauth["redirect_uri"],
                scopes=list(oauth["scopes"]),
                principal_id=oauth["principal_id"],
            )
            await session.commit()
            query = parse_qs(urlparse(started["authorization_url"]).query)
            state = query["state"][0]
            oauth_client = FeishuHttpClient(transport=httpx.MockTransport(oauth_handler))
            try:
                await complete_user_oauth(
                    session,
                    state=state,
                    code=oauth["code"],
                    expected_principal_id="browser:wrong-binding",
                    http_client=oauth_client,
                )
            except FeishuConnectorError as exc:
                wrong_principal_rejected = "浏览器会话" in str(exc)
            else:
                wrong_principal_rejected = False
            grant, bound_source, cleanup_refs = await complete_user_oauth(
                session,
                state=state,
                code=oauth["code"],
                expected_principal_id=oauth["principal_id"],
                http_client=oauth_client,
            )
            await session.commit()
            cleanup_oauth_credentials(cleanup_refs)
            oauth_session = (await session.execute(select(FeishuOAuthSession))).scalar_one()

            web_item, web_job, deduplicated = await create_read_later_item(
                session,
                base_dir=root,
                url=str(web["original_url"]),
            )
            if deduplicated or web_job is None:
                raise AssertionError("web capture fixture unexpectedly deduplicated or omitted its job")
            first_capture = await process_read_later_capture_job(session, base_dir=root, job=web_job)
            first_item = await session.get(ReadLaterItem, web_item.id)
            if first_item is None or first_item.document_id is None:
                raise AssertionError("web capture did not materialize a document")
            first_document = await session.get(KnowledgeDocument, first_item.document_id)
            if first_document is None:
                raise AssertionError("web capture document disappeared before retry")
            retry_job = await retry_read_later_item(session, item=first_item)
            retry_capture = await process_read_later_capture_job(session, base_dir=root, job=retry_job)
            retried_item = await session.get(ReadLaterItem, web_item.id)
            if retried_item is None or retried_item.document_id is None:
                raise AssertionError("web capture retry removed its document identity")
            retried_document = await session.get(KnowledgeDocument, retried_item.document_id)
            if retried_document is None:
                raise AssertionError("web capture retry document missing")

            api = _FakeFeishuApi(sync_scenario)
            first_sync = await create_sync_run(session, source=bound_source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=root, run=first_sync, api=api)
            second_sync = await create_sync_run(session, source=bound_source, mode="incremental")
            await session.commit()
            await process_feishu_sync_run(session, base_dir=root, run=second_sync, api=api)

            sources = list((await session.execute(select(KnowledgeSourceConnection).order_by(KnowledgeSourceConnection.name))).scalars())
            items = list((await session.execute(select(KnowledgeSourceItem).order_by(KnowledgeSourceItem.title))).scalars())
            documents = list((await session.execute(select(KnowledgeDocument).order_by(KnowledgeDocument.title))).scalars())
            sync_runs = list((await session.execute(select(KnowledgeSyncRun).order_by(KnowledgeSyncRun.mode, KnowledgeSyncRun.created_at))).scalars())
            grants = list((await session.execute(select(FeishuUserGrant))).scalars())
            result = {
                "oauth": {
                    "auth_host": urlparse(started["authorization_url"]).netloc,
                    "pkce_method": query["code_challenge_method"][0],
                    "requested_scopes": started["scopes"],
                    "wrong_principal_rejected": wrong_principal_rejected,
                    "session_status": oauth_session.status,
                    "grant_status": grant.status,
                    "granted_scopes": sorted(grant.granted_scopes),
                    "source_status": bound_source.status,
                    "temporary_vault_item_removed": all(not credential_store.get(ref) for ref in cleanup_refs),
                },
                "web_capture": {
                    "canonical_url": retried_item.canonical_url,
                    "parse_status": retried_item.parse_status,
                    "first_job_status": first_capture.status,
                    "retry_job_status": retry_capture.status,
                    "same_document_identity": first_document.id == retried_document.id,
                    "document": _stable_document(retried_document, root=root / "knowledge"),
                },
                "feishu_sync": {
                    "first_status": first_sync.status,
                    "first_stats": {key: first_sync.stats_json.get(key) for key in ("discovered", "changed", "unchanged", "failed")},
                    "second_stats": {key: second_sync.stats_json.get(key) for key in ("discovered", "changed", "unchanged", "failed")},
                    "block_calls": api.block_calls,
                    "item": {
                        "external_id": items[-1].external_id,
                        "status": items[-1].status,
                        "revision": items[-1].revision,
                        "path": items[-1].path_json,
                    },
                    "document": _stable_document(documents[-1], root=root / "knowledge"),
                },
                "source_identity": {
                    "source_count": len(sources),
                    "connector_keys": sorted(source.connector_key for source in sources),
                    "item_count": len(items),
                    "document_count": len(documents),
                    "grant_count": len(grants),
                    "sync_run_count": len(sync_runs),
                    "source_item_identity_scope": "source_connection_id+external_id",
                },
            }
            files_after = _file_snapshot(root / "knowledge") if (root / "knowledge").exists() else []
            implementation_dependencies = _implementation_dependencies()
            try:
                normalize_api_base("https://feishu.cn.attacker.invalid")
            except FeishuConnectorError:
                unsafe_api_base_rejected = True
            else:
                unsafe_api_base_rejected = False
    finally:
        FetchURLTool._request_once = original_request_once
        await engine.dispose()

    if not unsafe_api_base_rejected:
        raise AssertionError("connector API base allowlist did not reject an attacker domain")
    if result["oauth"]["session_status"] != "consumed" or not result["oauth"]["wrong_principal_rejected"]:
        raise AssertionError("OAuth state binding/one-time consumption semantics are not preserved")
    if result["web_capture"]["parse_status"] != "ready" or not result["web_capture"]["same_document_identity"]:
        raise AssertionError("web capture retry did not remain idempotent")
    if result["feishu_sync"]["first_stats"]["changed"] != 1 or result["feishu_sync"]["second_stats"]["unchanged"] != 1:
        raise AssertionError("Feishu incremental sync did not distinguish changed and unchanged revisions")
    if result["feishu_sync"]["block_calls"] != 1:
        raise AssertionError("unchanged incremental sync refetched remote document blocks")
    return {
        "result": result,
        "evidence": {
            "provider_boundary": "fixed_http_and_in_memory_feishu_api",
            "provider_revision": "legacy-connector-capture-v1",
            "remote_network_used": False,
            "oauth_pkce_binding_real": True,
            "source_identity_binding": "source_connection_id+external_id",
            "incremental_revision_fast_path": "same_revision_skips_block_fetch",
            "raw_values_recorded": False,
            "unsafe_api_base_rejected": unsafe_api_base_rejected,
            "implementation_dependencies": implementation_dependencies,
        },
        "database_side_effects": {
            "business_rows_written": result["source_identity"]["source_count"]
            + result["source_identity"]["item_count"]
            + result["source_identity"]["document_count"]
            + result["source_identity"]["grant_count"]
            + result["source_identity"]["sync_run_count"],
            "raw_values_in_catalog": False,
            "oauth_session_replay_rejected": result["oauth"]["session_status"] == "consumed",
        },
        "filesystem_side_effects": {
            "business_writes": [entry["path"] for entry in files_after if entry["path"] not in {item["path"] for item in files_before}],
            "before": files_before,
            "after": files_after,
            "knowledge_artifacts_under_root": all(
                entry["path"].startswith("imported/") or entry["path"].startswith("read-later/") or entry["path"].startswith("assets/")
                for entry in files_after
            ),
        },
        "provider_revision": "legacy-connectors-and-capture-v1",
        "failure_semantics": "wrong_principal->oauth_rejected; retry->same_document; same_revision->incremental_unchanged; unsafe_api_base->rejected",
        "sanitized_fixture_manifest": {
            "fixtures": ["docs/knowledge-platform/golden-fixtures/connectors_and_capture.json"]
        },
    }


def observe() -> dict[str, Any]:
    fixture = _fixture()
    previous_home = os.environ.get("PUDDINGCLAW_HOME")
    previous_key_provider = os.environ.get("PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER")
    previous_knowledge_dir = os.environ.get("PUDDINGCLAW_KNOWLEDGE_DIR")
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-connectors-") as directory:
        root = Path(directory)
        os.environ["PUDDINGCLAW_HOME"] = str(root / "home")
        os.environ["PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER"] = "file"
        try:
            return asyncio.run(_observe_async(root, fixture))
        finally:
            if previous_home is None:
                os.environ.pop("PUDDINGCLAW_HOME", None)
            else:
                os.environ["PUDDINGCLAW_HOME"] = previous_home
            if previous_key_provider is None:
                os.environ.pop("PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER", None)
            else:
                os.environ["PUDDINGCLAW_CREDENTIAL_KEY_PROVIDER"] = previous_key_provider
            if previous_knowledge_dir is None:
                os.environ.pop("PUDDINGCLAW_KNOWLEDGE_DIR", None)
            else:
                os.environ["PUDDINGCLAW_KNOWLEDGE_DIR"] = previous_knowledge_dir


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
