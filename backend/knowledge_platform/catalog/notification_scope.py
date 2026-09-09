"""Space-scoped Platform notification events.

The event body is an append-only fact.  Inbox/read state belongs to the
consuming product, while this module owns the explicit event-to-Space binding
needed before an Admin client may discover an event.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from knowledge_contracts import NotificationEvent

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_CATEGORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_MAX_LIMIT = 100


def _valid_id(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _valid_category(value: Any) -> str:
    if not isinstance(value, str) or _CATEGORY_RE.fullmatch(value) is None:
        raise ValueError("notification category is invalid")
    return value


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    try:
        decoded = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is invalid JSON") from error
    if not isinstance(decoded, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): item for key, item in decoded.items()}


class SqliteNotificationEventScopeStore:
    """Persist and read only explicitly Space-bound Platform events."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    @property
    def catalog_revision(self) -> str:
        digest = hashlib.sha256()
        for candidate in (
            self._database_path,
            Path(f"{self._database_path}-wal"),
            Path(f"{self._database_path}-shm"),
        ):
            if candidate.is_symlink():
                raise OSError("Catalog sidecar must not be a symlink")
            if not candidate.is_file():
                continue
            digest.update(candidate.name.encode("utf-8"))
            digest.update(b"\0")
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def bind_event(self, *, event_id: str, space_id: str, bound_at: str | None = None) -> dict[str, str]:
        event_id = _valid_id(event_id, label="event_id")
        space_id = _valid_id(space_id, label="space_id")
        timestamp = bound_at or datetime.now(UTC).isoformat()
        if not isinstance(timestamp, str) or not timestamp.strip():
            raise ValueError("notification binding timestamp is invalid")
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM knowledge_notification_events WHERE id = ?", (event_id,)
            ).fetchone() is None:
                raise LookupError("notification event does not exist")
            existing = connection.execute(
                "SELECT space_id, bound_at FROM knowledge_notification_event_scopes WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != space_id:
                    raise ValueError("notification event is already bound to another Space")
                return {"event_id": event_id, "space_id": space_id, "bound_at": str(existing[1])}
            connection.execute(
                "INSERT INTO knowledge_notification_event_scopes (event_id, space_id, bound_at) VALUES (?, ?, ?)",
                (event_id, space_id, timestamp),
            )
        return {"event_id": event_id, "space_id": space_id, "bound_at": timestamp}

    def publish(self, *, event: NotificationEvent, category: str, space_id: str) -> dict[str, Any]:
        if not isinstance(event, NotificationEvent):
            raise TypeError("event must be a NotificationEvent")
        category = _valid_category(category)
        space_id = _valid_id(space_id, label="space_id")
        payload = json.dumps(dict(event.payload), ensure_ascii=False, sort_keys=True)
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT event_type, category, subject_type, subject_id, title, body, payload_json, created_at "
                "FROM knowledge_notification_events WHERE id = ?",
                (event.event_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO knowledge_notification_events "
                    "(id, event_type, category, subject_type, subject_id, title, body, payload_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.event_type,
                        category,
                        event.subject_type,
                        event.subject_id,
                        event.title,
                        event.body,
                        payload,
                        event.occurred_at,
                    ),
                )
            elif tuple(str(value) for value in existing) != (
                event.event_type,
                category,
                event.subject_type,
                event.subject_id,
                event.title,
                event.body,
                payload,
                event.occurred_at,
            ):
                raise ValueError("notification event identity already has a different definition")
            existing_scope = connection.execute(
                "SELECT space_id FROM knowledge_notification_event_scopes WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()
            if existing_scope is not None and str(existing_scope[0]) != space_id:
                raise ValueError("notification event is already bound to another Space")
            if existing_scope is None:
                connection.execute(
                    "INSERT INTO knowledge_notification_event_scopes (event_id, space_id, bound_at) VALUES (?, ?, ?)",
                    (event.event_id, space_id, event.occurred_at),
                )
        return self._public_event(
            {
                "id": event.event_id,
                "event_type": event.event_type,
                "category": category,
                "subject_type": event.subject_type,
                "subject_id": event.subject_id,
                "title": event.title,
                "body": event.body,
                "payload_json": payload,
                "created_at": event.occurred_at,
                "space_id": space_id,
            }
        )

    def list_events(self, *, space_id: str, limit: int = 20) -> list[dict[str, Any]]:
        space_id = _valid_id(space_id, label="space_id")
        if type(limit) is not int or not 1 <= limit <= _MAX_LIMIT:
            raise ValueError("notification limit is invalid")
        revision_before = self.catalog_revision
        connection = sqlite3.connect(f"file:{self._database_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            rows = connection.execute(
                "SELECT e.id, e.event_type, e.category, e.subject_type, e.subject_id, e.title, e.body, "
                "e.payload_json, e.created_at, s.space_id "
                "FROM knowledge_notification_events AS e "
                "JOIN knowledge_notification_event_scopes AS s ON s.event_id = e.id "
                "WHERE s.space_id = ? ORDER BY e.created_at DESC, e.id DESC LIMIT ?",
                (space_id, limit),
            ).fetchall()
        finally:
            connection.close()
        result = [self._public_event(dict(row)) for row in rows]
        if revision_before != self.catalog_revision:
            raise OSError("Catalog changed during notification read")
        return result

    @staticmethod
    def _public_event(row: Mapping[str, Any]) -> dict[str, Any]:
        event = NotificationEvent(
            event_id=str(row["id"]),
            event_type=str(row["event_type"]),
            subject_type=str(row["subject_type"]),
            subject_id=str(row["subject_id"]),
            title=str(row["title"]),
            body=str(row["body"] or ""),
            payload=_json_object(row.get("payload_json"), label="notification payload"),
            occurred_at=str(row["created_at"]),
        )
        space_id = _valid_id(row.get("space_id"), label="space_id")
        data = event.to_dict()
        data.update(
            {
                "category": _valid_category(str(row["category"])),
                "space_id": space_id,
                "resource_uri": f"knowledge://events/notifications/{event.event_id}",
            }
        )
        return data


__all__ = ["SqliteNotificationEventScopeStore"]
