"""Host-owned input/output bindings for portable Knowledge Packages."""
from __future__ import annotations

import json
from pathlib import Path
import re
import hashlib
import sqlite3
from dataclasses import replace

from knowledge_contracts import QueryError, QueryErrorCode, QueryResult
from knowledge_platform.ingestion.admin import _authorized

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def load_package_config(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or set(value) != {"version", "space_ids", "imports", "exports"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("Package configuration fields are invalid")
    spaces = value["space_ids"]
    if (not isinstance(spaces, list) or not spaces or len(spaces) > 1000
            or any(not isinstance(s, str) or not _ID.fullmatch(s) for s in spaces)
            or len(set(spaces)) != len(spaces)):
        raise ValueError("Package Space bindings are invalid")
    destinations = set()
    for kind in ("imports", "exports"):
        entries = value[kind]
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ValueError("Package bindings are invalid")
        seen = set()
        for entry in entries:
            required = {"id", "path", "digest"} if kind == "imports" else {"id", "path"}
            if not isinstance(entry, dict) or set(entry) != required:
                raise ValueError("Package binding fields are invalid")
            identifier = entry["id"]
            if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen:
                raise ValueError("Package binding ID is invalid")
            seen.add(identifier)
            raw = entry["path"]
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise ValueError("Package path is invalid")
            bound = Path(raw)
            if not bound.is_absolute() or ".." in bound.parts or bound == Path("/"):
                raise ValueError("Package path must be absolute")
            if any(p.is_symlink() for p in (bound, *bound.parents)):
                raise ValueError("Package path contains a symlink")
            if kind == "imports":
                if not isinstance(entry["digest"], str) or not _DIGEST.fullmatch(entry["digest"]):
                    raise ValueError("Package digest binding is invalid")
            else:
                if str(bound) in destinations:
                    raise ValueError("Package destinations must be distinct")
                destinations.add(str(bound))
    return value


def package_principal(principal, config):
    allowed = set(config['space_ids'])
    def permitted(scope):
        for prefix in ('knowledge.space:', 'knowledge:space:'):
            if scope.startswith(prefix):
                return scope[len(prefix):] in allowed
        return True
    scopes = tuple(scope for scope in principal.scopes if permitted(scope))
    return replace(principal, scopes=scopes)


class BoundPackageImport:
    """Existing Admin DTO, resolved against host-owned immutable ZIP bindings."""
    def __init__(self, publisher, config):
        self.publisher, self.config = publisher, config
        self.bindings = {entry["id"]: entry for entry in config["imports"]}
        with publisher._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS knowledge_package_requests (request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL)")

    async def stage(self, *, principal, correlation, request):
        def error(code, message):
            return QueryResult(status="error", trace_id=correlation.trace_id,
                               error=QueryError(code=code, message=message))
        if not all(_authorized(principal, s) for s in self.config["space_ids"]):
            return error(QueryErrorCode.PERMISSION_DENIED, "Admin and configured Space scopes are required")
        binding = self.bindings.get(request.package_ref)
        if binding is None:
            return error(QueryErrorCode.NOT_FOUND, "Package binding is unavailable")
        key = hashlib.sha256(json.dumps([principal.subject_id, request.idempotency_key]).encode()).hexdigest()
        fingerprint = hashlib.sha256(json.dumps([binding, sorted(principal.scopes), self.config["space_ids"]], sort_keys=True).encode()).hexdigest()
        try:
            with self.publisher._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT fingerprint FROM knowledge_package_requests WHERE request_key=?", (key,)).fetchone()
                if prior is not None and prior[0] != fingerprint:
                    return error(QueryErrorCode.INVALID_REQUEST, "Package request identity changed")
                db.execute("INSERT OR IGNORE INTO knowledge_package_requests VALUES (?,?)", (key, fingerprint))
            result = await self.publisher.import_package(principal=package_principal(principal, self.config), package_zip=Path(binding["path"]),
                                                        expected_digest=binding["digest"])
            return QueryResult(status="ok", trace_id=correlation.trace_id, data=result)
        except PermissionError:
            return error(QueryErrorCode.PERMISSION_DENIED, "Package scope is not authorized")
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            return error(QueryErrorCode.INVALID_REQUEST, "Package import failed validation or publication")
