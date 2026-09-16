from __future__ import annotations

import base64
import hashlib
import json
import secrets

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from knowledge_platform.distribution import credential_rebind as rebind
from knowledge_platform.local.vault import LocalCredentialStore

OWNER = 'local'
PRECISE = 'sk-mineru-precise-9f8e7d6c5b4a'
LIGHT = 'sk-mineru-light-1a2b3c4d5e6f'
LLAMA = 'llx-llama-cloud-0a1b2c3d4e5f'
DATABASE = 'pg-analytics-password-6f7e8d9c'
FEISHU_APP = json.dumps({'app_id': 'cli_a123', 'app_secret': 'feishu-app-secret-778899aabbcc'}, separators=(',', ':'))
FEISHU_GRANT = json.dumps(
    {'access_token': 'u-access-token-112233445566', 'refresh_token': 'u-refresh-token-aabbccddeeff'},
    separators=(',', ':'),
)
FEISHU_VERIFIER = 'pkce-verifier-0123456789abcdef'
DEEPSEEK = 'sk-deepseek-provider-abcdef0123'
SILICONFLOW = 'sk-siliconflow-provider-456789abcd'
TAVILY = 'tvly-web-search-feedface00'
ALL_SECRETS = (PRECISE, LIGHT, LLAMA, DATABASE, FEISHU_APP, FEISHU_GRANT, FEISHU_VERIFIER, DEEPSEEK, SILICONFLOW, TAVILY)
FEISHU_ENTRIES = {
    'feishu-app-fapp_1': FEISHU_APP,
    'feishu-oauth-verifier-foauth_1': FEISHU_VERIFIER,
    'feishu-user-grant-fgrant_1-v1': FEISHU_GRANT,
}
PROVIDER_ENTRIES = {
    'deepseek-credential-default': DEEPSEEK,
    'siliconflow-credential-default': SILICONFLOW,
}


def _seal(key, plaintext, *, owner=OWNER, provider='provider-registry', profile_id='default'):
    """Mirror of the legacy PuddingClaw envelope seal (profiles.py:451-462)."""

    nonce = secrets.token_bytes(12)
    aad = f'puddingclaw:v1:{owner}:{provider}:{profile_id}'.encode()
    envelope = {
        'version': 2,
        'algorithm': 'AES-256-GCM',
        'key_id': 'sha256:' + hashlib.sha256(key).hexdigest()[:32],
        'nonce': base64.b64encode(nonce).decode('ascii'),
        'ciphertext': base64.b64encode(AESGCM(key).encrypt(nonce, plaintext, aad)).decode('ascii'),
    }
    return json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode()


def _credentials():
    return {
        'parser-mineru_cloud_precise': {'value': PRECISE, 'updated_at': 1},
        'parser-mineru_cloud_light': {'value': LIGHT, 'updated_at': 1},
        'parser-llama_parse_cloud': {'value': LLAMA, 'updated_at': 1},
        'database-config': {'value': DATABASE, 'updated_at': 1},
        **{ref: {'value': value, 'updated_at': 1} for ref, value in FEISHU_ENTRIES.items()},
        **{ref: {'value': value, 'updated_at': 1} for ref, value in PROVIDER_ENTRIES.items()},
        'web-search-tavily-default': {'value': TAVILY, 'updated_at': 1},
    }


def _registry(credentials):
    return json.dumps({'version': 1, 'credentials': credentials}, sort_keys=True, separators=(',', ':')).encode()


