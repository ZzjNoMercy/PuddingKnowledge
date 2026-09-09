"""Independent SQLAlchemy metadata and session-factory boundary.

This is a Phase 0B ownership primitive, not a production migration. It is not
imported by the legacy database initializer. Keeping the two metadata objects
separate makes accidental cross-owner ORM relationships impossible to hide in
a later extraction.
"""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool


class CatalogOwner(StrEnum):
    HARNESS = "harness"
    KNOWLEDGE = "knowledge"


HARNESS_METADATA = MetaData()
KNOWLEDGE_METADATA = MetaData()


class HarnessBase(DeclarativeBase):
    metadata = HARNESS_METADATA


class KnowledgeBase(DeclarativeBase):
    metadata = KNOWLEDGE_METADATA


def create_session_factory(
    database_url: str,
    *,
    owner: CatalogOwner,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create an owner-scoped engine without consulting Claw configuration."""

    if not database_url.strip():
        raise ValueError("database_url must not be empty")
    if owner not in (CatalogOwner.HARNESS, CatalogOwner.KNOWLEDGE):
        raise ValueError(f"unsupported catalog owner: {owner}")

    kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
    if database_url.startswith("sqlite+"):
        kwargs["connect_args"] = {"check_same_thread": False}
    elif database_url.startswith("postgresql+asyncpg"):
        kwargs["poolclass"] = NullPool
    engine = create_async_engine(database_url, **kwargs)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def create_harness_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create the Harness-owned session factory without a generic-owner call site."""

    return create_session_factory(database_url, owner=CatalogOwner.HARNESS)


def create_knowledge_session_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Create the Platform-owned session factory without legacy configuration."""

    return create_session_factory(database_url, owner=CatalogOwner.KNOWLEDGE)
