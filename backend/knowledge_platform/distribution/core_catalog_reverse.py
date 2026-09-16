"""Build an inactive old-schema Catalog candidate from a verified core projection.

This reverses representable metadata and deletion deltas. Content/layout changes
and new identities require their own reverse migration first; other changed
target domains refuse unless a verified non-core disposition receipt covers
exactly the changed tables.
"""
from contextlib import ExitStack
import argparse
import copy
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import tempfile
from urllib.parse import quote

from sqlalchemy import create_engine, MetaData, Table, select
from ..catalog.rehearsal_runner import (
    _canonical_space, _canonical_asset, _canonical_dataset, _json_safe,
)
from . import sqlite_reverse_delta as sql
from .other_catalog_reverse import verify_other_catalog_disposition

FORMAT = 'puddingknowledge-core-catalog-reverse/v1'
CORE = ('knowledge_spaces', 'knowledge_assets', 'knowledge_datasets')
LEGACY = ('knowledge_bases', 'knowledge_documents')
RESERVED = {'legacy_document_id', 'legacy_status', 'legacy_virtual_path',
            'legacy_source_type', 'source_reference_digest', 'origin_url_digest'}


def _rows(path, names):
    engine = create_engine('sqlite:///file:' + quote(str(path), safe='/') + '?mode=ro&immutable=1&uri=true')
    try:
        with engine.connect() as connection:
            return {name: [dict(row) for row in connection.execute(
                select(Table(name, MetaData(), autoload_with=connection))).mappings()]
                for name in names}
    finally:
        engine.dispose()


def _by_id(rows):
    result = {row['id']: row for row in rows}
    if len(result) != len(rows):
        raise ValueError('Duplicate identity')
    return result


def _projection(legacy, revision):
    spaces = [_canonical_space(row) for row in legacy['knowledge_bases']]
    assets = [_canonical_asset(row, source_revision=revision,
                               space_id='space_' + row['knowledge_base_id'])
              for row in legacy['knowledge_documents']]
    return dict(zip(CORE, (spaces, assets, [
        _canonical_dataset(space, assets, source_revision=revision) for space in spaces])))


def _same(left, right):
    return sql._json(_json_safe(left)) == sql._json(_json_safe(right))


def _verify_projection(legacy, target, revision):
    projected = _projection(legacy, revision)
    for name in CORE:
        if not _same(_by_id(projected[name]), _by_id(target[name])):
            raise ValueError('Core projection is not losslessly representable')


def _has_redaction(value):
    return value == '<redacted>' or (isinstance(value, dict) and any(_has_redaction(v) for v in value.values())) or (isinstance(value, list) and any(_has_redaction(v) for v in value))


def _metadata_overlay(source, before, after):
    if _same(before, after):
        return copy.deepcopy(source)
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(source, dict):
        result = copy.deepcopy(source)
        for key in set(before) | set(after):
            if key not in after:
                if _has_redaction(before[key]):
                    raise ValueError('Ambiguous redacted metadata deletion')
                result.pop(key, None)
            elif key not in before:
                if _has_redaction(after[key]):
                    raise ValueError('Ambiguous redacted metadata insertion')
                result[key] = copy.deepcopy(after[key])
            else:
                result[key] = _metadata_overlay(source.get(key), before[key], after[key])
        return result
    # A changed container with a redacted descendant cannot reconstruct it
    # unambiguously (e.g. list reordering). Never substitute redaction sentinels.
    if _has_redaction(before) or _has_redaction(after):
        raise ValueError('Ambiguous redacted metadata update')
    return copy.deepcopy(after)