def _home(tmp_path, *, provider='file', key=None, manifest_key=None, registry_key=None, credentials=None,
          registry=True, file_key=True, flows=1, profiles=True, skill_secrets=True):
    home = tmp_path / 'legacy-home'
    key = key or secrets.token_bytes(32)
    manifest_key = key if manifest_key is None else manifest_key
    registry_key = manifest_key if registry_key is None else registry_key
    vault_keys = home / '.vault-keys'
    vault_keys.mkdir(parents=True)
    if file_key:
        (vault_keys / f'{OWNER}.key').write_bytes(key)
    manifest = {
        'schema_version': 1,
        'owner_user_id': OWNER,
        'provider': provider,
        'key_id': 'sha256:' + hashlib.sha256(manifest_key).hexdigest()[:32],
        'created_at': 1757000000,
    }
    (vault_keys / f'{OWNER}.provider.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
    credentials_root = home / 'users' / OWNER / 'credentials'
    credentials_root.mkdir(parents=True)
    if registry:
        (credentials_root / 'provider-registry.enc').write_bytes(
            _seal(registry_key, _registry(_credentials() if credentials is None else credentials))
        )
    for index in range(flows):
        flow_dir = credentials_root / 'authorization-flows'
        flow_dir.mkdir(exist_ok=True)
        (flow_dir / f'flow-{index}.enc').write_bytes(b'opaque-flow-envelope')
    if profiles:
        profile = credentials_root / 'lark' / 'lark_default'
        profile.mkdir(parents=True)
        (profile / 'vault.enc').write_bytes(b'opaque-profile-vault')
        (profile / 'profile.json').write_text('{}')
    if skill_secrets:
        secrets_dir = home / 'users' / OWNER / 'skill-secrets'
        secrets_dir.mkdir(parents=True)
        (secrets_dir / 'registry.enc').write_bytes(b'opaque-skill-secrets')
    return home, key


def _bundle(entries):
    return json.dumps(
        {'format': rebind.BUNDLE_FORMAT, 'entries': entries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    )


def _run(home, target, receipt, **kwargs):
    return rebind.rebind_credentials(home, OWNER, target, receipt, **kwargs)


def _entry(result, slot):
    matches = [entry for entry in result['rebinds'] if entry['slot'] == slot]
    assert len(matches) == 1
    return matches[0]


def _store(target):
    return LocalCredentialStore(target, owner_user_id=OWNER)


def test_rebind_happy_path_round_trips_every_present_slot(tmp_path) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    result = _run(home, target, receipt)

    assert result['state'] == 'completed'
    assert result['credential_continuity_verified'] is True
    assert result['counts'] == {'rebound': 6, 'absent': 0, 'failed': 0, 'not_applicable': 5, 'skipped': 1, 'retained': 1}
    assert result['source']['authority_provider'] == 'file'
    assert result['source']['fingerprint_unchanged'] is True

    expected_prefix = f'credential://users/{OWNER}/credentials/'
    assert _entry(result, 'database.password')['target_ref'] == expected_prefix + 'database.password'
    assert _entry(result, 'knowledge.parsers.items.mineru_cloud_precise')['target_ref'] == (
        expected_prefix + 'knowledge.parsers.items.mineru_cloud_precise.api_key'
    )
    assert _entry(result, 'knowledge.parsers.items.mineru_cloud_light')['target_ref'] == (
        expected_prefix + 'knowledge.parsers.items.mineru_cloud_light.api_key'
    )
    assert _entry(result, 'knowledge.parsers.items.llama_parse_cloud')['target_ref'] == (
        expected_prefix + 'knowledge.parsers.items.llama_parse_cloud.api_key'
    )
    feishu = _entry(result, 'knowledge.feishu.app_credentials')
    assert feishu['target_ref'] == expected_prefix + 'knowledge.feishu.app_credentials'
    assert feishu['bundle'] is True and sorted(feishu['source_refs']) == sorted(FEISHU_ENTRIES)
    providers = _entry(result, 'providers.*.credentials')
    assert providers['target_ref'] == expected_prefix + 'providers.credentials'
    assert providers['bundle'] is True and sorted(providers['source_refs']) == sorted(PROVIDER_ENTRIES)
    for entry in result['rebinds']:
        assert entry['source_ref_digest'].startswith('sha256:')
        assert entry['target_ref'].startswith('credential://')
        if entry['status'] == 'rebound':
            assert entry['vault_ref'].startswith(f'vault://users/{OWNER}/credentials/')
            assert entry['materialized'] is True

    reference_slots = {
        'knowledge.llm_wiki.gbrain.*': ['providers.*.credentials'],
        'knowledge.mineru.*': [
            'knowledge.parsers.items.mineru_cloud_precise',
            'knowledge.parsers.items.mineru_cloud_light',
            'providers.*.credentials',
        ],
        'knowledge.multimodal_index.*': ['providers.*.credentials'],
        'rag.*': ['providers.*.credentials'],
        'vanna.*': ['providers.*.credentials', 'database.password'],
    }
    for slot, covered_by in reference_slots.items():
        entry = _entry(result, slot)
        assert entry['status'] == 'not-applicable'
        assert entry['covered_by'] == covered_by
        assert entry['materialized'] is False

    store = _store(target)
    assert store.get(f'vault://users/{OWNER}/credentials/database.password') == DATABASE
    assert store.get(f'vault://users/{OWNER}/credentials/knowledge.parsers.items.mineru_cloud_precise.api_key') == PRECISE
    assert store.get(f'vault://users/{OWNER}/credentials/knowledge.parsers.items.mineru_cloud_light.api_key') == LIGHT
    assert store.get(f'vault://users/{OWNER}/credentials/knowledge.parsers.items.llama_parse_cloud.api_key') == LLAMA
    assert json.loads(store.get(f'vault://users/{OWNER}/credentials/knowledge.feishu.app_credentials')) == {
        'format': rebind.BUNDLE_FORMAT,
        'entries': dict(sorted(FEISHU_ENTRIES.items())),
    }
    assert json.loads(store.get(f'vault://users/{OWNER}/credentials/providers.credentials')) == {
        'format': rebind.BUNDLE_FORMAT,
        'entries': dict(sorted(PROVIDER_ENTRIES.items())),
    }

    assert result['skipped'] == [
        {'artifact': f'users/{OWNER}/credentials/authorization-flows/flow-0.enc', 'status': 'skipped',
         'reason': 'transient_in_flight_oauth_flow', 'source_ref_digest': result['skipped'][0]['source_ref_digest']}
    ]
    assert result['skipped'][0]['source_ref_digest'].startswith('sha256:')
    reasons = {item['artifact']: item['reason'] for item in result['not_applicable_artifacts']}
    assert reasons[f'users/{OWNER}/credentials/lark/lark_default/vault.enc'] == 'harness_owned_provider_profile_state'
    assert reasons[f'users/{OWNER}/skill-secrets/registry.enc'] == 'harness_owned_skill_secrets'
    assert result['retained_source_refs'] == [{'ref': 'web-search-tavily-default', 'reason': 'harness_owned'}]
    assert result['unrecognized_source_artifacts'] == []

    receipt_text = receipt.read_text()
    assert json.loads(receipt_text) == result
    for secret in ALL_SECRETS:
        assert secret not in receipt_text


def test_rebind_exact_retry_is_a_no_op_with_identical_receipt(tmp_path) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    first = _run(home, target, receipt)
    vault_bytes = (target / 'credentials' / OWNER / 'vault.enc').read_bytes()
    receipt_bytes = receipt.read_bytes()

    second = _run(home, target, receipt)

    assert second == first
    assert receipt.read_bytes() == receipt_bytes
    assert (target / 'credentials' / OWNER / 'vault.enc').read_bytes() == vault_bytes
    assert second['credential_continuity_verified'] is True


def test_rebind_refuses_a_target_conflict_without_overwriting(tmp_path) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    _store(target).put('database.password', 'pre-existing-different-value')

    result = _run(home, target, receipt)

    conflict = _entry(result, 'database.password')
    assert conflict['status'] == 'failed'
    assert conflict['reason'] == 'target_conflict'
    assert conflict['materialized'] is False
    assert result['credential_continuity_verified'] is False
    assert result['state'] == 'failed'
    assert result['counts']['rebound'] == 5
    assert _store(target).get(f'vault://users/{OWNER}/credentials/database.password') == 'pre-existing-different-value'


def test_rebind_corrupted_envelope_fails_closed_with_nothing_written(tmp_path) -> None:
    home, _key = _home(tmp_path)
    registry_path = home / 'users' / OWNER / 'credentials' / 'provider-registry.enc'
    envelope = json.loads(registry_path.read_bytes().decode('utf-8'))
    ciphertext = bytearray(base64.b64decode(envelope['ciphertext']))
    ciphertext[0] ^= 1
    envelope['ciphertext'] = base64.b64encode(bytes(ciphertext)).decode('ascii')
    registry_path.write_bytes(json.dumps(envelope, sort_keys=True, separators=(',', ':')).encode())
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'

    result = _run(home, target, receipt)

    assert result['credential_continuity_verified'] is False
    assert result['state'] == 'failed'
    assert result['error'] == 'legacy credential envelope cannot be decrypted'
    assert result['counts']['failed'] == 6
    assert result['counts']['rebound'] == 0
    assert not (target / 'credentials' / OWNER / 'vault.enc').exists()
    assert receipt.is_file()


def test_rebind_wrong_master_key_fails_closed(tmp_path) -> None:
    authority_key, registry_key = secrets.token_bytes(32), secrets.token_bytes(32)
    home, _key = _home(tmp_path / 'one', key=authority_key, registry_key=registry_key)
    target, receipt = tmp_path / 'one' / 'target', tmp_path / 'one' / 'receipt.json'
    result = _run(home, target, receipt)
    assert result['error'] == 'legacy credential envelope belongs to another key authority'
    assert result['credential_continuity_verified'] is False
    assert not (target / 'credentials' / OWNER / 'vault.enc').exists()

    home, _key = _home(tmp_path / 'two', key=secrets.token_bytes(32), manifest_key=registry_key,
                       registry_key=registry_key)
    target, receipt = tmp_path / 'two' / 'target', tmp_path / 'two' / 'receipt.json'
    result = _run(home, target, receipt)
    assert result['error'] == 'legacy credential authority key does not match its manifest'
    assert result['credential_continuity_verified'] is False
    assert not (target / 'credentials' / OWNER / 'vault.enc').exists()


def test_rebind_environment_provider_key_from_environment(tmp_path, monkeypatch) -> None:
    home, key = _home(tmp_path / 'hex', provider='environment', file_key=False)
    monkeypatch.setenv('PUDDINGCLAW_MASTER_KEY', key.hex())
    result = _run(home, tmp_path / 'hex' / 'target', tmp_path / 'hex' / 'receipt.json')
    assert result['credential_continuity_verified'] is True
    assert result['source']['authority_provider'] == 'environment'

    home, key = _home(tmp_path / 'b64', provider='environment', file_key=False)
    monkeypatch.setenv('PUDDINGCLAW_MASTER_KEY', base64.urlsafe_b64encode(key).decode('ascii').rstrip('='))
    result = _run(home, tmp_path / 'b64' / 'target', tmp_path / 'b64' / 'receipt.json')
    assert result['credential_continuity_verified'] is True

    monkeypatch.delenv('PUDDINGCLAW_MASTER_KEY')
    result = _run(home, tmp_path / 'b64' / 'target-2', tmp_path / 'b64' / 'receipt-2.json')
    assert result['credential_continuity_verified'] is False
    assert 'PUDDINGCLAW_MASTER_KEY' in result['error']


def test_rebind_keychain_only_behind_the_explicit_flag(tmp_path, monkeypatch) -> None:
    home, key = _home(tmp_path, provider='keychain')

    def forbidden(*args, **kwargs):
        raise AssertionError('security must not be invoked without --allow-keychain')

    monkeypatch.setattr(rebind.subprocess, 'run', forbidden)
    result = _run(home, tmp_path / 'target', tmp_path / 'receipt.json')
    assert result['credential_continuity_verified'] is False
    assert '--allow-keychain' in result['error']
    assert not (tmp_path / 'target' / 'credentials' / OWNER / 'vault.enc').exists()

    class _Completed:
        returncode = 0
        stderr = ''
        stdout = base64.urlsafe_b64encode(key).decode('ascii')

    monkeypatch.setattr(rebind.subprocess, 'run', lambda *args, **kwargs: _Completed())
    result = _run(home, tmp_path / 'target', tmp_path / 'receipt.json', allow_keychain=True)
    assert result['credential_continuity_verified'] is True
    assert result['source']['authority_provider'] == 'keychain'


def test_rebind_absent_slots_are_reported_without_failing(tmp_path) -> None:
    credentials = _credentials()
    del credentials['database-config']
    for ref in FEISHU_ENTRIES:
        del credentials[ref]
    credentials['parser-llama_parse_cloud'] = {'value': '', 'updated_at': 1}
    home, _key = _home(tmp_path, credentials=credentials, flows=0, profiles=False, skill_secrets=False)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'

    result = _run(home, target, receipt)

    assert result['credential_continuity_verified'] is True
    assert result['counts'] == {'rebound': 3, 'absent': 3, 'failed': 0, 'not_applicable': 5, 'skipped': 0, 'retained': 1}
    for slot in ('database.password', 'knowledge.feishu.app_credentials', 'knowledge.parsers.items.llama_parse_cloud'):
        entry = _entry(result, slot)
        assert entry['status'] == 'absent'
        assert entry['materialized'] is False
        assert entry['source_ref_digest'].startswith('sha256:')
        assert entry['target_ref'].startswith('credential://')
    assert result['skipped'] == []
    assert result['not_applicable_artifacts'] == []


def test_rebind_slot_filter_runs_only_the_selected_slots(tmp_path) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    result = _run(home, target, receipt, slots=['database.password'])

    assert result['credential_continuity_verified'] is True
    assert result['slots_selected'] == ['database.password']
    assert [entry['slot'] for entry in result['rebinds']] == ['database.password']
    assert result['counts']['rebound'] == 1
    assert result['retained_source_refs'] == [{'ref': 'web-search-tavily-default', 'reason': 'harness_owned'}]
    with pytest.raises(rebind.CredentialRebindError, match='unknown credential slot'):
        _run(home, tmp_path / 'target-2', tmp_path / 'receipt-2.json', slots=['no.such.slot'])


def test_rebind_source_mutation_aborts_unverified(tmp_path) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    registry_path = home / 'users' / OWNER / 'credentials' / 'provider-registry.enc'

    result = _run(home, target, receipt, _before_final_fingerprint=lambda: registry_path.write_bytes(b'mutated'))

    assert result['credential_continuity_verified'] is False
    assert result['state'] == 'failed'
    assert result['source']['fingerprint_unchanged'] is False
    assert result['counts']['rebound'] == 6


def test_rebind_manifest_owner_mismatch_fails_closed(tmp_path) -> None:
    home, _key = _home(tmp_path)
    manifest_path = home / '.vault-keys' / f'{OWNER}.provider.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['owner_user_id'] = 'someone-else'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + '\n')

    result = _run(home, tmp_path / 'target', tmp_path / 'receipt.json')

    assert result['credential_continuity_verified'] is False
    assert result['error'] == 'legacy credential authority manifest is invalid'
    assert result['counts']['failed'] == 6


def test_rebind_cli_contract_and_no_plaintext_leak(tmp_path, capsys) -> None:
    home, _key = _home(tmp_path)
    target, receipt = tmp_path / 'target', tmp_path / 'receipt.json'
    argv = ['--source-home', str(home), '--owner', OWNER, '--target-root', str(target), '--receipt', str(receipt)]

    assert rebind.main(argv) == 0
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary['format'] == rebind.FORMAT
    assert summary['credential_continuity_verified'] is True
    assert summary['counts']['rebound'] == 6

    receipt_text = receipt.read_text()
    for secret in ALL_SECRETS:
        assert secret not in receipt_text
        assert secret not in captured.out
        assert secret not in captured.err

    _store(target).put('database.password', 'cli-conflicting-value')
    assert rebind.main([*argv, '--receipt', str(tmp_path / 'receipt-2.json')]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)['credential_continuity_verified'] is False
    for secret in ALL_SECRETS:
        assert secret not in (tmp_path / 'receipt-2.json').read_text()
        assert secret not in captured.out
        assert secret not in captured.err

    assert rebind.main([*argv, '--slot', 'no.such.slot']) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out)['error_code'] == 'credential_rebind_rejected'
    for secret in ALL_SECRETS:
        assert secret not in captured.out
        assert secret not in captured.err


def test_rebind_source_files_are_byte_identical_after_the_run(tmp_path) -> None:
    home, _key = _home(tmp_path)
    before = {
        path.relative_to(home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(home.rglob('*'))
        if path.is_file()
    }
    result = _run(home, tmp_path / 'target', tmp_path / 'receipt.json')

    after = {
        path.relative_to(home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(home.rglob('*'))
        if path.is_file()
    }
    assert result['credential_continuity_verified'] is True
    assert after == before
    assert result['source']['fingerprint'] == {relative: 'sha256:' + digest for relative, digest in before.items()}
