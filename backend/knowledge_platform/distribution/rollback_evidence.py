"""Assemble the single deterministic rollback evidence file from reverse-step receipts.

This is VERIFY-AND-BIND only: the heavy reverse steps (frozen workspace export,
non-core Catalog disposition, document reverse and Wiki reverse) are driven by
their own CLIs during the rehearsal.  This assembly re-verifies the four
presented artifacts — canonical bytes, format/state, inert flags, exact
cross-linkages and every committed output digest — then publishes one canonical
evidence file.  The Wiki artifact is either the verified inactive Wiki
candidate manifest or, for an installation that never had a Wiki domain, the
verified absent-Wiki attestation (a proof of absence, never a fallback for a
failed Wiki reverse).  The Harness rollback orchestrator binds sha256 of these
exact bytes into the ROLLED_BACK installation manifest, and both products'
writer rev-assignment must commit the same digest, so an exact retry must
produce byte-identical evidence and any drift must refuse.  No writer is
switched; rollback completion is the Harness orchestration plus assignment,
never this file.
"""
import argparse
import hashlib
import json
import os
import re

from . import wiki_archive as files, sqlite_reverse_delta as sql
from .other_catalog_reverse import FORMAT as DISPOSITION_FORMAT, STATE as DISPOSITION_STATE, load_disposition
from ..local import writer_authority as control
from ..local.workspace_freeze import _sync_directory

FORMAT = 'puddingknowledge-rollback-evidence/v1'
STATE = 'verified_rollback_evidence'
# Producing modules for the verified envelope contracts:
# - frozen export: local/frozen_export.py (puddingknowledge-frozen-workspace-export/v1)
# - document reverse: distribution/document_reverse.py (puddingknowledge-document-reverse/v5)
# - core reverse receipt embedded in the document manifest:
#   distribution/core_catalog_reverse.py (puddingknowledge-core-catalog-reverse/v1)
# - Wiki reverse: distribution/wiki_reverse.py + distribution/wiki_reverse_plan.py
#   (puddingknowledge-wiki-reverse/v1), or the proof-of-absence attestation
#   distribution/wiki_reverse_absent.py (puddingknowledge-wiki-reverse-absent/v1)
#   when the installation never had a Wiki domain
# - non-core disposition: distribution/other_catalog_reverse.py (imported above)
EXPORT_FORMAT = 'puddingknowledge-frozen-workspace-export/v1'
EXPORT_STATE = 'verified_frozen_export'
DOCUMENT_FORMAT = 'puddingknowledge-document-reverse/v5'
DOCUMENT_STATE = 'verified_inactive_documents'
CORE_FORMAT = 'puddingknowledge-core-catalog-reverse/v1'
CORE_STATE = 'verified_inactive_core_metadata'
WIKI_FORMAT = 'puddingknowledge-wiki-reverse/v1'
WIKI_STATE = 'verified_inactive_wiki'
WIKI_ABSENT_FORMAT = 'puddingknowledge-wiki-reverse-absent/v1'
WIKI_ABSENT_STATE = 'verified_absent_wiki'
FLAGS = ('rollback_completed', 'activation_allowed', 'installation_cutover_performed', 'indexes_rebuilt')
EXPORT_FLAGS = ('activation_allowed', 'rollback_completed', 'legacy_schema_converted',
                'credential_continuity_verified')
CANDIDATE_FLAGS = ('activation_allowed', 'rollback_completed', 'credential_continuity_verified',
                   'indexes_rebuilt', 'installation_path_rebound')
CORE_FLAGS = ('activation_allowed', 'rollback_completed', 'file_layout_reversed',
              'credential_continuity_verified')
EXPORT_KEYS = {'format', 'plan', 'plan_sha256', 'state', 'normalized_databases', 'normalized_inventory',
               *EXPORT_FLAGS}
EXPORT_PLAN_KEYS = {'format', 'operation_id', 'binding', 'journal_sha256', 'source_inventory',
                    'output_identity', 'databases'}
DOCUMENT_KEYS = {'format', 'plan', 'state', 'core_receipt', 'identity_map', 'catalog_sha256',
                 'derived_metadata_invalidation', 'attachment_rebinding', 'document_routes',
                 *CANDIDATE_FLAGS}
