"""Harness-owned catalog models kept independent from Platform metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .metadata import HarnessBase


class HarnessSchemaVersion(HarnessBase):
    __tablename__ = "harness_catalog_schema_versions"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HarnessWorkerAccessLog(HarnessBase):
    """Minimal Harness audit record; request/session payloads stay Harness-owned."""

    __tablename__ = "worker_access_logs"
    __table_args__ = (Index("ix_worker_access_logs_key_created", "key_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    key_id: Mapped[str] = mapped_column(String(160), nullable=False)
    key_name: Mapped[str] = mapped_column(String(160), nullable=False, default="")
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status_code: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
