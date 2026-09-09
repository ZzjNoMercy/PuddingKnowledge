"""Deterministic Vanna-index rebuild from portable Package evidence.

This module deliberately accepts a narrow rebuilder port.  The port may wrap
the vendored Vanna fork, but the Package is the source of truth and a runtime
Milvus collection is never copied or treated as migration input.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from knowledge_platform.package.builder import PackageValidationError, _load_json, validate_package


class VannaIndexRebuilder(Protocol):
    def begin(self, *, package_revision: str, input_digest: str) -> None: ...

    def add_ddl(self, *, source_id: str, item_id: str, content: str) -> None: ...

    def add_documentation(self, *, source_id: str, item_id: str, content: str) -> None: ...

    def add_sql_example(self, *, source_id: str, item_id: str, question: str, sql: str) -> None: ...

    def add_entity(
        self,
        *,
        source_id: str,
        item_id: str,
        canonical_name: str,
        entity_type: str,
        table_column: str,
        aliases: Sequence[str],
    ) -> None: ...

    def commit(self) -> None: ...

    def abort(self) -> None: ...


@dataclass(frozen=True, slots=True)
class VannaRebuildResult:
    package_revision: str
    input_digest: str
    source_count: int
    counts: Mapping[str, int]


def _input_digest(sources: Sequence[Mapping[str, object]]) -> str:
    payload = json.dumps(sources, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def rebuild_vanna_indexes(*, package_root: Path, rebuilder: VannaIndexRebuilder) -> VannaRebuildResult:
    """Rebuild four Vanna evidence indexes after full Package validation.

    The operation is explicit and transactional at the rebuilder boundary;
    callers choose when to run it after Package import.  No host path,
    credential, database connection string or Milvus state is accepted.
    """

    validation = validate_package(package_root)
    database = _load_json(validation.package_root, "database/index.json")
    if not isinstance(database, dict) or database.get("format") != "agent-knowledge-database-index/v1":
        raise PackageValidationError("database rebuild index is invalid")
    sources = database.get("sources")
    if not isinstance(sources, list) or any(not isinstance(source, Mapping) for source in sources):
        raise PackageValidationError("database rebuild sources are invalid")
    ordered_sources = sorted(sources, key=lambda source: str(source.get("id") or ""))
    input_digest = _input_digest(ordered_sources)
    counts = {"ddl": 0, "documentation": 0, "sql_examples": 0, "entities": 0}
    rebuilder.begin(package_revision=validation.package_revision, input_digest=input_digest)
    try:
        for source in ordered_sources:
            source_id = str(source["id"])
            for item in source["ddl"]:
                rebuilder.add_ddl(source_id=source_id, item_id=str(item["id"]), content=str(item["content"]))
                counts["ddl"] += 1
            for item in source["documentation"]:
                rebuilder.add_documentation(
                    source_id=source_id, item_id=str(item["id"]), content=str(item["content"])
                )
                counts["documentation"] += 1
            for item in source["sql_examples"]:
                rebuilder.add_sql_example(
                    source_id=source_id,
                    item_id=str(item["id"]),
                    question=str(item["question"]),
                    sql=str(item["sql"]),
                )
                counts["sql_examples"] += 1
            for item in source["entities"]:
                aliases = item.get("aliases", [])
                if not isinstance(aliases, list):
                    raise PackageValidationError("database entity aliases are invalid")
                rebuilder.add_entity(
                    source_id=source_id,
                    item_id=str(item["id"]),
                    canonical_name=str(item["canonical_name"]),
                    entity_type=str(item["entity_type"]),
                    table_column=str(item["table_column"]),
                    aliases=tuple(str(alias) for alias in aliases),
                )
                counts["entities"] += 1
        rebuilder.commit()
    except Exception:
        try:
            rebuilder.abort()
        except Exception:
            pass
        raise
    return VannaRebuildResult(
        package_revision=validation.package_revision,
        input_digest=input_digest,
        source_count=len(ordered_sources),
        counts=counts,
    )


class RecordingVannaIndexRebuilder:
    """Deterministic local rebuilder used by shadow tests and dry runs."""

    def __init__(self) -> None:
        self.items: dict[str, list[dict[str, object]]] = {
            "ddl": [],
            "documentation": [],
            "sql_examples": [],
            "entities": [],
        }
        self.committed = False
        self.aborted = False

    def begin(self, *, package_revision: str, input_digest: str) -> None:
        self._header = {"package_revision": package_revision, "input_digest": input_digest}

    def add_ddl(self, *, source_id: str, item_id: str, content: str) -> None:
        self.items["ddl"].append({"source_id": source_id, "id": item_id, "content": content})

    def add_documentation(self, *, source_id: str, item_id: str, content: str) -> None:
        self.items["documentation"].append({"source_id": source_id, "id": item_id, "content": content})

    def add_sql_example(self, *, source_id: str, item_id: str, question: str, sql: str) -> None:
        self.items["sql_examples"].append(
            {"source_id": source_id, "id": item_id, "question": question, "sql": sql}
        )

    def add_entity(
        self,
        *,
        source_id: str,
        item_id: str,
        canonical_name: str,
        entity_type: str,
        table_column: str,
        aliases: Sequence[str],
    ) -> None:
        self.items["entities"].append(
            {
                "source_id": source_id,
                "id": item_id,
                "canonical_name": canonical_name,
                "entity_type": entity_type,
                "table_column": table_column,
                "aliases": list(aliases),
            }
        )

    def commit(self) -> None:
        self.committed = True

    def abort(self) -> None:
        self.aborted = True
        for values in self.items.values():
            values.clear()


_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class LocalVannaCollectionCandidateRebuilder(RecordingVannaIndexRebuilder):
    """Persist a local, inactive Vanna Collection candidate.

    This is deliberately a file-backed staging adapter rather than a Milvus
    client.  It proves the Package -> Vanna evidence mapping and gives the
    local operator a durable candidate to inspect, while keeping activation,
    provider I/O and the legacy collection outside this boundary.
    """

    def __init__(self, *, collection_root: Path, collection_name: str) -> None:
        super().__init__()
        if not _COLLECTION_NAME_RE.fullmatch(collection_name):
            raise ValueError("Vanna Collection name is invalid")
        self._collection_root = collection_root.expanduser().absolute()
        self.collection_name = collection_name
        self._created_root = False

    def commit(self) -> None:
        if self.committed:
            raise RuntimeError("Vanna Collection candidate was already committed")
        parent = self._collection_root.parent
        if parent.exists() and parent.is_symlink():
            raise OSError("Vanna Collection parent must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True)
        if self._collection_root.exists() or self._collection_root.is_symlink():
            raise FileExistsError("Vanna Collection candidate already exists")
        os.mkdir(self._collection_root, 0o700)
        self._created_root = True
        try:
            file_digests: dict[str, str] = {}
            for category, items in self.items.items():
                file_digests[f"{category}.jsonl"] = self._write_jsonl(category, items)
            manifest = {
                "format": "agent-knowledge-platform-vanna-collection-candidate/v1",
                "collection_name": self.collection_name,
                "package_revision": self._header["package_revision"],
                "input_digest": self._header["input_digest"],
                "counts": {category: len(items) for category, items in self.items.items()},
                "file_digests": file_digests,
                "active": False,
                "activation_allowed": False,
                "provider_io_performed": False,
                "legacy_collection_read": False,
            }
            self._write_bytes("collection-manifest.json", _json_bytes(manifest))
            self.committed = True
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        super().abort()
        if self._created_root and self._collection_root.exists() and not self._collection_root.is_symlink():
            shutil.rmtree(self._collection_root)
        self._created_root = False

    def _write_jsonl(self, category: str, items: list[dict[str, object]]) -> str:
        payload = b"".join(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            for item in items
        )
        self._write_bytes(f"{category}.jsonl", payload)
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _write_bytes(self, name: str, payload: bytes) -> None:
        destination = self._collection_root / name
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Vanna Collection candidate file already exists")
        with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{name}.", dir=self._collection_root, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def local_vanna_collection_candidate(
    *, package_root: Path, output_root: Path, collection_name: str
) -> VannaRebuildResult:
    """Rebuild Package evidence into one inactive local Collection candidate."""

    rebuilder = LocalVannaCollectionCandidateRebuilder(
        collection_root=output_root / "collections" / collection_name,
        collection_name=collection_name,
    )
    return rebuild_vanna_indexes(package_root=package_root, rebuilder=rebuilder)