def _inverse(legacy, before, after, revision):
    _verify_projection(legacy, before, revision)
    spaces = _by_id(after['knowledge_spaces'])
    assets = _by_id(after['knowledge_assets'])
    old_spaces = _by_id(before['knowledge_spaces'])
    old_assets = _by_id(before['knowledge_assets'])
    if not set(assets) <= set(old_assets):
        raise ValueError('New identities require an explicit reverse identity and file mapping')
    result = {name: [] for name in LEGACY}
    for original in legacy['knowledge_bases']:
        current = spaces.get('space_' + original['id'])
        if current is None:
            continue
        row = copy.deepcopy(original)
        for field in ('name', 'description', 'created_at', 'updated_at'):
            # Preserve source NULL/format distinctions when its projection did
            # not change, rather than normalizing unmodified legacy values.
            if not _same(current[field], old_spaces[current['id']][field]):
                row[field] = current[field]
        if len(row['name']) > 200:
            raise ValueError('Legacy space name exceeds its declared width')
        result['knowledge_bases'].append(row)
    for identity in sorted(set(spaces) - set(old_spaces)):
        current = spaces[identity]
        if not isinstance(identity, str) or not identity.startswith('space_') or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,63}', identity[6:]):
            raise ValueError('New space requires a portable legacy identity')
        if not isinstance(current['name'], str) or len(current['name']) > 200:
            raise ValueError('Legacy space name exceeds its declared width')
        result['knowledge_bases'].append({'id': identity[6:], **{key: current[key] for key in ('name', 'description', 'created_at', 'updated_at')}})
    legacy_space_ids = {row['id'] for row in result['knowledge_bases']}
    for original in legacy['knowledge_documents']:
        old = _canonical_asset(original, source_revision=revision,
                               space_id='space_' + original['knowledge_base_id'])
        current = assets.get(old['id'])
        if current is None:
            continue
        row = copy.deepcopy(original)
        space_id = current['space_id']
        if not isinstance(space_id, str) or not space_id.startswith('space_') or space_id[6:] not in legacy_space_ids:
            raise ValueError('Reverse document has an unrepresented parent')
        row['knowledge_base_id'] = space_id[6:]
        for field in ('title', 'mime_type', 'source_type', 'created_at', 'updated_at'):
            if not _same(current[field], old[field]):
                row[field] = current[field]
        for field, limit in (('title', 300), ('mime_type', 120), ('source_type', 40)):
            if not isinstance(row[field], str) or len(row[field]) > limit:
                raise ValueError('Legacy document field exceeds its declared width')
        metadata = current['metadata_json']
        old_metadata = old['metadata_json']
        if not isinstance(metadata, dict):
            raise ValueError('Invalid asset metadata')
        for field, key in (('status', 'legacy_status'), ('virtual_path', 'legacy_virtual_path')):
            if metadata.get(key) != old_metadata.get(key):
                if not isinstance(metadata.get(key), str):
                    raise ValueError('Invalid legacy metadata')
                row[field] = metadata[key]
        if len(str(row['status'] or '')) > 40:
            raise ValueError('Legacy status exceeds its declared width')
        # Retain source secret values when the redacted projection is unchanged.
        old_user = {k: v for k, v in old_metadata.items() if k not in RESERVED}
        new_user = {k: v for k, v in metadata.items() if k not in RESERVED}
        if not _same(old_user, new_user):
            # Exclude provenance shadow keys but retain their original legacy
            # values, if any; canonical projection overwrites those keys.
            original_metadata = copy.deepcopy(original.get('doc_metadata') or {})
            source_user = {k: v for k, v in original_metadata.items() if k not in RESERVED}
            restored = _metadata_overlay(source_user, old_user, new_user)
            row['doc_metadata'] = {**{k: v for k, v in original_metadata.items() if k in RESERVED}, **restored}
        result['knowledge_documents'].append(row)
    # Checks content/revision/URI, permissions, redaction, dataset membership,
    # schema-only capabilities and every other projected field, not a subset.
    _verify_projection(result, after, revision)
    return result


