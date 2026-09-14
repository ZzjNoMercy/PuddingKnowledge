"""Owned document bootstrap from an explicit, offline migration candidate.

The candidate lock serializes cooperating publishers. This is not an active
installation writer fence or authentication of user-supplied migration inputs.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
from urllib.parse import quote


class MigratedDocumentWorkspaceError(RuntimeError):
    pass


_PROVIDER = 'knowledge_migrated_documents'
_FORMAT = 'puddingknowledge-document-migration/v3'
_ID = re.compile(r'[A-Za-z0-9._:-]{1,160}')
_BLOB = re.compile(r'blobs/[0-9a-f]{64}')
_MAX_CATALOG = 64 * 1024 * 1024
_MAX_BLOB = 32 * 1024 * 1024
_MAX_TOTAL = 256 * 1024 * 1024


def _real(path):
    path = Path(path).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise MigratedDocumentWorkspaceError('workspace paths must be absolute without parent traversal')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise MigratedDocumentWorkspaceError('workspace path contains a symlink')
    return path


def _read(path, limit):
    path = _real(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit:
            raise MigratedDocumentWorkspaceError('workspace file must be private, bounded and unlinked')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        after = path.stat()
        if len(data) > limit or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise MigratedDocumentWorkspaceError('workspace file changed during read')
        return data
    finally:
        os.close(fd)


def _digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(data, object_pairs_hook=pairs)
        if not isinstance(value, dict): raise ValueError('not an object')
        return value
    except (ValueError, UnicodeError) as error:
        raise MigratedDocumentWorkspaceError('workspace manifest is invalid') from error


def _write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())


def _sync(path):
    fd = os.open(path, os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def _candidate_lock(candidate):
    candidate = _real(candidate)
    if not candidate.is_dir() or candidate.stat().st_mode & 0o077:
        raise MigratedDocumentWorkspaceError('candidate must be a private directory')
    lock = candidate / '.migration.lock'
    _read(lock, 1024)
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise MigratedDocumentWorkspaceError('candidate lock is invalid')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error: raise MigratedDocumentWorkspaceError('candidate is busy') from error
        yield
    finally:
        os.close(fd)


def _facts(catalog, bindings, *, exact):
    """Validate the selected immutable migration facts; allow later new assets."""
    uri = 'file:' + quote(str(catalog), safe='/') + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as db:
        db.row_factory = sqlite3.Row
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise MigratedDocumentWorkspaceError('owned Catalog integrity failed')
        spaces = {str(r[0]) for r in db.execute('SELECT id FROM knowledge_spaces')}
        rows = db.execute('SELECT id,space_id,content_digest,source_uri FROM knowledge_assets').fetchall()
        if len(rows) > 100000: raise MigratedDocumentWorkspaceError('Catalog has too many assets')
        assets = {str(r['id']): dict(r) for r in rows}
        if (exact and set(assets) != set(bindings)) or not set(bindings) <= set(assets):
            raise MigratedDocumentWorkspaceError('candidate Asset bindings are incomplete')
        selected = {}
        for key, relative in bindings.items():
            row = assets[key]
            if row['space_id'] not in spaces or not _ID.fullmatch(row['space_id']) or row['content_digest'] != 'sha256:' + relative[6:] or row['source_uri'] != f"knowledge://spaces/{row['space_id']}/assets/{key}":
                raise MigratedDocumentWorkspaceError('candidate Asset facts disagree with binding')
            selected[key] = row
        collections = []
        covered = set()
        for row in db.execute('SELECT id,space_id,version,asset_ids,capabilities FROM knowledge_datasets'):
            ids, capabilities = json.loads(row['asset_ids']), json.loads(row['capabilities'])
            if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
                raise MigratedDocumentWorkspaceError('Collection asset list is invalid')
            if not set(ids).intersection(bindings): continue
            if not ids or not set(ids) <= set(bindings) or any(assets[i]['space_id'] != row['space_id'] for i in ids) or 'document_rag_query' not in capabilities:
                raise MigratedDocumentWorkspaceError('Collection crosses migrated Asset scope')
            if not all(isinstance(row[k], str) and _ID.fullmatch(row[k]) for k in ('id','space_id','version')):
                raise MigratedDocumentWorkspaceError('Collection identity is invalid')
            if not exact:
                # Index management may legitimately replace lexical routing.
                # The owned Catalog remains that mutable routing authority.
                binding_row = db.execute('SELECT binding_json FROM knowledge_collection_bindings WHERE space_id=? AND collection_id=? AND collection_version=? AND capability=?',
                    (row['space_id'], row['id'], row['version'], 'document_rag_query')).fetchone()
                binding = _json(binding_row[0]) if binding_row else {}
                if set(binding) != {'provider_id'} or binding['provider_id'] not in {_PROVIDER, 'knowledge_local_vector'}:
                    raise MigratedDocumentWorkspaceError('migrated Collection provider is unavailable')
            collections.append({'id': row['id'], 'space_id': row['space_id'], 'version': row['version'], 'asset_ids': ids})
            covered.update(ids)
        if covered != set(bindings): raise MigratedDocumentWorkspaceError('migrated Assets lack a document Collection')
    return {'assets': selected, 'collections': sorted(collections, key=lambda r: (r['space_id'], r['id'], r['version']))}


def _bindings(raw):
    if not isinstance(raw, dict) or not 1 <= len(raw) <= 5000 or any(
        not isinstance(k, str) or not _ID.fullmatch(k) or not isinstance(v, str) or not _BLOB.fullmatch(v) for k,v in raw.items()):
        raise MigratedDocumentWorkspaceError('document bindings are invalid')
    return raw


def _original_bindings(raw, bodies):
    if not isinstance(raw, dict) or not set(raw) <= set(bodies):
        raise MigratedDocumentWorkspaceError('original bindings are invalid')
    return _bindings(raw) if raw else {}


def _verify_original_catalog(path, originals):
    _read(path, _MAX_CATALOG)
    uri = 'file:' + quote(str(path), safe='/') + '?mode=ro&immutable=1'
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute('SELECT id,source_type,mime_type,metadata_json FROM knowledge_assets').fetchall()
    expected = {}
    for identity, source_type, mime_type, raw in rows:
        metadata = json.loads(raw)
        if source_type.startswith('pdf_') or metadata.get('mode') == 'multimodal_pdf':
            digest = metadata.get('original_sha256', '')
            if not isinstance(digest, str): raise MigratedDocumentWorkspaceError('invalid PDF original digest')
            relative = 'blobs/'+digest.removeprefix('sha256:').lower()
            if not _BLOB.fullmatch(relative) or mime_type != 'text/markdown' or metadata.get('mode') != 'multimodal_pdf':
                raise MigratedDocumentWorkspaceError('invalid PDF representation')
            expected[identity] = relative
    if expected != originals:
        raise MigratedDocumentWorkspaceError('PDF original coverage differs from Catalog')


def load_migrated_workspace(root, manifest):
    required = {'version','owner','kind','catalog','blob_root','provider_id','file_bindings','facts','pages','activation_allowed'}
    if manifest.get('version') in (3,4): required.add('original_bindings')
    if manifest.get('version') == 4: required.update(('document_tree','tree_bindings'))
    if set(manifest) != required or manifest['version'] not in (2,3,4) or manifest['kind'] != 'migrated_documents' or manifest['catalog'] != 'catalog.sqlite3' or manifest['blob_root'] != 'blobs' or manifest['provider_id'] != _PROVIDER or manifest['activation_allowed'] is not False:
        raise MigratedDocumentWorkspaceError('migrated workspace manifest is invalid')
    if manifest['version'] != 4 and ((root/'resources').exists() or (root/'resources').is_symlink()):raise MigratedDocumentWorkspaceError('Unregistered document resources')
    bindings = _bindings(manifest['file_bindings'])
    originals = _original_bindings(manifest.get('original_bindings', {}), bindings)
    _read(root/'catalog.sqlite3', _MAX_CATALOG)
    # A stopped workspace must have a checkpointed Catalog; never ignore WAL.
    for suffix in ('-wal','-shm','-journal'):
        sidecar = root/('catalog.sqlite3'+suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise MigratedDocumentWorkspaceError('migrated Catalog must be checkpointed before restart')
    _verify_original_catalog(root/'catalog.sqlite3', originals)
    if _facts(root/'catalog.sqlite3', bindings, exact=False) != manifest['facts']:
        raise MigratedDocumentWorkspaceError('owned migration facts changed')
    blobs = _real(root/'blobs')
    if not blobs.is_dir() or blobs.stat().st_mode & 0o077:
        raise MigratedDocumentWorkspaceError('owned blob directory is invalid')
    if {p.name for p in blobs.iterdir()} != {v[6:] for v in [*bindings.values(), *originals.values()]}:
        raise MigratedDocumentWorkspaceError('owned blobs contain unregistered files')
    total = 0
    for relative in set([*bindings.values(), *originals.values()]):
        data = _read(root/relative, _MAX_BLOB); total += len(data)
        if total > _MAX_TOTAL or _digest(data) != 'sha256:'+relative[6:]:
            raise MigratedDocumentWorkspaceError('owned blob digest mismatch')
    paths = {key: root/relative for key,relative in bindings.items()}
    if manifest['version'] == 4:
        from ..distribution.document_tree import validate_owned_tree
        paths = validate_owned_tree(root,manifest['document_tree'],manifest['tree_bindings'],bindings)
    return {'catalog': root/'catalog.sqlite3', 'file_bindings': paths, 'document_bindings': dict(paths),
        'space_ids': sorted({r['space_id'] for r in manifest['facts']['assets'].values()}), 'pages': 0,
        'document_resources': {'root':str(root/'resources'),'tree':manifest['document_tree'],'bindings':manifest['tree_bindings'],'facts':manifest['facts']} if manifest['version']==4 else None}


def bootstrap_migrated_documents(candidate, state_dir):
    """Initialize only a new state directory; caller owns its workspace lock."""
    candidate, root = _real(candidate), _real(state_dir)
    if candidate == root or candidate.is_relative_to(root) or root.is_relative_to(candidate):
        raise MigratedDocumentWorkspaceError('candidate and owned state must be disjoint')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(p.name != '.workspace.lock' for p in root.iterdir()):
        raise MigratedDocumentWorkspaceError('workspace must be empty')
    with _candidate_lock(candidate):
        manifest = _json(_read(candidate/'manifest.json', 1024*1024))
        required = {'format','state','plan','asset_bindings','files','activation_allowed','complete_installation_migration'}
        if set(manifest) != required or manifest['format'] not in (_FORMAT, 'puddingknowledge-document-migration/v1', 'puddingknowledge-document-migration/v2') or manifest['state'] != 'verified_inactive' or manifest['activation_allowed'] is not False or manifest['complete_installation_migration'] is not False:
            raise MigratedDocumentWorkspaceError('candidate is not a completed inactive migration')
        bindings = _bindings(manifest['asset_bindings'])
        originals = _original_bindings(manifest['plan'].get('original_bindings', {}), bindings)
        tree=manifest['plan'].get('document_tree'); tree_bindings=manifest['plan'].get('tree_bindings')
        if manifest['format'] == _FORMAT and (tree is None or tree_bindings is None):raise MigratedDocumentWorkspaceError('Document tree missing')
        tree_files={} if tree is None else tree['files']
        files = manifest['files']
        if not isinstance(files, dict) or set(files) != {'catalog.sqlite3', *bindings.values(), *originals.values(), *('resources/'+name for name in tree_files)}:
            raise MigratedDocumentWorkspaceError('candidate inventory is invalid')
        for p in candidate.iterdir():
            if p.name not in {'catalog.sqlite3','blobs','resources','manifest.json','checkpoint.json','.migration.lock'}:
                raise MigratedDocumentWorkspaceError('candidate contains unexpected data')
        if (candidate/'checkpoint.json').exists(): _read(candidate/'checkpoint.json', 1024*1024)
        blob_root = _real(candidate/'blobs')
        if not blob_root.is_dir() or blob_root.stat().st_mode & 0o077 or {p.name for p in blob_root.iterdir()} != {v[6:] for v in [*bindings.values(), *originals.values()]}:
            raise MigratedDocumentWorkspaceError('candidate blobs are invalid')
        if tree is not None:
            from ..distribution.document_tree import validate_owned_tree
            validate_owned_tree(candidate,tree,tree_bindings,bindings)
        elif (candidate/'resources').exists():raise MigratedDocumentWorkspaceError('Unregistered document resources')
        _write(root/'.initializing', b'document bootstrap\n'); _sync(root)
        staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=root))
        try:
            (staging/'blobs').mkdir(mode=0o700)
            if tree is not None:
                (staging/'resources').mkdir(mode=0o700)
                for name in sorted(tree['directories'],key=lambda value:(value.count('/'),value)):(staging/'resources'/name).mkdir(mode=0o700)
            total = 0
            for relative, expected in files.items():
                data = _read(candidate/relative, _MAX_CATALOG if relative == 'catalog.sqlite3' else _MAX_BLOB)
                total += len(data)
                if total > _MAX_TOTAL or _digest(data) != expected or (relative.startswith('blobs/') and expected != 'sha256:'+relative[6:]):
                    raise MigratedDocumentWorkspaceError('candidate file digest mismatch')
                _write(staging/relative, data)
            facts = _facts(staging/'catalog.sqlite3', bindings, exact=True)
            _verify_original_catalog(staging/'catalog.sqlite3', originals)
            with sqlite3.connect(staging/'catalog.sqlite3') as db:
                for row in facts['collections']:
                    db.execute('INSERT OR REPLACE INTO knowledge_collection_bindings(space_id,collection_id,collection_version,capability,binding_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                        (row['space_id'],row['id'],row['version'],'document_rag_query',json.dumps({'provider_id':_PROVIDER}),'',''))
                db.commit()
            _sync(staging/'catalog.sqlite3'); _sync(staging/'blobs')
            owned = {'version':2,'owner':'puddingknowledge-local','kind':'migrated_documents','catalog':'catalog.sqlite3',
                'blob_root':'blobs','provider_id':_PROVIDER,'file_bindings':bindings,'facts':facts,'pages':0,'activation_allowed':False}
            if originals: owned.update(version=3, original_bindings=originals)
            if tree is not None:
                owned.update(version=4,original_bindings=originals,document_tree=tree,tree_bindings=tree_bindings)
                validate_owned_tree(staging,tree,tree_bindings,bindings)
                for name in sorted(tree['directories'],key=lambda value:-value.count('/')):_sync(staging/'resources'/name)
                _sync(staging/'resources')
            _write(staging/'workspace.json', json.dumps(owned, sort_keys=True).encode())
            for name in ['catalog.sqlite3','blobs','workspace.json', *(['resources'] if tree is not None else [])]: os.rename(staging/name, root/name)
            _sync(root)
            (root/'.initializing').unlink(); _sync(root)
            return load_migrated_workspace(root, owned)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
