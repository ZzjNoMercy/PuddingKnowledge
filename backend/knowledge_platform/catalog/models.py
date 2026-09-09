"""First independent Platform Catalog tables.

These tables are intentionally new Platform-owned models. They do not reuse
the legacy ``knowledge.models.Base`` and do not create relationships to
Harness tables. Legacy KB/Document rows are migrated later by an explicit
versioned adapter, never by importing the old ORM into this package.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, PrimaryKeyConstraint, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, Mapper, mapped_column
from sqlalchemy.types import JSON

from knowledge_contracts import NotificationEvent

from .metadata import KnowledgeBase


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class KnowledgeSpace(KnowledgeBase):
    __tablename__ = "knowledge_spaces"
    __table_args__ = (UniqueConstraint("name", name="uq_knowledge_spaces_name"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeAsset(KnowledgeBase):
    __tablename__ = "knowledge_assets"
    __table_args__ = (
        Index("ix_knowledge_assets_space_kind", "space_id", "kind"),
        Index("ix_knowledge_assets_space_revision", "space_id", "revision"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mime_type: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[str] = mapped_column(String(240), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(120), nullable=False)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeConnector(KnowledgeBase):
    """Platform-owned source connection metadata; secrets stay behind refs."""

    __tablename__ = "knowledge_connectors"
    __table_args__ = (
        Index("ix_knowledge_connectors_space_status", "space_id", "status"),
        UniqueConstraint("space_id", "connector_key", "name", name="uq_knowledge_connector_name"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="ready")
    auth_type: Mapped[str] = mapped_column(String(60), nullable=False, default="builtin")
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    schedule_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    last_sync_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeDatabaseSource(KnowledgeBase):
    """Platform-owned database connection metadata; credentials stay in Vault."""

    __tablename__ = "knowledge_database_connectors"
    __table_args__ = (
        UniqueConstraint("space_id", "source_key", name="uq_knowledge_database_source_key"),
        Index("ix_knowledge_database_connectors_space_updated", "space_id", "updated_at"),
        Index("ix_knowledge_database_connectors_type", "source_type"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    host: Mapped[str] = mapped_column(String(300), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    database_name: Mapped[str] = mapped_column(String(200), nullable=False)
    username: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    selected_tables: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeSourceItem(KnowledgeBase):
    """Stable external source identity, decoupled from a connector ORM FK."""

    __tablename__ = "knowledge_source_items"
    __table_args__ = (
        UniqueConstraint("connector_id", "external_id", name="uq_platform_source_item_external_id"),
        Index("ix_platform_source_items_connector_status", "connector_id", "status"),
        Index("ix_platform_source_items_asset", "asset_id"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(160), nullable=False)
    external_id: Mapped[str] = mapped_column(String(500), nullable=False)
    external_parent_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_type: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    path_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    revision: Mapped[str | None] = mapped_column(String(240), nullable=True)
    content_digest: Mapped[str | None] = mapped_column(String(120), nullable=True)
    asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="discovered")
    remote_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    remote_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_sync_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeSyncRun(KnowledgeBase):
    """Platform-owned source sync job with explicit lease/fencing state."""

    __tablename__ = "knowledge_sync_runs"
    __table_args__ = (
        Index("ix_platform_sync_runs_status_created", "status", "created_at"),
        Index("ix_platform_sync_runs_connector_created", "connector_id", "created_at"),
        Index("ix_platform_sync_runs_lease", "lease_owner", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    connector_id: Mapped[str] = mapped_column(String(160), nullable=False)
    mode: Mapped[str] = mapped_column(String(60), nullable=False, default="incremental")
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="queued")
    cursor_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    current_step: Mapped[str] = mapped_column(String(100), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeCredential(KnowledgeBase):
    """Non-secret provider credential metadata; secret material stays in a Vault."""

    __tablename__ = "knowledge_credentials"
    __table_args__ = (
        UniqueConstraint("owner_principal_id", "provider", "external_key", name="uq_knowledge_credential_owner_key"),
        Index("ix_knowledge_credentials_provider_status", "provider", "status"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    owner_principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    external_key: Mapped[str] = mapped_column(String(240), nullable=False)
    display_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    api_base_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="pending_validation")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeCredentialGrant(KnowledgeBase):
    """OAuth grant metadata with the access/refresh tokens kept out of Catalog."""

    __tablename__ = "knowledge_credential_grants"
    __table_args__ = (
        UniqueConstraint("credential_id", "connector_id", "principal_id", name="uq_knowledge_credential_grant_binding"),
        Index("ix_knowledge_credential_grants_status", "status", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    credential_id: Mapped[str] = mapped_column(String(160), nullable=False)
    connector_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    open_id: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    union_id: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    tenant_key: Mapped[str] = mapped_column(String(240), nullable=False, default="")
    token_credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    granted_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    access_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeOAuthSession(KnowledgeBase):
    """One-time OAuth state metadata; PKCE verifier remains in the Vault."""

    __tablename__ = "knowledge_oauth_sessions"
    __table_args__ = (
        UniqueConstraint("state_hash", name="uq_knowledge_oauth_session_state_hash"),
        Index("ix_knowledge_oauth_sessions_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    state_hash: Mapped[str] = mapped_column(String(160), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(160), nullable=False)
    connector_id: Mapped[str] = mapped_column(String(160), nullable=False)
    principal_id: Mapped[str] = mapped_column(String(160), nullable=False)
    redirect_uri_digest: Mapped[str] = mapped_column(String(120), nullable=False)
    verifier_credential_ref: Mapped[str] = mapped_column(Text, nullable=False)
    requested_scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(80), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeWebCapture(KnowledgeBase):
    """URL capture metadata; raw URLs and storage paths stay outside Catalog."""

    __tablename__ = "knowledge_web_captures"
    __table_args__ = (
        UniqueConstraint("space_id", "canonical_url_digest", name="uq_knowledge_web_capture_canonical_digest"),
        Index("ix_knowledge_web_captures_space_status", "space_id", "parse_status", "reading_status"),
        Index("ix_knowledge_web_captures_source_item", "source_item_id"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    original_url_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    canonical_url_digest: Mapped[str] = mapped_column(String(120), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    site_name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    image_url_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    content_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    raw_snapshot_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    parse_status: Mapped[str] = mapped_column(String(60), nullable=False, default="queued")
    reading_status: Mapped[str] = mapped_column(String(60), nullable=False, default="unread")
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    connector_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    ingestion_job_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeIngestionJob(KnowledgeBase):
    """Durable capture/import work with explicit lease and fencing metadata."""

    __tablename__ = "knowledge_ingestion_jobs"
    __table_args__ = (
        Index("ix_knowledge_ingestion_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_ingestion_jobs_space_created", "space_id", "created_at"),
        Index("ix_knowledge_ingestion_jobs_connector_created", "connector_id", "created_at"),
        Index("ix_knowledge_ingestion_jobs_lease", "lease_owner", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, default="import")
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="queued")
    file_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(60), nullable=False, default="file")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    publish_targets: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    current_step: Mapped[str] = mapped_column(String(100), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    capture_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    connector_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KnowledgeIngestionEvent(KnowledgeBase):
    """Redacted, append-only progress evidence for an ingestion job."""

    __tablename__ = "knowledge_ingestion_events"
    __table_args__ = (Index("ix_knowledge_ingestion_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    level: Mapped[str] = mapped_column(String(30), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeStructuredAsset(KnowledgeBase):
    """Structured-file metadata; raw files and profiles live in Blob storage."""

    __tablename__ = "knowledge_structured_assets"
    __table_args__ = (
        UniqueConstraint("space_id", "source_key", name="uq_knowledge_structured_asset_source_key"),
        Index("ix_knowledge_structured_assets_space_status", "space_id", "reference_status"),
        Index("ix_knowledge_structured_assets_space_profile", "space_id", "profile_status"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source_key: Mapped[str] = mapped_column(String(240), nullable=False)
    document_asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_type: Mapped[str] = mapped_column(String(80), nullable=False)
    file_name: Mapped[str] = mapped_column(String(500), nullable=False)
    sheet_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    source_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    logical_path_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    profile_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    profile_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    content_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    profile_status: Mapped[str] = mapped_column(String(60), nullable=False, default="missing")
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    columns_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    reference_status: Mapped[str] = mapped_column(String(60), nullable=False, default="pending")
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeQueryResult(KnowledgeBase):
    """Portable query result metadata; session/tool-call IDs are correlation only."""

    __tablename__ = "knowledge_query_results"
    __table_args__ = (
        Index("ix_knowledge_query_results_status_created", "status", "created_at"),
        Index("ix_knowledge_query_results_expires", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="ready")
    question: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sql_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    columns_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    artifact_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    artifact_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    artifact_format: Mapped[str] = mapped_column(String(60), nullable=False, default="jsonl")
    correlation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeQueryResultScope(KnowledgeBase):
    """Explicit Space ownership binding for a QueryResult artifact."""

    __tablename__ = "knowledge_query_result_scopes"
    __table_args__ = (Index("ix_knowledge_query_result_scopes_space_bound", "space_id", "bound_at"),)

    query_result_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("knowledge_query_results.id"), primary_key=True
    )
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeProcessingJob(KnowledgeBase):
    """Portable import/processing job metadata with a fenced lease."""

    __tablename__ = "knowledge_processing_jobs"
    __table_args__ = (
        Index("ix_knowledge_processing_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_processing_jobs_space_created", "space_id", "created_at"),
        Index("ix_knowledge_processing_jobs_lease", "lease_owner", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, default="import")
    status: Mapped[str] = mapped_column(String(60), nullable=False, default="queued")
    title: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    file_name: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    file_type: Mapped[str] = mapped_column(String(60), nullable=False, default="file")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    source_sha256: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    asset_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_item_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sync_run_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    current_step: Mapped[str] = mapped_column(String(100), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KnowledgeProcessingEvent(KnowledgeBase):
    """Redacted progress evidence for a processing job."""

    __tablename__ = "knowledge_processing_events"
    __table_args__ = (Index("ix_knowledge_processing_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), nullable=False)
    level: Mapped[str] = mapped_column(String(30), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeAuthoringJob(KnowledgeBase):
    """Semantic-authoring job metadata without Session/Query ownership."""

    __tablename__ = "knowledge_authoring_jobs"
    __table_args__ = (
        Index("ix_knowledge_authoring_jobs_status_created", "status", "created_at"),
        Index("ix_knowledge_authoring_jobs_dimension_created", "dimension_id", "created_at"),
        Index("ix_knowledge_authoring_jobs_lease", "lease_owner", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, default="semantic_dimension_build")
    dimension_id: Mapped[str] = mapped_column(String(240), nullable=False)
    adapter: Mapped[str] = mapped_column(String(240), nullable=False)
    scope_uri: Mapped[str] = mapped_column(Text, nullable=False)
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    input_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(100), nullable=False, default="queued")
    current_step: Mapped[str] = mapped_column(String(120), nullable=False, default="queued")
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    staging_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    staging_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    published_uri: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_reference_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    result_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    correlation_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    error_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class KnowledgeAuthoringEvent(KnowledgeBase):
    """Redacted progress evidence for a semantic-authoring job."""

    __tablename__ = "knowledge_authoring_events"
    __table_args__ = (Index("ix_knowledge_authoring_events_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(160), ForeignKey("knowledge_authoring_jobs.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(30), nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeNotificationEvent(KnowledgeBase):
    """Platform event; Harness owns inbox/read state and consumes this event."""

    __tablename__ = "knowledge_notification_events"
    __table_args__ = (
        Index("ix_knowledge_notification_events_subject_created", "subject_type", "subject_id", "created_at"),
        Index("ix_knowledge_notification_events_type_created", "event_type", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(120), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def _validate_notification_event_model(mapper: Mapper[Any], connection: Any, target: KnowledgeNotificationEvent) -> None:
    """Keep ORM writes subject to the same framework-neutral event contract."""

    NotificationEvent(
        event_id=target.id,
        event_type=target.event_type,
        subject_type=target.subject_type,
        subject_id=target.subject_id,
        title=target.title,
        body=target.body,
        payload=target.payload_json or {},
        occurred_at=target.created_at.isoformat() if isinstance(target.created_at, datetime) else str(target.created_at),
    )


event.listen(KnowledgeNotificationEvent, "before_insert", _validate_notification_event_model)
event.listen(KnowledgeNotificationEvent, "before_update", _validate_notification_event_model)


class KnowledgeNotificationEventScope(KnowledgeBase):
    """Explicit Platform ownership binding for one notification event."""

    __tablename__ = "knowledge_notification_event_scopes"
    __table_args__ = (Index("ix_knowledge_notification_event_scopes_space_created", "space_id", "bound_at"),)

    event_id: Mapped[str] = mapped_column(
        String(160), ForeignKey("knowledge_notification_events.id"), primary_key=True
    )
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeDataset(KnowledgeBase):
    __tablename__ = "knowledge_datasets"
    __table_args__ = (
        PrimaryKeyConstraint("space_id", "id", "version", name="pk_knowledge_datasets_version"),
        Index("ix_knowledge_datasets_space_kind", "space_id", "kind"),
    )

    id: Mapped[str] = mapped_column(String(160))
    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    version: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    asset_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    semantic_asset_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    capabilities: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    freshness: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    manifest_digest: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeCollectionBinding(KnowledgeBase):
    """Explicit provider identity bound to one versioned Collection."""

    __tablename__ = "knowledge_collection_bindings"
    __table_args__ = (
        PrimaryKeyConstraint(
            "space_id",
            "collection_id",
            "collection_version",
            "capability",
            name="pk_knowledge_collection_bindings",
        ),
    )

    space_id: Mapped[str] = mapped_column(String(120), nullable=False)
    collection_id: Mapped[str] = mapped_column(String(160), nullable=False)
    collection_version: Mapped[str] = mapped_column(String(80), nullable=False)
    capability: Mapped[str] = mapped_column(String(80), nullable=False)
    binding_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class KnowledgeCatalogSchemaVersion(KnowledgeBase):
    __tablename__ = "knowledge_catalog_schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
