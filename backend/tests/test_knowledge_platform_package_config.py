import json
from pathlib import Path

import pytest

from knowledge_platform.local.packages import load_package_config


def test_host_bindings_reject_aliases_and_overlapping_outputs(tmp_path):
    config = {'version': 1, 'space_ids': ['s'], 'imports': [],
              'exports': [{'id': 'a', 'path': str(tmp_path / 'a.zip')}]}
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config))
    assert load_package_config(path) == config
    config['exports'].append({'id': 'b', 'path': str(tmp_path / 'a.zip')})
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): load_package_config(path)
    config['exports'].pop()
    (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
    config['exports'][0]['path'] = str(tmp_path / 'alias' / 'a.zip')
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): load_package_config(path)


@pytest.mark.parametrize('update', [
    {'version': True}, {'space_ids': []}, {'space_ids': ['s', 's']},
    {'imports': [{'id': 'i', 'path': '/private/tmp/i.zip', 'digest': 'unverified'}]},
    {'exports': [{'id': 'o', 'path': '../a.zip'}]},
])
def test_config_is_explicit_and_digest_bound(tmp_path, update):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'version': 1, 'space_ids': ['s'], 'imports': [], 'exports': [], **update}))
    with pytest.raises(ValueError): load_package_config(path)


@pytest.mark.asyncio
async def test_bound_request_identity_survives_restart_and_rejects_changed_binding(tmp_path):
    from knowledge_contracts import Correlation
    from knowledge_platform.ingestion.admin import PackageImportRequest
    from knowledge_platform.local.packages import BoundPackageImport
    from knowledge_platform.local.package_import import LocalPackagePublisher
    from test_knowledge_platform_local_package_import import _catalog, _package, _principal, _digest
    archive = _package(tmp_path); catalog = tmp_path / 'catalog.db'; _catalog(catalog)
    config = {'version': 1, 'space_ids': ['space_1'], 'exports': [],
              'imports': [{'id': 'in', 'path': str(archive), 'digest': _digest(archive)}]}
    request = PackageImportRequest('in', 'once')
    for turn in range(2):
        with LocalPackagePublisher(catalog, tmp_path / 'state') as publisher:
            service = BoundPackageImport(publisher, config)
            result = await service.stage(principal=_principal('space_1'), correlation=Correlation('test'), request=request)
            assert result.status == 'ok'
            assert result.data['idempotent'] is bool(turn)
    changed = {**config, 'imports': [{**config['imports'][0], 'digest': 'sha256:' + '0' * 64}]}
    with LocalPackagePublisher(catalog, tmp_path / 'state') as publisher:
        service = BoundPackageImport(publisher, changed)
        result = await service.stage(principal=_principal('space_1'), correlation=Correlation('test'), request=request)
        assert result.status == 'error' and 'identity changed' in result.error.message


def test_package_scope_cannot_inherit_other_runtime_service_spaces():
    from knowledge_contracts import Principal
    from knowledge_platform.local.packages import package_principal
    principal = Principal('host', scopes=('knowledge.admin', 'knowledge.space:default', 'knowledge:space:approved'))
    restricted = package_principal(principal, {'space_ids': ['approved']})
    assert restricted.scopes == ('knowledge.admin', 'knowledge:space:approved')
    assert principal.scopes == ('knowledge.admin', 'knowledge.space:default', 'knowledge:space:approved')
