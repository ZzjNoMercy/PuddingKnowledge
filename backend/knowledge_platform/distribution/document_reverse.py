"""Materialize offline document bodies and an inactive old-schema Catalog.

No writer is switched. The candidate's absolute body bindings are tied to this
output directory; relocating or activating it requires a separate audited step.
"""
from contextlib import ExitStack
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from . import wiki_archive as files, sqlite_reverse_delta as sql
from .core_catalog_reverse import build_core_catalog_reverse, _rows
from ..local import writer_authority as control
from ..local.workspace_freeze import _sync_directory

FORMAT = 'puddingknowledge-document-reverse/v1'
EXTENSIONS = {'text/markdown': '.md', 'text/plain': '.txt', 'application/pdf': '.pdf',
              'text/csv': '.csv', 'application/json': '.json', 'text/html': '.html'}


def _json_file(path):
    raw, _ = files._read(path, limit=files.MAX_JSON, private=True)
    return json.loads(raw, object_pairs_hook=control._unique)


def _copy(source, destination, fact):
    part = Path(str(destination) + '.reverse-part')
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        files._check(os.fstat(descriptor), private=True)
        os.ftruncate(descriptor, 0)
        _, actual = files._read(source, destination=descriptor)
        if actual != fact:
            raise ValueError('Reverse input changed during copy')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(part, destination)
    _sync_directory(destination.parent)


