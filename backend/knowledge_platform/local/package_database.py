"""Rebuild the local example gateway from verified, published Package evidence."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from knowledge_platform.database import LocalVannaCollectionCandidateRebuilder, LocalVannaCollectionGateway


def source_input_digest(source: dict) -> str:
    return 'sha256:' + hashlib.sha256(json.dumps([source], ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


class PublishedDatabaseGateway:
    """A disposable index; the persistent Package record remains authoritative."""

    def __init__(self, publisher, *, source_id: str, package_revision: str, input_digest: str,
                 dataset_id: str, space_id: str, collection_id: str, collection_version: str):
        self.publisher, self.source_id = publisher, source_id
        self.package_revision, self.input_digest = package_revision, input_digest
        self.dataset_id, self.space_id = dataset_id, space_id
        self.collection_id, self.collection_version = collection_id, collection_version
        source = self._verify()
        self._temporary = tempfile.TemporaryDirectory(prefix='knowledge-database-index-',
            dir=Path(tempfile.gettempdir()).resolve())
        root = Path(self._temporary.name) / 'package_evidence'
        builder = LocalVannaCollectionCandidateRebuilder(collection_root=root, collection_name=root.name)
        try:
            builder.begin(package_revision=package_revision, input_digest=input_digest)
            for item in source['ddl']:
                builder.add_ddl(source_id=source_id, item_id=item['id'], content=item['content'])
            for item in source['documentation']:
                builder.add_documentation(source_id=source_id, item_id=item['id'], content=item['content'])
            for item in source['sql_examples']:
                builder.add_sql_example(source_id=source_id, item_id=item['id'], question=item['question'], sql=item['sql'])
            for item in source['entities']:
                builder.add_entity(source_id=source_id, item_id=item['id'], canonical_name=item['canonical_name'],
                    entity_type=item['entity_type'], table_column=item['table_column'], aliases=item['aliases'])
            builder.commit()
            self.gateway = LocalVannaCollectionGateway(root, expected_collection_name=root.name,
                expected_package_revision=package_revision, expected_input_digest=input_digest)
        except BaseException:
            self._temporary.cleanup()
            raise

    def _verify(self):
        source = self.publisher.read_database_source(self.source_id, self.package_revision)
        if (source['dataset_id'] != self.dataset_id or source['space_id'] != self.space_id
                or source.get('dialect') != 'postgresql' or source_input_digest(source) != self.input_digest):
            raise ValueError('Package database source does not match the host binding')
        collection = self.publisher.read_database_collection(self.space_id, self.collection_id,
            self.collection_version, self.package_revision)
        if self.source_id not in collection.get('database_source_ids', []) or 'database_nl2sql' not in collection['capabilities']:
            raise ValueError('Package database source is not owned by this Collection')
        return source

    def get_related_ddl(self, question):
        self._verify()
        return self.gateway.get_related_ddl(question)

    def get_related_documentation(self, question):
        self._verify()
        return self.gateway.get_related_documentation(question)

    def get_related_entities(self, question):
        self._verify()
        return self.gateway.get_related_entities(question)

    def generate_sql(self, question, **kwargs):
        self._verify()
        return self.gateway.generate_sql(question, **kwargs)

    def close(self):
        self._temporary.cleanup()


class PublishedDatabaseResolver:
    """Revalidate Package evidence before schema, generation and plan execution."""
    def __init__(self, initial, gateway):
        self.initial, self.gateway = initial, gateway

    def resolve(self, *, dataset_id, space_id):
        try:
            self.gateway._verify()
        except Exception:
            # Corrupt persisted facts and unavailable Catalog storage revoke
            # the binding; they must not escape as an unhandled API error.
            return None
        return self.initial.resolve(dataset_id=dataset_id, space_id=space_id)
