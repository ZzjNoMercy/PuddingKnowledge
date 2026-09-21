"""Preserve and verify an offline Wiki brain root; never activate or execute it."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat

FORMAT = 'puddingknowledge-wiki-archive/v1'
MAX_FILES = 50_000
MAX_FILE = 128 * 1024 * 1024
MAX_TOTAL = 2 * 1024**3
MAX_JSON = 32 * 1024 * 1024
FLAGS = {'archive_only': True, 'activation_allowed': False,
         'complete_installation_migration': False, 'wiki_semantics_verified': False}
TOKEN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$')


def _path(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Archive paths must be absolute and normalized')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Archive refuses path links')
    return path


@contextmanager
def _directory(path):
    path = _path(path)
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
        yield fd
    finally:
        os.close(fd)


def _check(info, *, directory=False, private=False, links=1):
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Unsupported archive object')
    if not directory and info.st_nlink != links:
        raise ValueError('Archive refuses hardlinks')
    if private and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise ValueError('Archive object must be owned and private')


def _entity(info):
    return info.st_dev, info.st_ino


def _read(path, *, limit=MAX_FILE, private=False, links=1, destination=None):
    path = _path(path)
    with _directory(path.parent) as parent:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd); _check(before, private=private, links=links)
            if before.st_size > limit: raise ValueError('Archive file exceeds budget')
            chunks = []; size = 0; digest = hashlib.sha256()
            while chunk := os.read(fd, min(1024 * 1024, limit + 1 - size)):
                size += len(chunk)
                if size > limit: raise ValueError('Archive file exceeds budget')
                digest.update(chunk)
                if destination is None: chunks.append(chunk)
                else:
                    view = memoryview(chunk)
                    while view: view = view[os.write(destination, view):]
            after = os.fstat(fd)
            if (_entity(before), before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    _entity(after), after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('Archive file changed during read')
            if _entity(after) != _entity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)):
                raise ValueError('Archive path changed during read')
            return b''.join(chunks), {'sha256': digest.hexdigest(), 'size_bytes': size}
        finally: os.close(fd)


def _inventory(root, *, private=False):
    files = {}; directories = []; total = 0; count = 0
    def walk(fd, prefix):
        nonlocal total, count
        _check(os.fstat(fd), directory=True, private=private)
        names = os.listdir(fd)
        count += len(names)
        if count > MAX_FILES: raise ValueError('Archive entry count exceeds budget')
        for name in sorted(names):
            relative = '/'.join((*prefix, name)); _relative(relative)
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    if _entity(info) != _entity(os.fstat(child)): raise ValueError('Directory changed')
                    directories.append(relative); walk(child, (*prefix, name))
                    if _entity(info) != _entity(os.stat(name, dir_fd=fd, follow_symlinks=False)):
                        raise ValueError('Directory changed')
                finally: os.close(child)
            else:
                _check(info, private=private)
                if total + info.st_size > MAX_TOTAL: raise ValueError('Archive exceeds budget')
                _, fact = _read(root / relative, private=private)
                total += fact['size_bytes']
                if total > MAX_TOTAL: raise ValueError('Archive exceeds budget')
                files[relative] = fact
    with _directory(root) as fd:
        entity = _entity(os.fstat(fd)); walk(fd, ())
    with _directory(root) as fd:
        if entity != _entity(os.fstat(fd)): raise ValueError('Archive root changed')
    return {'files': files, 'directories': sorted(directories)}


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid relative path')
    if any(p in {'', '.', '..'} for p in value.split('/')):
        raise ValueError('Invalid relative path')
    return value


def _encode(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
    if len(data) > MAX_JSON: raise ValueError('Archive metadata exceeds budget')
    return data


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value: raise ValueError('Duplicate JSON key')
        value[key] = item
    return value


def _json(data):
    return json.loads(data, object_pairs_hook=_pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))


def _metadata(path):
    data, _ = _read(path, private=True, limit=MAX_JSON)
    value = _json(data)
    if not isinstance(value, dict) or data != _encode(value): raise ValueError('Noncanonical metadata')
    return value


def _raw(root, inventory):
    # An entirely empty brain root holds no Raw snapshots; the absent journal
    # is vacuously consistent. Any preserved content without it still refuses.
    if not inventory['files'] and not inventory['directories']: return
    data, fact = _read(root / 'raw/manifest.jsonl', limit=MAX_JSON)
    if inventory['files'].get('raw/manifest.jsonl') != fact: raise ValueError('Raw manifest changed')
    seen = set()
    for line in data.decode('utf-8').splitlines():
        if not line.strip(): continue
        row = _json(line)
        if not isinstance(row, dict): raise ValueError('Invalid Raw record')
        path = _relative(row.get('snapshot_path'))
        if path in seen: raise ValueError('Duplicate Raw snapshot path')
        seen.add(path)
        digest = row.get('sha256'); size = row.get('size_bytes')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Invalid Raw digest')
        if type(size) is not int or size < 0: raise ValueError('Invalid Raw size')
        if inventory['files'].get('raw/' + path) != {'sha256': digest, 'size_bytes': size}:
            raise ValueError('Raw snapshot integrity mismatch')


def _sync(path):
    with _directory(path) as fd: os.fsync(fd)


def _recover_pair(part, final):
    if not part.exists(): return
    if final.exists():
        left, right = part.lstat(), final.lstat()
        _check(left, private=True, links=2); _check(right, private=True, links=2)
        if _entity(left) != _entity(right): raise ValueError('Invalid publication pair')
    else:
        _read(part, private=True)
    part.unlink(); _sync(part.parent)


def _publish(path, data):
    part = path.with_name(path.name + '.part')
    _recover_pair(part, path)
    if path.exists():
        if _read(path, private=True, limit=MAX_JSON)[0] != data: raise ValueError('Changed commitment')
        return
    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data); stream.flush(); os.fsync(stream.fileno())
    os.link(part, path); part.unlink(); _sync(path.parent)


@contextmanager
def _lock(output, *, create=False, shared=False):
    with _directory(output) as parent:
        _check(os.fstat(parent), directory=True, private=True)
        fd = os.open('.archive.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK |
                     (os.O_CREAT if create else 0), 0o600, dir_fd=parent)
        try:
            _check(os.fstat(fd), private=True)
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            if _entity(os.fstat(fd)) != _entity(os.stat('.archive.lock', dir_fd=parent, follow_symlinks=False)):
                raise ValueError('Archive lock replaced')
            yield
        finally: os.close(fd)


def _manifest(plan):
    return {'format': FORMAT, 'state': 'verified_inactive', 'plan_sha256': hashlib.sha256(_encode(plan)).hexdigest(),
            'installation_id': plan['installation_id'], 'source_revision': plan['source_revision'],
            **plan['source_inventory'], **FLAGS}


def _checkpoint(plan):
    return {'format': FORMAT, 'state': 'copying', 'plan': plan, **FLAGS}


def _verify(output):
    if {p.name for p in output.iterdir()} != {'.archive.lock', 'plan.json', 'checkpoint.json', 'manifest.json', 'archive'}:
        raise ValueError('Unknown or missing archive output')
    plan = _metadata(output / 'plan.json')
    if set(plan) != {'format', 'installation_id', 'source_revision', 'source_identity', 'output_identity', 'source_inventory'} or plan['format'] != FORMAT:
        raise ValueError('Invalid archive plan')
    for key in ('installation_id', 'source_revision'):
        if not isinstance(plan[key], str) or not TOKEN.fullmatch(plan[key]): raise ValueError('Invalid identity')
    if _encode(_metadata(output / 'checkpoint.json')) != _encode(_checkpoint(plan)): raise ValueError('Checkpoint mismatch')
    manifest = _metadata(output / 'manifest.json')
    if _encode(manifest) != _encode(_manifest(plan)): raise ValueError('Manifest mismatch')
    inventory = _inventory(output / 'archive', private=True)
    if inventory != plan['source_inventory']: raise ValueError('Archive integrity mismatch')
    _raw(output / 'archive', inventory)
    return manifest


def verify_archive(output):
    output = _path(output)
    with _lock(output, shared=True): return _verify(output)


def prepare_wiki_archive(source_root, output, *, installation_id='wiki-archive', source_revision='legacy-1', _after_publish=None):
    source, output = _path(source_root), _path(output)
    if source == output or source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('Archive paths overlap')
    for value in (installation_id, source_revision):
        if not isinstance(value, str) or not TOKEN.fullmatch(value): raise ValueError('Invalid identity')
    with _directory(source) as fd: source_entity = _entity(os.fstat(fd))
    inventory = _inventory(source); _raw(source, inventory)
    plan = {'format': FORMAT, 'installation_id': installation_id, 'source_revision': source_revision,
            'source_identity': {'path_sha256': hashlib.sha256(str(source).encode()).hexdigest(), 'device': source_entity[0], 'inode': source_entity[1]},
            'output_identity': hashlib.sha256(str(output).encode()).hexdigest(), 'source_inventory': inventory}
    encoded_plan = _encode(plan)
    created = not output.exists()
    if created: output.mkdir(mode=0o700); _sync(output.parent)
    elif not (output / '.archive.lock').exists(): raise ValueError('Unowned output')
    with _lock(output, create=created):
        allowed = {'.archive.lock', 'plan.json', 'plan.json.part', 'checkpoint.json', 'checkpoint.json.part', 'manifest.json', 'manifest.json.part', '.payload.part', 'archive'}
        if any(p.name not in allowed for p in output.iterdir()): raise ValueError('Unknown archive output')
        if not (output / 'plan.json').exists() and {p.name for p in output.iterdir()} - {'.archive.lock', 'plan.json.part'}:
            raise ValueError('Unowned incomplete archive')
        _publish(output / 'plan.json', encoded_plan)
        if (output / 'manifest.json').exists():
            _recover_pair(output / 'manifest.json.part', output / 'manifest.json')
            result = _verify(output)
        else:
            _publish(output / 'checkpoint.json', _encode(_checkpoint(plan)))
            archive = output / 'archive'
            if not archive.exists(): archive.mkdir(mode=0o700); _sync(output)
            # Only a single fixed temporary outside the payload can be recovered.
            # A legitimate source file named *.part is ordinary immutable data.
            part = output / '.payload.part'
            if part.exists():
                info = part.lstat()
                if info.st_nlink == 2:
                    matches = [archive / name for name in inventory['files']
                               if (archive / name).exists() and _entity((archive / name).lstat()) == _entity(info)]
                    if len(matches) != 1: raise ValueError('Unknown payload publication pair')
                    _recover_pair(part, matches[0])
                else: _recover_pair(part, output / '.never-published')
            existing = _inventory(archive, private=True)
            if not set(existing['directories']) <= set(inventory['directories']): raise ValueError('Unknown payload directory')
            if any(inventory['files'].get(name) != fact for name, fact in existing['files'].items()):
                raise ValueError('Unknown or changed payload')
            for name in inventory['directories']:
                directory = archive / name
                if not directory.exists(): directory.mkdir(mode=0o700); _sync(directory.parent)
            for name, fact in sorted(inventory['files'].items()):
                target = archive / name
                if not target.exists():
                    fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    try:
                        _, actual = _read(source / name, destination=fd)
                        os.fsync(fd)
                    finally: os.close(fd)
                    if actual != fact: raise ValueError('Source changed during copy')
                    os.link(part, target); part.unlink(); _sync(target.parent); _sync(output)
                if _after_publish: _after_publish(name)
            if _inventory(archive, private=True) != inventory: raise ValueError('Copied archive changed')
            if _inventory(source) != inventory: raise ValueError('Source changed')
            _publish(output / 'manifest.json', _encode(_manifest(plan)))
            result = _verify(output)
        with _directory(source) as fd:
            if _entity(os.fstat(fd)) != source_entity: raise ValueError('Source root changed')
        if _inventory(source) != inventory: raise ValueError('Source changed')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root'); parser.add_argument('--output', required=True)
    parser.add_argument('--installation-id', default='wiki-archive'); parser.add_argument('--source-revision', default='legacy-1')
    parser.add_argument('--verify', action='store_true'); args = parser.parse_args()
    try:
        result = verify_archive(args.output) if args.verify else prepare_wiki_archive(args.source_root, args.output, installation_id=args.installation_id, source_revision=args.source_revision)
        print(json.dumps(result, sort_keys=True)); return 0
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'wiki_archive_rejected', **FLAGS})); return 1


if __name__ == '__main__': raise SystemExit(main())