def prepare_document_reverse(source_snapshot, target_before, target_after, body_root, bindings,
                             output, *, source_revision, _after_copy=None):
    paths = [sql._path(value) for value in (source_snapshot, target_before, target_after)]
    root, stage = [files._path(value) for value in (body_root, output)]
    files._check(root.stat(), directory=True)
    if root == stage or root.is_relative_to(stage) or stage.is_relative_to(root) or any(path.is_relative_to(stage) for path in paths):
        raise ValueError('Reverse output overlaps input')
    if not isinstance(bindings, dict) or len(bindings) > 5000:
        raise ValueError('Invalid body bindings')
    input_digests = [sql._file_digest(path) for path in paths]
    # SQLite sees only private copies, even when an offline header uses WAL.
    with tempfile.TemporaryDirectory(prefix='document-reverse-inspect-') as temporary:
        copied = Path(temporary).resolve()/'catalog.sqlite3'
        if sql._file_digest(paths[2], copy_to=copied) != input_digests[2]:
            raise ValueError("Current Catalog changed during inspection")
        assets = _rows(copied, ('knowledge_assets',))['knowledge_assets']
    documents = {row['id']: row for row in assets if row['kind'] == 'document'}
    if set(bindings) != set(documents):
        raise ValueError('Every current document requires exactly one body binding')
    facts, sources, total = {}, {}, 0
    for asset_id in sorted(documents):
        relative = bindings[asset_id]
        if not isinstance(relative, str) or not relative or '\\' in relative:
            raise ValueError('Invalid relative body path')
        path = Path(relative)
        if path.is_absolute() or '..' in path.parts or path.as_posix() != relative:
            raise ValueError('Body path escaped snapshot')
        source = root/path
        _, fact = files._read(source)
        total += fact['size_bytes']
        if total > files.MAX_TOTAL:
            raise ValueError('Document body budget exceeded')
        asset = documents[asset_id]
        if asset['content_digest'] != 'sha256:' + fact['sha256'] or asset['revision'] != asset['content_digest']:
            raise ValueError('Body digest or content revision mismatch')
        extension = EXTENSIONS.get(asset['mime_type'], '.bin')
        name = fact['sha256'] + extension
        facts[asset_id] = {'input_relative': relative, 'output_relative': 'bodies/'+name, **fact}
        sources[name] = source
    if any(sql._file_digest(path) != digest for path, digest in zip(paths, input_digests)):
        raise ValueError("Catalog changed after body inspection")
    if not stage.exists():
        stage.mkdir(mode=0o700); _sync_directory(stage.parent)
    stage_identity = control.identity(stage)
    with control.lock(stage, exclusive=True) as lease:
        lease_identity = os.fstat(lease)
        plan = {'format': FORMAT, 'source_revision': source_revision,
                'inputs': [{'path': str(path), 'sha256': digest} for path, digest in zip(paths, input_digests)],
                'body_root': str(root), 'bodies': facts, 'output_identity': stage_identity}
        base = {'format': FORMAT, 'plan': plan, 'state': 'copying', 'activation_allowed': False,
                'rollback_completed': False, 'credential_continuity_verified': False,
                'indexes_rebuilt': False, 'installation_path_rebound': False}
        if len(control.encoded(base)) > files.MAX_JSON:
            raise ValueError('Reverse manifest budget exceeded')
        marker = stage/'manifest.json'; previous = None
        if marker.exists() or marker.is_symlink():
            previous = _json_file(marker)
            expected_keys = set(base) | ({'core_receipt','identity_map','catalog_sha256'} if previous.get('state') == 'verified_inactive_documents' else set())
            if set(previous) != expected_keys or previous['state'] not in ('copying','verified_inactive_documents') or any(not sql._json(previous[key]) == sql._json(value) for key,value in base.items() if key != 'state'):
                raise ValueError('Reverse plan changed')
        elif any(p.name != '.writer-authority.lock' for p in stage.iterdir()):
            raise ValueError('Unowned reverse output')
        else:
            control._replace(marker, base)
        complete = previous is not None and previous['state'] == 'verified_inactive_documents'
        for entry in stage.iterdir():
            if entry.name in {'.writer-authority.lock','manifest.json','bodies','catalog.sqlite3','catalog.sqlite3.reverse-part'}:
                continue
            if re.fullmatch(r'\.manifest.json\.tmp-[a-f0-9]{16}',entry.name):
                files._check(entry.lstat(),private=True); continue
            if re.fullmatch(r'\.reverse-work-[a-z0-9_]{8}',entry.name):
                files._inventory(entry,private=True); continue
            raise ValueError('Unknown reverse output entry')
        body_dir = stage/'bodies'
        expected = {fact['output_relative'].split('/',1)[1]: {key:fact[key] for key in ('sha256','size_bytes')} for fact in facts.values()}
        if complete:
            if files._inventory(body_dir,private=True) != {'files':expected,'directories':[]}:
                raise ValueError('Completed reverse bodies changed')
            if files._read(stage/'catalog.sqlite3',private=True)[1]['sha256'] != previous['catalog_sha256'] or (stage/'catalog.sqlite3.reverse-part').exists():
                raise ValueError('Completed reverse Catalog changed')
        if not body_dir.exists():
            body_dir.mkdir(mode=0o700); _sync_directory(stage)
        files._check(body_dir.lstat(),directory=True,private=True)
        for entry in body_dir.iterdir():
            if entry.name not in expected and not (entry.name.endswith('.reverse-part') and entry.name.removesuffix('.reverse-part') in expected):
                raise ValueError('Unknown reverse body')
            files._check(entry.lstat(),private=True)
        if not complete:
            for name,fact in expected.items():
                target = body_dir/name
                if target.exists():
                    if files._read(target,private=True)[1] != fact:
                        raise ValueError('Reverse body changed')
                else:
                    _copy(sources[name],target,fact)
                    if _after_copy:_after_copy(name)
            if files._inventory(body_dir,private=True) != {'files':expected,'directories':[]}:
                raise ValueError('Reverse body inventory mismatch')
            from .document_reverse_plan import plan_document_reverse
            identities = {}
            def transform(legacy,before,after):
                body_bindings = {asset: {'storage_path':str(stage/fact['output_relative']),
                                        'sha256':fact['sha256'],'size_bytes':fact['size_bytes']} for asset,fact in facts.items()}
                prepared, baseline, normalized, mapping = plan_document_reverse(legacy,before,after,source_revision,body_bindings)
                identities.update(mapping)
                return prepared,baseline,normalized
            with tempfile.TemporaryDirectory(prefix='.reverse-work-',dir=stage) as temporary:
                candidate=Path(temporary)/'catalog.sqlite3'
                receipt=build_core_catalog_reverse(*paths,candidate,source_revision=source_revision,_document_transform=transform)
                _, fact=files._read(candidate,private=True)
                target=stage/'catalog.sqlite3'
                if target.exists():
                    if files._read(target,private=True)[1] != fact:
                        raise ValueError('Interrupted reverse Catalog differs')
                else:_copy(candidate,target,fact)
                if _after_copy:_after_copy('catalog.sqlite3')
            result={**base,'state':'verified_inactive_documents','core_receipt':receipt,
                    'identity_map':identities,'catalog_sha256':fact['sha256']}
        else:
            result=previous
        for path,digest in zip(paths,input_digests):
            sql._path(path)
            if sql._file_digest(path) != digest:
                raise ValueError('Reverse Catalog input changed')
        for asset,fact in facts.items():
            if files._read(root/fact['input_relative'])[1] != {key:fact[key] for key in ('sha256','size_bytes')}:
                raise ValueError('Reverse body input changed')
        if files._inventory(body_dir,private=True) != {'files':expected,'directories':[]} or files._read(stage/'catalog.sqlite3',private=True)[1]['sha256'] != result['catalog_sha256']:
            raise ValueError('Reverse output changed before commit')
        current=(stage/'.writer-authority.lock').lstat()
        if control.identity(stage)!=stage_identity or (current.st_dev,current.st_ino)!=(lease_identity.st_dev,lease_identity.st_ino):
            raise ValueError('Reverse control identity changed')
        if len(control.encoded(result)) > files.MAX_JSON:
            raise ValueError("Completed reverse manifest exceeds budget")
        if not complete:control._replace(marker,result)
        return {'format':FORMAT,'state':result['state'],'plan_sha256':control.digest(plan),
                'identity_map':result['identity_map'],'document_count':len(facts),'catalog_sha256':result['catalog_sha256'],
                'idempotent':complete,'document_bodies_materialized':True,'activation_allowed':False,
                'rollback_completed':False,'credential_continuity_verified':False,
                'indexes_rebuilt':False,'installation_path_rebound':False}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source-snapshot','target-before','target-after','body-root','bindings','output','source-revision'):
        parser.add_argument('--'+name,required=True)
    args=vars(parser.parse_args(argv))
    try:
        args['bindings']=_json_file(Path(args['bindings']))
        result=prepare_document_reverse(**args)
    except Exception:
        print(json.dumps({'format':FORMAT,'status':'error','error_code':'document_reverse_rejected','activation_allowed':False}));return 1
    print(json.dumps(result,sort_keys=True));return 0


if __name__=='__main__':raise SystemExit(main())
