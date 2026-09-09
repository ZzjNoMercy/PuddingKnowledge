from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from knowledge_platform.local.database import build_database_services, load_database_config

DIGEST = "sha256:" + "a" * 64


def database_config(root: Path, port: int = 5432) -> dict:
    return {
        "format": "knowledge-local-database/v1", "collection_id": "dataset_kb_default",
        "source": {"dataset_id": "database_sales", "host": "127.0.0.1", "port": port,
                   "database": "postgres", "username": "knowledge_reader", "allowed_tables": ["sales"], "password_env": None},
        "vanna": {"root": str(root), "collection_name": "local_collection", "package_revision": DIGEST, "input_digest": DIGEST},
    }


@pytest.mark.parametrize("section,field,value", [
    ("source", "host", "example.com"), ("source", "port", True),
    ("source", "port", 65536), ("source", "allowed_tables", []),
    ("source", "allowed_tables", ["sales", "sales"]),
    ("source", "allowed_tables", ["sales;DROP TABLE sales"]),
    ("source", "password_env", "PYTHONPATH"), ("source", "password", "must-not-accept"),
    ("vanna", "root", "relative/path"), ("vanna", "input_digest", "wrong"),
])
def test_database_config_rejects_unsafe_values(tmp_path, section, field, value):
    config = database_config(tmp_path)
    config[section][field] = value
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises((TypeError, ValueError)):
        load_database_config(path)


def test_database_config_is_explicit_bounded_and_secret_free(tmp_path):
    path = tmp_path / "config.json"
    config = database_config(tmp_path)
    config["source"]["password_env"] = "KNOWLEDGE_DB_PASSWORD"
    path.write_text(json.dumps(config))
    loaded = load_database_config(path)
    assert loaded.password_env == "KNOWLEDGE_DB_PASSWORD"
    assert loaded.source.password == ""
    assert loaded.source.semantic_context_hash == DIGEST
    link = tmp_path / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_database_config(link)
    path.write_text('{"format":"first","format":"second"}')
    with pytest.raises(ValueError, match="duplicate"):
        load_database_config(path)
    path.write_text(" " * 65537)
    with pytest.raises(ValueError, match="size limit"):
        load_database_config(path)


def test_vanna_identity_rejected_before_database_io(tmp_path, monkeypatch):
    from knowledge_platform.database import LocalVannaCollectionCandidateRebuilder
    from knowledge_platform.database.postgres import LocalPostgresDatabaseDatasetResolver

    root = tmp_path / "local_collection"
    builder = LocalVannaCollectionCandidateRebuilder(collection_root=root, collection_name="local_collection")
    builder.begin(package_revision=DIGEST, input_digest=DIGEST)
    builder.commit()
    config = copy.deepcopy(database_config(root))
    config["vanna"]["input_digest"] = "sha256:" + "b" * 64
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    def no_database_io(*args, **kwargs):
        pytest.fail("database must not be contacted for invalid Vanna identity")
    monkeypatch.setattr(LocalPostgresDatabaseDatasetResolver, "resolve", no_database_io)
    with pytest.raises(ValueError):
        build_database_services(load_database_config(path), tmp_path / "unused.sqlite3")


def test_missing_explicit_password_refuses_before_database_io(tmp_path, monkeypatch):
    from knowledge_platform.database import LocalVannaCollectionCandidateRebuilder
    from knowledge_platform.database.postgres import LocalPostgresDatabaseDatasetResolver

    root = tmp_path / "local_collection"
    builder = LocalVannaCollectionCandidateRebuilder(collection_root=root, collection_name="local_collection")
    builder.begin(package_revision=DIGEST, input_digest=DIGEST)
    builder.commit()
    config = database_config(root)
    config["source"]["password_env"] = "KNOWLEDGE_DB_MISSING"
    monkeypatch.delenv("KNOWLEDGE_DB_MISSING", raising=False)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    def no_database_io(*args, **kwargs):
        pytest.fail("missing explicit credential must not trigger database IO")
    monkeypatch.setattr(LocalPostgresDatabaseDatasetResolver, "resolve", no_database_io)
    with pytest.raises(ValueError, match="environment variable is missing"):
        build_database_services(load_database_config(path), tmp_path / "unused.sqlite3")