DOCUMENT_PLAN_KEYS = {'format', 'source_revision', 'inputs', 'body_root', 'bodies', 'dependency_graph',
                      'output_inventory', 'attachment_bindings', 'output_identity',
                      'candidate_knowledge_root'}
CORE_KEYS = {'format', 'state', 'source_revision', 'input_sha256', 'output_sha256', 'changes',
             *CORE_FLAGS}
WIKI_KEYS = {'format', 'plan', 'state', 'receipt', *CANDIDATE_FLAGS}
WIKI_PLAN_KEYS = {'format', 'installation_id', 'source_revision', 'inputs', 'output_identity',
                  'output_inventory', 'delta', 'receipts'}
WIKI_ABSENT_KEYS = {'format', 'plan', 'state', *CANDIDATE_FLAGS}
WIKI_ABSENT_PLAN_KEYS = {'format', 'source_revision', 'inputs', 'output_identity', 'absence'}
WIKI_ABSENT_EVIDENCE_KEYS = {'tables', 'wiki_evidence_present', 'wiki_schema_present'}
# Mirrors distribution/wiki_reverse_absent.py INSPECTED_TABLES: the active
# path's core domain tables, then the Wiki state tables (_STATE_TABLES).
WIKI_ABSENT_TABLES = ('knowledge_spaces', 'knowledge_datasets', 'knowledge_assets',
                      'knowledge_local_wiki_compilations', 'knowledge_local_wiki_schema_pages',
                      'knowledge_local_wiki_schema_log', 'knowledge_wiki_authoring_state',
                      'knowledge_wiki_authoring_pages', 'knowledge_wiki_authoring_commits')
WIKI_ABSENT_CORE = frozenset(WIKI_ABSENT_TABLES[:3])
# The absent attestation has no brain tree; the assembly binds the canonical
# empty inventory digest in its place.
EMPTY_INVENTORY = {'files': {}, 'directories': []}
_MAX_EVIDENCE = 1024 * 1024
_HEX = re.compile(r'[0-9a-f]{64}')


class RollbackEvidenceError(ValueError):
    """A presented artifact, cross-linkage or committed output refuses assembly."""


def _read_canonical(path, limit):
    raw, _ = files._read(path, limit=limit, private=True)
    value = json.loads(raw, object_pairs_hook=control._unique)
    if not isinstance(value, dict) or control.encoded(value) != raw:
        raise RollbackEvidenceError('Protocol evidence must be canonical JSON')
    return value, raw


