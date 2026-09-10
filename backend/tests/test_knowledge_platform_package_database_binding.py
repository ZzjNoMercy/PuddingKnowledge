import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from knowledge_contracts import Principal
from knowledge_platform.local.package_import import LocalPackagePublisher
from knowledge_platform.local.package_database import PublishedDatabaseGateway
from knowledge_platform.local.database import load_database_config, build_database_services
from test_knowledge_platform_local_runtime import _build_minimal_catalog
from test_knowledge_platform_package_database_runtime import database_package


def published(tmp_path):
    archive,revision,digest=database_package(tmp_path)
    catalog=tmp_path/'catalog.db';_build_minimal_catalog(catalog)
    publisher=LocalPackagePublisher(catalog,tmp_path/'processing')
    asyncio.run(publisher.import_package(Principal('admin',scopes=('knowledge.admin','knowledge.space:space_kb_default')),
        archive,'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()))
    return publisher,revision,digest


@pytest.mark.parametrize('mutation',[
    "UPDATE knowledge_datasets SET asset_ids='[\"forged\"]' WHERE id='portable_database'",
    "DELETE FROM knowledge_package_database_collections",
    "UPDATE knowledge_package_database_collection_facts SET collection_json='[]', content_digest='sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'",
    "UPDATE knowledge_package_imports SET status='staged'",
])
def test_gateway_rejects_current_publication_changes(tmp_path,mutation):
    publisher,revision,digest=published(tmp_path)
    gateway=PublishedDatabaseGateway(publisher,source_id='sales_evidence',package_revision=revision,
        input_digest=digest,dataset_id='database_sales',space_id='space_kb_default',
        collection_id='portable_database',collection_version='1')
    root=gateway.gateway._root
    try:
        assert gateway.generate_sql('sales total',allow_llm_to_see_data=False,table_names=['sales']).startswith('SELECT')
        with sqlite3.connect(publisher.catalog) as db:db.execute(mutation)
        with pytest.raises((ValueError,LookupError)):
            gateway.generate_sql('sales total',allow_llm_to_see_data=False,table_names=['sales'])
    finally:gateway.close();publisher.close()
    assert not root.exists()


def test_failed_host_binding_cleans_index_and_does_not_connect(tmp_path,monkeypatch):
    from knowledge_platform.database.postgres import LocalPostgresDatabaseDatasetResolver
    publisher,revision,digest=published(tmp_path)
    config={'format':'knowledge-local-database/v2','collection_id':'portable_database','collection_version':'1',
        'source':{'dataset_id':'database_sales','host':'127.0.0.1','port':5432,'database':'postgres',
            'username':'knowledge_reader','allowed_tables':['sales'],'password_env':'KNOWLEDGE_DB_MISSING'},
        'vanna':{'package_source_id':'sales_evidence','package_revision':revision,'input_digest':digest}}
    path=tmp_path/'config.json';path.write_text(json.dumps(config))
    monkeypatch.delenv('KNOWLEDGE_DB_MISSING',raising=False)
    monkeypatch.setattr(LocalPostgresDatabaseDatasetResolver,'resolve',lambda *a,**k: pytest.fail('must not contact database'))
    roots=[];original=PublishedDatabaseGateway.close
    def close(self):roots.append(self.gateway._root);original(self)
    monkeypatch.setattr(PublishedDatabaseGateway,'close',close)
    try:
        with pytest.raises(ValueError,match='environment variable is missing'):
            build_database_services(load_database_config(path),publisher.catalog,publisher=publisher)
        assert len(roots)==1 and not roots[0].exists()
        del config['collection_version'];path.write_text(json.dumps(config))
        with pytest.raises(ValueError):load_database_config(path)
    finally:publisher.close()