def build_core_catalog_reverse(source_snapshot, target_before, target_after, output, *, source_revision, other_catalog_disposition=None, _document_transform=None):
    if not isinstance(source_revision, str) or not source_revision or len(source_revision) > 200:
        raise ValueError('Invalid source revision')
    sources = [sql._path(path) for path in (source_snapshot, target_before, target_after)]
    destination = sql._path(output, output=True)
    if len({(p.stat().st_dev, p.stat().st_ino) for p in sources}) != 3:
        raise ValueError('Distinct snapshot inputs required')
    stage = destination.parent / ('.' + destination.name + '.reverse-' + secrets.token_hex(8))
    try:
        with ExitStack() as stack:
            private = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix='.core-reverse-', dir=destination.parent)))
            copies = [private / f'{index}.sqlite3' for index in range(3)]
            digests = [sql._file_digest(path, copy_to=copied) for path, copied in zip(sources, copies)]
            connections = [sql._connect(path) for path in copies]
            for connection in connections:
                stack.callback(connection.close)
                if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                    raise ValueError('Invalid input database')
            src, pre, post = [sql._snapshot(connection) for connection in connections]
            if sql._layout(pre) != sql._layout(post):
                raise ValueError('Target schema changed')
            changed = {name for name in pre['tables']
                       if name not in CORE and pre['tables'][name] != post['tables'][name]}
            if changed and other_catalog_disposition is None:
                raise ValueError('Unmapped target domain changed')
            if other_catalog_disposition is not None:
                verify_other_catalog_disposition(other_catalog_disposition, changed=changed,
                                                 before=pre, after=post,
                                                 before_sha256=digests[1], after_sha256=digests[2])
            legacy = _rows(copies[0], LEGACY)
            before, after = [_rows(path, CORE) for path in copies[1:]]
            inverse_legacy, inverse_before, inverse_after = legacy, before, after
            if _document_transform is not None:
                # Internal integration hook: the document materializer verifies
                # actual file bytes and original baseline before rebinding paths.
                inverse_legacy, inverse_before, inverse_after = _document_transform(legacy, before, after)
            reversed_rows = _inverse(inverse_legacy, inverse_before, inverse_after, source_revision)
            fd = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            db = sqlite3.connect(stage)
            try:
                connections[0].backup(db)
            finally:
                db.close()
            expected_raw = {}
            engine = create_engine(f'sqlite:///{stage}')
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql('PRAGMA trusted_schema=OFF')
                    # Do not execute ON DELETE CASCADE on unrelated legacy rows.
                    # Full FK and logical-state checks below validate the final state.
                    connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
                    tables = {name: Table(name, MetaData(), autoload_with=connection) for name in LEGACY}
                    for name in reversed(LEGACY):
                        connection.execute(tables[name].delete())
                    for name in LEGACY:
                        columns = src['tables'][name]['columns']
                        original_typed = _by_id(legacy[name])
                        original_raw = {dict(zip(columns, values))['id']: dict(zip(columns, values))
                                        for values in src['tables'][name]['rows']}
                        values = []
                        for row in reversed_rows[name]:
                            old = original_typed.get(row['id'])
                            bound = []
                            for column in columns:
                                value = row.get(column)
                                if old is not None and _same(old.get(column), value):
                                    # Preserve exact raw JSON/date/NULL cells when
                                    # this legacy field was not changed.
                                    bound.append(original_raw[row['id']][column])
                                else:
                                    processor = tables[name].c[column].type.dialect_impl(connection.dialect).bind_processor(connection.dialect)
                                    bound.append(processor(value) if processor else value)
                            values.append(tuple(bound))
                        expected_raw[name] = sorted(values, key=sql._json)
                        if values:
                            statement = 'INSERT INTO ' + sql._q(name) + ' (' + ','.join(map(sql._q, columns)) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
                            connection.exec_driver_sql(statement, values)
                    if connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall():
                        raise ValueError('Reverse candidate would orphan a legacy reference')
                    if connection.exec_driver_sql('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('Reverse candidate integrity failed')
            finally:
                engine.dispose()
            candidate = sql._connect(stage)
            try:
                actual = sql._snapshot(candidate)
            finally:
                candidate.close()
            if sql._layout(actual) != sql._layout(src):
                raise ValueError('Legacy schema changed')
            for name in src['tables']:
                if name not in LEGACY and src['tables'][name] != actual['tables'][name]:
                    raise ValueError('Unowned legacy domain changed')
            for name in LEGACY:
                if actual['tables'][name]['rows'] != expected_raw[name]:
                    raise ValueError('Legacy raw cell preservation failed')
            actual_rows = _rows(stage, LEGACY)
            for name in LEGACY:
                if not _same(_by_id(actual_rows[name]), _by_id(reversed_rows[name])):
                    raise ValueError('Legacy rows changed during materialization')
            _verify_projection(actual_rows, inverse_after, source_revision)
            for path, digest in zip(sources, digests):
                sql._path(path)
                if sql._file_digest(path) != digest:
                    raise ValueError('Input snapshot changed')
            sql._path(destination, output=True)
            with stage.open('rb') as handle:
                os.fsync(handle.fileno())
            os.link(stage, destination)
            stage.unlink()
            fd = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            counts = {}
            for name in LEGACY:
                old, new = _by_id(legacy[name]), _by_id(actual_rows[name])
                counts[name] = {'deleted': len(old.keys() - new.keys()),
                                'inserted': len(new.keys() - old.keys()),
                                'updated': sum(not _same(old[key], new[key]) for key in new.keys() & old.keys())}
            return {'format': FORMAT, 'state': 'verified_inactive_core_metadata',
                    'source_revision': source_revision, 'input_sha256': digests,
                    'output_sha256': sql._file_digest(destination), 'changes': counts,
                    'activation_allowed': False, 'rollback_completed': False,
                    'file_layout_reversed': False, 'credential_continuity_verified': False}
    finally:
        if stage.exists():
            stage.unlink()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(stage) + suffix)
            if sidecar.exists():
                sidecar.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source-snapshot', 'target-before', 'target-after', 'output', 'source-revision'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--other-catalog-disposition')
    args = parser.parse_args(argv)
    try:
        result = build_core_catalog_reverse(**vars(args))
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'core_catalog_reverse_rejected', 'activation_allowed': False}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