def _hex64(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _fact(value):
    return (isinstance(value, dict) and set(value) == {'sha256', 'size_bytes'}
            and _hex64(value['sha256']) and type(value['size_bytes']) is int and value['size_bytes'] >= 0)


def _inventory(value):
    if not isinstance(value, dict) or set(value) != {'files', 'directories'}:
        raise RollbackEvidenceError('Committed inventory is invalid')
    files_map, directories = value['files'], value['directories']
    if not isinstance(files_map, dict) or any(not _fact(fact) for fact in files_map.values()):
        raise RollbackEvidenceError('Committed inventory is invalid')
    for name in files_map:
        files._relative(name)
    if not isinstance(directories, list) or any(not isinstance(name, str) for name in directories) \
            or directories != sorted(set(directories)):
        raise RollbackEvidenceError('Committed inventory is invalid')
    for name in directories:
        files._relative(name)
    return value


def _output_identity(plan, root, *, owner):
    identity = plan.get('output_identity')
    # The producing CLI commits writer_authority.identity(output); the artifact
    # must still live at that exact private directory.  Relocation is the
    # audited domain of distribution/installation_path_rebind.py after
    # assignment, never of this assembly.
    if identity != control.identity(root):
        raise RollbackEvidenceError(owner + ' output identity changed')


def _verify_frozen_export(path):
    """Verify the local/frozen_export.py manifest and its committed trees."""
    manifest, raw = _read_canonical(path, files.MAX_JSON)
    if set(manifest) != EXPORT_KEYS or manifest['format'] != EXPORT_FORMAT or manifest['state'] != EXPORT_STATE:
        raise RollbackEvidenceError('Frozen export is not a verified receipt')
    if any(manifest[flag] is not False for flag in EXPORT_FLAGS):
        raise RollbackEvidenceError('Frozen export is not inert')
    plan = manifest['plan']
    if not isinstance(plan, dict) or set(plan) != EXPORT_PLAN_KEYS or plan['format'] != EXPORT_FORMAT:
        raise RollbackEvidenceError('Frozen export plan is invalid')
    # local/frozen_export.py pins plan_sha256 = writer_authority.digest(plan).
    if control.digest(plan) != manifest['plan_sha256']:
        raise RollbackEvidenceError('Frozen export plan commitment mismatch')
    control._operation(plan['operation_id'])
    root = path.parent
    _output_identity(plan, root, owner='Frozen export')
    _inventory(plan['source_inventory'])
    _inventory(manifest['normalized_inventory'])
    # local/frozen_export.py pins the raw Catalog bytes in
    # plan.source_inventory.files['catalog.sqlite3'].
    catalog = plan['source_inventory']['files'].get('catalog.sqlite3')
    if not _fact(catalog):
        raise RollbackEvidenceError('Frozen export has no pinned Catalog fact')
    # distribution/other_catalog_reverse.py refuses a suspended Catalog with
    # live sidecars; the same quiescence gate applies to this binding.
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = root / 'raw' / ('catalog.sqlite3' + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise RollbackEvidenceError('Frozen export Catalog is not quiesced')
    report = manifest['normalized_databases']
    entry = report.get('catalog') if isinstance(report, dict) else None
    committed = entry.get('normalized_catalog') if isinstance(entry, dict) else None
    if not isinstance(committed, dict) or set(committed) != {'digest', 'size'} \
            or not isinstance(committed['digest'], str) or not committed['digest'].startswith('sha256:') \
            or not _hex64(committed['digest'][7:]) or type(committed['size']) is not int or committed['size'] < 0:
        raise RollbackEvidenceError('Frozen export Catalog report is missing')
    if files._inventory(root / 'raw', private=True) != plan['source_inventory']:
        raise RollbackEvidenceError('Frozen export raw content changed')
    normalized = files._inventory(root / 'normalized', private=True)
    if normalized != manifest['normalized_inventory']:
        raise RollbackEvidenceError('Frozen export normalized content changed')
    normalized_catalog = normalized['files'].get('catalog/catalog.sqlite3')
    # local/frozen_export.py normalized_databases.catalog.normalized_catalog
    # commits {digest: 'sha256:'+hex, size} of normalized/catalog/catalog.sqlite3.
    if normalized_catalog is None or committed != {'digest': 'sha256:' + normalized_catalog['sha256'],
                                                   'size': normalized_catalog['size_bytes']}:
        raise RollbackEvidenceError('Frozen export Catalog commitment mismatch')
    return {'manifest': manifest, 'raw': raw, 'root': root, 'catalog_sha256': catalog['sha256'],
            'normalized_catalog_sha256': normalized_catalog['sha256'],
            'operation_id': plan['operation_id']}


def _verify_disposition(path, export):
    """Verify the distribution/other_catalog_reverse.py receipt and its export binding."""
    try:
        loaded = load_disposition(path)
    except ValueError as error:
        raise RollbackEvidenceError(str(error)) from error
    receipt, raw = _read_canonical(path, _MAX_EVIDENCE)
    # load_disposition already forces frozen_export.catalog_sha256 ==
    # target_after_sha256; the remaining bindings tie the receipt to the exact
    # presented export and to the bytes the core/document reverse consumed.
    evidence = receipt['frozen_export']
    # distribution/other_catalog_reverse.py _verify_frozen_export commits
    # frozen_export.manifest_sha256 over the presented export manifest bytes.
    if evidence['manifest_sha256'] != hashlib.sha256(export['raw']).hexdigest():
        raise RollbackEvidenceError('Disposition binds another frozen export')
    # ... and frozen_export.catalog_sha256 over the export's pinned raw Catalog
    # fact (local/frozen_export.py plan.source_inventory.files['catalog.sqlite3']).
    if evidence['catalog_sha256'] != export['catalog_sha256']:
        raise RollbackEvidenceError('Disposition binds another frozen export Catalog')
    return {'receipt': receipt, 'raw': raw, 'loaded': loaded}


def _verify_document(path):
    """Verify the distribution/document_reverse.py v5 manifest and candidate."""
    manifest, raw = _read_canonical(path, files.MAX_JSON)
    if set(manifest) != DOCUMENT_KEYS or manifest['format'] != DOCUMENT_FORMAT or manifest['state'] != DOCUMENT_STATE:
        raise RollbackEvidenceError('Not a verified inactive document candidate receipt')
    if any(manifest[flag] is not False for flag in CANDIDATE_FLAGS):
        raise RollbackEvidenceError('Document candidate receipt is not inert')
    if not _hex64(manifest['catalog_sha256']):
        raise RollbackEvidenceError('Document candidate Catalog commitment is invalid')
    plan = manifest['plan']
    if not isinstance(plan, dict) or set(plan) != DOCUMENT_PLAN_KEYS or plan['format'] != DOCUMENT_FORMAT:
        raise RollbackEvidenceError('Document reverse plan is invalid')
    root = path.parent
    _output_identity(plan, root, owner='Document reverse')
    if plan['candidate_knowledge_root'] != str(root / 'bodies'):
        raise RollbackEvidenceError('Document candidate knowledge root does not match its output identity')
    _inventory(plan['output_inventory'])
    inputs = plan['inputs']
    # distribution/document_reverse.py commits one {path, sha256} entry per
    # snapshot in (source, target_before, target_after) order.
    if not isinstance(inputs, list) or len(inputs) != 3 \
            or any(not isinstance(entry, dict) or set(entry) != {'path', 'sha256'}
                   or not isinstance(entry['path'], str) or not _hex64(entry['sha256']) for entry in inputs):
        raise RollbackEvidenceError('Document reverse input commitments are invalid')
    receipt = manifest['core_receipt']
    if not isinstance(receipt, dict) or set(receipt) != CORE_KEYS \
            or receipt['format'] != CORE_FORMAT or receipt['state'] != CORE_STATE:
        raise RollbackEvidenceError('Embedded core reverse receipt is not verified')
    if any(receipt[flag] is not False for flag in CORE_FLAGS):
        raise RollbackEvidenceError('Embedded core reverse receipt is not inert')
    if not isinstance(receipt['source_revision'], str) or not receipt['source_revision'] \
            or len(receipt['source_revision']) > 200 or receipt['source_revision'] != plan['source_revision']:
        raise RollbackEvidenceError('Core reverse source revision disagrees with the document plan')
    # distribution/core_catalog_reverse.py returns input_sha256 over the same
    # three snapshots and output_sha256 over the candidate Catalog;
    # distribution/document_reverse.py embeds that receipt and pins
    # catalog_sha256 over the same candidate bytes at <output>/catalog.sqlite3.
    if receipt['input_sha256'] != [entry['sha256'] for entry in inputs]:
        raise RollbackEvidenceError('Embedded core reverse receipt binds other snapshots')
    if receipt['output_sha256'] != manifest['catalog_sha256']:
        raise RollbackEvidenceError('Embedded core reverse receipt binds another candidate')
    if files._inventory(root / 'bodies', private=True) != plan['output_inventory']:
        raise RollbackEvidenceError('Document candidate bodies changed')
    if sql._file_digest(root / 'catalog.sqlite3') != manifest['catalog_sha256']:
        raise RollbackEvidenceError('Document candidate Catalog changed')
    return {'manifest': manifest, 'raw': raw, 'root': root, 'plan': plan}


def _verify_wiki(path):
    """Verify the distribution/wiki_reverse.py manifest and brain candidate."""
    manifest, raw = _read_canonical(path, files.MAX_JSON)
    if set(manifest) != WIKI_KEYS or manifest['format'] != WIKI_FORMAT or manifest['state'] != WIKI_STATE:
        raise RollbackEvidenceError('Not a verified inactive Wiki candidate receipt')
    if any(manifest[flag] is not False for flag in CANDIDATE_FLAGS):
        raise RollbackEvidenceError('Wiki candidate receipt is not inert')
    plan = manifest['plan']
    if not isinstance(plan, dict) or set(plan) != WIKI_PLAN_KEYS or plan['format'] != WIKI_FORMAT:
        raise RollbackEvidenceError('Wiki reverse plan is invalid')
    root = path.parent
    _output_identity(plan, root, owner='Wiki reverse')
    _inventory(plan['output_inventory'])
    # distribution/wiki_reverse.py commits its own frozen export binding
    # (inputs.current_workspace); that export is a separate workspace from the
    # Catalog chain's and is verified by the Wiki reverse CLI itself, so only
    # the commitment's shape is re-checked here.
    inputs = plan['inputs']
    if not isinstance(inputs, dict) or set(inputs) != {'source', 'baseline_archive', 'current_workspace'}:
        raise RollbackEvidenceError('Wiki reverse input commitments are invalid')
    if not isinstance(inputs['source'], dict) or set(inputs['source']) != {'path', 'inventory_sha256'} \
            or not isinstance(inputs['source']['path'], str) \
            or not _hex64(inputs['source']['inventory_sha256']):
        raise RollbackEvidenceError('Wiki reverse source commitment is invalid')
    if not isinstance(inputs['baseline_archive'], dict) \
            or set(inputs['baseline_archive']) != {'path', 'manifest_sha256'} \
            or not isinstance(inputs['baseline_archive']['path'], str) \
            or not _hex64(inputs['baseline_archive']['manifest_sha256']):
        raise RollbackEvidenceError('Wiki reverse baseline commitment is invalid')
    if not isinstance(inputs['current_workspace'], dict) \
            or set(inputs['current_workspace']) != {'path', 'manifest_sha256', 'catalog_sha256'} \
            or not isinstance(inputs['current_workspace']['path'], str) \
            or not _hex64(inputs['current_workspace']['manifest_sha256']) \
            or not _hex64(inputs['current_workspace']['catalog_sha256']):
        raise RollbackEvidenceError('Wiki reverse workspace commitment is invalid')
    if not isinstance(plan['source_revision'], str) or not plan['source_revision'] \
            or len(plan['source_revision']) > 200:
        raise RollbackEvidenceError('Wiki reverse source revision is invalid')
    if files._inventory(root / 'brain', private=True) != plan['output_inventory']:
        raise RollbackEvidenceError('Wiki candidate brain changed')
    return {'manifest': manifest, 'raw': raw, 'root': root, 'plan': plan}


def _verify_wiki_absent(path):
    """Verify the distribution/wiki_reverse_absent.py proof-of-absence attestation."""
    manifest, raw = _read_canonical(path, files.MAX_JSON)
    if set(manifest) != WIKI_ABSENT_KEYS or manifest['format'] != WIKI_ABSENT_FORMAT \
            or manifest['state'] != WIKI_ABSENT_STATE:
        raise RollbackEvidenceError('Not a verified absent Wiki attestation')
    if any(manifest[flag] is not False for flag in CANDIDATE_FLAGS):
        raise RollbackEvidenceError('Absent Wiki attestation is not inert')
    plan = manifest['plan']
    if not isinstance(plan, dict) or set(plan) != WIKI_ABSENT_PLAN_KEYS or plan['format'] != WIKI_ABSENT_FORMAT:
        raise RollbackEvidenceError('Absent Wiki attestation plan is invalid')
    root = path.parent
    _output_identity(plan, root, owner='Wiki absence attestation')
    # distribution/wiki_reverse_absent.py commits the same frozen export
    # binding shape as distribution/wiki_reverse.py inputs.current_workspace;
    # that export is verified by the producing CLI itself, so only the
    # commitment's shape is re-checked here.
    inputs = plan['inputs']
    if not isinstance(inputs, dict) or set(inputs) != {'current_workspace'}:
        raise RollbackEvidenceError('Absent Wiki input commitments are invalid')
    workspace = inputs['current_workspace']
    if not isinstance(workspace, dict) or set(workspace) != {'path', 'manifest_sha256', 'catalog_sha256'} \
            or not isinstance(workspace['path'], str) \
            or not _hex64(workspace['manifest_sha256']) or not _hex64(workspace['catalog_sha256']):
        raise RollbackEvidenceError('Absent Wiki workspace commitment is invalid')
    if not isinstance(plan['source_revision'], str) or not plan['source_revision'] \
            or len(plan['source_revision']) > 200:
        raise RollbackEvidenceError('Absent Wiki source revision is invalid')
    absence = plan['absence']
    if not isinstance(absence, dict) or set(absence) != WIKI_ABSENT_EVIDENCE_KEYS \
            or absence['wiki_evidence_present'] is not False or absence['wiki_schema_present'] is not False:
        raise RollbackEvidenceError('Absent Wiki evidence is invalid')
    tables = absence['tables']
    if not isinstance(tables, list) or len(tables) != len(WIKI_ABSENT_TABLES):
        raise RollbackEvidenceError('Absent Wiki evidence is invalid')
    for entry, name in zip(tables, WIKI_ABSENT_TABLES):
        if not isinstance(entry, dict) or set(entry) != {'table', 'present', 'rows'} or entry['table'] != name \
                or type(entry['rows']) is not int or entry['rows'] != 0 \
                or not isinstance(entry['present'], bool) \
                or (name in WIKI_ABSENT_CORE and entry['present'] is not True):
            raise RollbackEvidenceError('Absent Wiki evidence is invalid')
    return {'manifest': manifest, 'raw': raw, 'root': root, 'plan': plan}


def _verify_wiki_artifact(path):
    """Dispatch on the committed format: active Wiki candidate or absent attestation."""
    manifest, _raw = _read_canonical(path, files.MAX_JSON)
    if manifest.get('format') == WIKI_ABSENT_FORMAT:
        return _verify_wiki_absent(path), WIKI_ABSENT_STATE
    return _verify_wiki(path), WIKI_STATE


def _publish(path, data):
    part = path.parent / (path.name + '.part')
    if part.exists() or part.is_symlink():
        files._read(part, limit=_MAX_EVIDENCE, private=True)
        part.unlink()
        _sync_directory(path.parent)
    if path.exists():
        existing, _ = files._read(path, limit=_MAX_EVIDENCE, private=True)
        if existing != data:
            raise RollbackEvidenceError('Existing rollback evidence disagrees')
        return True
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(part, path)
    part.unlink()
    _sync_directory(path.parent)
    return False


def assemble_rollback_evidence(frozen_export_manifest, other_catalog_disposition,
                               document_reverse_manifest, wiki_reverse_manifest, output):
    """Verify the four reverse-step artifacts and publish the evidence file.

    All four artifacts must be the exact files the step CLIs produced, still at
    their committed locations: the frozen export manifest, the non-core Catalog
    disposition receipt, the document_reverse v5 manifest and the wiki_reverse
    manifest (or, for a Wiki-less installation, the wiki_reverse_absent
    attestation).  An exact retry publishes byte-identical evidence; a
    disagreeing existing evidence file refuses.
    """
    export_path, disposition_path, document_path, wiki_path, destination = [
        files._path(value) for value in (frozen_export_manifest, other_catalog_disposition,
                                         document_reverse_manifest, wiki_reverse_manifest, output)]
    if destination.exists() and not destination.is_file():
        raise RollbackEvidenceError('Invalid rollback evidence path')
    if not destination.parent.is_dir():
        raise RollbackEvidenceError('Evidence parent is unavailable')
    if len({str(path) for path in (export_path, disposition_path, document_path, wiki_path, destination)}) != 5:
        raise RollbackEvidenceError('Rollback evidence inputs must be distinct')
    export = _verify_frozen_export(export_path)
    disposition = _verify_disposition(disposition_path, export)
    document = _verify_document(document_path)
    wiki, wiki_state = _verify_wiki_artifact(wiki_path)
    for root in (export['root'], document['root'], wiki['root']):
        if destination == root or destination.is_relative_to(root):
            raise RollbackEvidenceError('Rollback evidence must live outside the step outputs')
    # Cross-step Catalog linkage: distribution/other_catalog_reverse.py
    # target_before_sha256/target_after_sha256 must equal the snapshots the
    # core/document reverse consumed (distribution/document_reverse.py
    # plan.inputs[1]/plan.inputs[2], re-committed as
    # distribution/core_catalog_reverse.py input_sha256[1]/input_sha256[2]).
    inputs = document['plan']['inputs']
    loaded = disposition['loaded']
    if loaded['target_before_sha256'] != inputs[1]['sha256']:
        raise RollbackEvidenceError('Disposition and document reverse disagree on the target-before Catalog')
    if loaded['target_after_sha256'] != inputs[2]['sha256']:
        raise RollbackEvidenceError('Disposition and document reverse disagree on the target-after Catalog')
    # Source snapshot identity: the document plan's source is a legacy Catalog
    # file and the Wiki plan's source is a brain tree (or, in the absent
    # attestation, the same operator-committed revision standing for a brain
    # tree that never existed), so byte identity is not equatable across the
    # two formats; the shared source snapshot revision
    # (distribution/wiki_reverse.py or distribution/wiki_reverse_absent.py
    # plan.source_revision vs distribution/document_reverse.py
    # plan.source_revision and the embedded core receipt) is the strongest
    # committed agreement.
    if wiki['plan']['source_revision'] != document['plan']['source_revision']:
        raise RollbackEvidenceError('Wiki and document reverse disagree on the source snapshot revision')
    # Operation identity exists only on the frozen export plan
    # (local/frozen_export.py plan.operation_id, the suspension operation); no
    # other artifact format commits one, so there is no second carrier to bind.
    artifacts = []
    for role, state, raw in (('frozen_export', EXPORT_STATE, export['raw']),
                             ('other_catalog_disposition', DISPOSITION_STATE, disposition['raw']),
                             ('document_reverse', DOCUMENT_STATE, document['raw']),
                             ('wiki_reverse', wiki_state, wiki['raw'])):
        artifacts.append({'role': role, 'state': state, 'sha256': hashlib.sha256(raw).hexdigest()})
    linkages = {'frozen_export_manifest_sha256': artifacts[0]['sha256'],
                'frozen_export_catalog_sha256': export['catalog_sha256'],
                'frozen_export_normalized_catalog_sha256': export['normalized_catalog_sha256'],
                'target_before_sha256': loaded['target_before_sha256'],
                'target_after_sha256': loaded['target_after_sha256'],
                'document_candidate_catalog_sha256': document['manifest']['catalog_sha256']}
    if wiki_state == WIKI_ABSENT_STATE:
        linkages['wiki_domain_attested_absent'] = True
    wiki_inventory = wiki['plan']['output_inventory'] if wiki_state == WIKI_STATE else EMPTY_INVENTORY
    evidence = {'format': FORMAT, 'state': STATE,
                'operation_id': export['operation_id'],
                'source_revision': document['plan']['source_revision'],
                'artifacts': artifacts,
                'linkages': linkages,
                'artifact_digests': {
                    'frozen_export_raw_inventory_sha256': control.digest(export['manifest']['plan']['source_inventory']),
                    'frozen_export_normalized_inventory_sha256': control.digest(export['manifest']['normalized_inventory']),
                    'document_bodies_inventory_sha256': control.digest(document['plan']['output_inventory']),
                    'wiki_brain_inventory_sha256': control.digest(wiki_inventory)},
                **{flag: False for flag in FLAGS}}
    data = control.encoded(evidence)
    if len(data) > _MAX_EVIDENCE:
        raise RollbackEvidenceError('Rollback evidence exceeds its budget')
    # The presented artifacts must not drift while the evidence is derived.
    for path, raw, limit in ((export_path, export['raw'], files.MAX_JSON),
                             (disposition_path, disposition['raw'], _MAX_EVIDENCE),
                             (document_path, document['raw'], files.MAX_JSON),
                             (wiki_path, wiki['raw'], files.MAX_JSON)):
        if files._read(path, limit=limit, private=True)[0] != raw:
            raise RollbackEvidenceError('Presented artifact changed during assembly')
    if sql._file_digest(document['root'] / 'catalog.sqlite3') != document['manifest']['catalog_sha256']:
        raise RollbackEvidenceError('Document candidate Catalog changed during assembly')
    idempotent = _publish(destination, data)
    return dict(evidence, idempotent=idempotent, evidence_sha256=hashlib.sha256(data).hexdigest())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('frozen-export-manifest', 'other-catalog-disposition',
                 'document-reverse-manifest', 'wiki-reverse-manifest', 'output'):
        parser.add_argument('--' + name, required=True)
    args = parser.parse_args(argv)
    try:
        result = assemble_rollback_evidence(args.frozen_export_manifest, args.other_catalog_disposition,
                                            args.document_reverse_manifest, args.wiki_reverse_manifest,
                                            args.output)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'rollback_evidence_rejected',
                          **{flag: False for flag in FLAGS}}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
