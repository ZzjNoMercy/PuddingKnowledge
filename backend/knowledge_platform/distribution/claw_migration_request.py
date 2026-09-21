"""Generate a pre-verified offline Claw migration request for migrate_from_claw.

The generator reads a quiesced copy of the legacy Claw Catalog and the
snapshot payload assembled by the Harness-side producer, then emits a
puddingknowledge-migrate-from-claw-request/v2 request that the forward chain
accepts on the first attempt: every commitment the chain verifies is verified
here first, so a bad request fails generation instead of the chain.

Request contract (mirrors distribution/migrate_from_claw.py validation):

- keys are exactly {format, installation_id, source_revision,
  source_schema_revision, source_catalog, source_files_root, source_wiki_root,
  bindings, original_bindings, attachment_bindings} and format is
  puddingknowledge-migrate-from-claw-request/v2 (v2 requires source_wiki_root);
- installation_id, source_revision and source_schema_revision fullmatch the
  identity TOKEN ^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$.  Knowledge consumers only
  bind source_schema_revision into the request digest and no derivation is
  established anywhere in this repository, so it stays an explicit CLI
  parameter (a natural value for a real legacy Home is its schema journal
  head, e.g. claw-schema-v<N> from the legacy core_schema_migrations table);
- source_catalog, source_files_root and source_wiki_root are absolute paths
  inside the independently approved snapshot root; containment is verified
  here up front;
- bindings covers exactly the knowledge_documents id set.  Bound body bytes
  must sha256-match representation['body_sha256'] for rows where
  legacy_document_representation(row) is truthy, else row['content_sha256']
  (bare hex in the legacy Catalog; the 'sha256:' prefix is added exactly as
  the document migration does);
- original_bindings covers exactly the representation rows; bound bytes must
  match 'sha256:'+representation['original_sha256'] and each binding must
  differ from that document's body binding;
- attachment_bindings covers exactly collect_attachment_references over the
  decoded rows (the same call document_tree.collect_tree makes); file
  references map to files and directory references to directories under the
  files root;
- every binding value is a canonical relative POSIX path.

Legacy rows carry absolute source paths.  Repeatable --map SRC=DST rules map
them into snapshot-payload relatives: a path matches a mapping when it equals
SRC or starts with SRC + '/', and the longest matching SRC wins.  An unmapped
reference refuses, and two distinct source paths mapping to one relative
refuse (collision).  Mapped relatives must stay canonical and inside the
files root.

The Catalog copy is opened with a SQLite mode=ro&immutable=1 URI and is never
mutated; WAL/SHM/journal sidecars refuse (the copy must be fully checkpointed)
and knowledge_documents must be non-empty.  Every bound body, original and
attachment file is streamed through the same bounded readers the chain uses,
committed digests and PDF size claims are compared, and the exact
document_tree.collect_tree semantics (attachment kinds, claimed facts,
directory topology, dependency graph) are re-run before publication.  The
chain's binding-count and byte budgets are mirrored.

Outputs are private 0600 files of canonical JSON (sorted keys, compact
separators, trailing newline) published atomically without replacement: an
identical re-run republishes byte-identical outputs and a disagreeing existing
file refuses.  The generation receipt binds the request and Catalog digests,
the mapping table, counts (documents/originals/attachments/bytes) and an
unmapped_references=0 assertion; every inert flag remains false.
"""
import argparse
import json
import posixpath
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .catalog_snapshot import _path, _identity
from .document_attachment_metadata import collect_attachment_references
from .document_migration import MAX_TOTAL, TOKEN, _digest, _encode as _canonical, _read
from .document_tree import collect_tree
from .migrate_from_claw import REQUEST_FORMAT_V2, _MAX_REQUEST, _private_read, _write
from .sqlite_reverse_delta import _file_digest
from ..catalog.document_representations import legacy_document_representation

FORMAT = 'puddingknowledge-claw-migration-request-receipt/v1'
STATE = 'verified_inactive_request'
FLAGS = {'activation_allowed': False, 'installation_prepared': False, 'complete_installation_migration': False}
_MAX_BINDINGS = 5000


def _encode(value):
    data = _canonical(value) + b'\n'
    if len(data) > _MAX_REQUEST: raise ValueError('Migration request output exceeds protocol budget')
    return data


def _absolute_source(value):
    if (not isinstance(value, str) or not value.startswith('/') or value.startswith('//') or value == '/'
            or '\\' in value or '\x00' in value or posixpath.normpath(value) != value):
        raise ValueError('Legacy source paths must be canonical absolute paths')
    return value


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('Invalid relative binding')
    path = Path(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')) or path.as_posix() != value:
        raise ValueError('Noncanonical relative binding')
    return value


def _mapping(rule):
    source, separator, relative = rule.partition('=')
    if not separator: raise ValueError('Mapping must be SRC=DST')
    return _absolute_source(source), _relative(relative)


def _map_source(path, rules):
    best = None
    for source, relative in rules:
        if path == source or path.startswith(source + '/'):
            if best is None or len(source) > len(best[0]): best = (source, relative)
    if best is None: raise ValueError('Unmapped legacy source reference')
    source, relative = best
    tail = path[len(source):].lstrip('/')
    return relative if not tail else relative + '/' + tail


def _documents(catalog):
    _identity(catalog)
    for suffix in ('-wal', '-shm', '-journal'):
        if _path(str(catalog) + suffix).exists(): raise ValueError('Legacy Catalog copy is not quiesced')
    try:
        connection = sqlite3.connect(f'file:{quote(str(catalog), safe="/")}?mode=ro&immutable=1', uri=True)
        try:
            connection.execute('PRAGMA query_only=ON')
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute('SELECT * FROM knowledge_documents')]
        finally: connection.close()
    except sqlite3.Error as exc: raise ValueError('Legacy Catalog is unreadable') from exc
    if not rows: raise ValueError('Legacy Catalog has no documents')
    decoded = []
    for row in rows:
        if isinstance(row.get('doc_metadata'), str): row['doc_metadata'] = json.loads(row['doc_metadata'])
        decoded.append(row)
    return decoded


def generate_migration_request(snapshot_root, catalog, files_root, wiki_root, *,
                               installation_id, source_revision, source_schema_revision,
                               mappings, output, receipt):
    for value in (snapshot_root, catalog, files_root, wiki_root, output, receipt):
        if not Path(value).expanduser().is_absolute(): raise ValueError('Protocol paths must be absolute')
    snapshot, catalog, files, wiki = (_path(value) for value in (snapshot_root, catalog, files_root, wiki_root))
    output, receipt = _path(output), _path(receipt)
    if not snapshot.is_dir(): raise ValueError('Snapshot root is unavailable')
    for source in (catalog, files, wiki):
        if not source.is_relative_to(snapshot): raise ValueError('Knowledge sources must belong to the approved snapshot')
    if not files.is_dir() or not wiki.is_dir(): raise ValueError('Knowledge source roots must be directories')
    for value in (installation_id, source_revision, source_schema_revision):
        if not isinstance(value, str) or not TOKEN.fullmatch(value): raise ValueError('Invalid migration source identity')
    if output == receipt or output.is_relative_to(snapshot) or receipt.is_relative_to(snapshot) or catalog in (output, receipt):
        raise ValueError('Migration request outputs overlap the approved snapshot')
    for target in (output, receipt):
        if not target.parent.is_dir(): raise ValueError('Migration request output parent is unavailable')
    if not mappings: raise ValueError('At least one path mapping is required')
    rules = [_mapping(rule) for rule in mappings]
    if len({source for source, _relative_root in rules}) != len(rules):
        raise ValueError('Duplicate mapping source prefix')

    rows = _documents(catalog)
    catalog_digest = 'sha256:' + _file_digest(catalog)
    ids = [str(row['id']) for row in rows]
    if len(ids) != len(set(ids)): raise ValueError('Duplicate document identity')
    if len(ids) > _MAX_BINDINGS or any(not TOKEN.fullmatch(doc_id) for doc_id in ids):
        raise ValueError('Invalid document bindings')
    representations = {}
    for row in rows:
        representation = legacy_document_representation(row)
        if representation: representations[str(row['id'])] = representation
    if len(representations) > _MAX_BINDINGS: raise ValueError('Invalid original document bindings')

    relatives, sources = {}, {}
    def bind(absolute):
        _absolute_source(absolute)
        if absolute in relatives: return relatives[absolute]
        relative = _relative(_map_source(absolute, rules))
        if sources.get(relative, absolute) != absolute: raise ValueError('Mapped legacy sources collide')
        relatives[absolute] = relative; sources[relative] = absolute
        return relative

    bindings, blobs, total = {}, {}, 0
    for row in rows:
        doc_id = str(row['id'])
        relative = bind(row.get('storage_path'))
        body = _read(files / relative); total += len(body)
        if total > MAX_TOTAL: raise ValueError('Document migration exceeds budget')
        digest = _digest(body); blobs.setdefault(digest, len(body))
        representation = representations.get(doc_id)
        expected = representation['body_sha256'] if representation else str(row.get('content_sha256') or '')
        if not expected.startswith('sha256:'): expected = 'sha256:' + expected
        if digest != expected: raise ValueError('Document content digest mismatch')
        if representation and (type(row.get('size_bytes')) is not int or row['size_bytes'] != len(body)):
            raise ValueError('PDF Markdown size mismatch')
        bindings[doc_id] = relative
    original_bindings = {}
    for doc_id, representation in representations.items():
        relative = bind(representation['original_path'])
        if relative == bindings[doc_id]: raise ValueError('PDF representations must have distinct bindings')
        original = _read(files / relative); total += len(original)
        if total > MAX_TOTAL: raise ValueError('Document migration exceeds budget')
        digest = _digest(original)
        if digest != 'sha256:' + representation['original_sha256']: raise ValueError('Original PDF digest mismatch')
        blobs.setdefault(digest, len(original))
        original_bindings[doc_id] = relative
    references = collect_attachment_references([{'metadata_json': row.get('doc_metadata') or {}} for row in rows])
    attachment_bindings = {reference: bind(reference) for reference in references}
    tree = collect_tree(files, rows, bindings, original_bindings, attachment_bindings)
    payload = sum(blobs.values()) + sum(fact['size_bytes'] for fact in tree['files'].values())
    if payload > MAX_TOTAL: raise ValueError('Document package exceeds byte budget')

    request = {'format': REQUEST_FORMAT_V2, 'installation_id': installation_id,
               'source_revision': source_revision, 'source_schema_revision': source_schema_revision,
               'source_catalog': str(catalog), 'source_files_root': str(files), 'source_wiki_root': str(wiki),
               'bindings': bindings, 'original_bindings': original_bindings, 'attachment_bindings': attachment_bindings}
    request_data = _encode(request)
    result = {'format': FORMAT, 'state': STATE, 'installation_id': installation_id,
              'source_revision': source_revision, 'source_schema_revision': source_schema_revision,
              'request_digest': _digest(request_data), 'catalog_digest': catalog_digest,
              'counts': {'documents': len(bindings), 'originals': len(original_bindings),
                         'attachments': len(attachment_bindings), 'bytes': payload},
              'mappings': [{'source_prefix': source, 'relative_root': relative} for source, relative in sorted(rules)],
              'unmapped_references': 0, **FLAGS}
    receipt_data = _encode(result)
    for target, data in ((output, request_data), (receipt, receipt_data)):
        if target.exists() and _private_read(target) != data:
            raise ValueError('Existing migration request output disagrees')
    for target, data in ((output, request_data), (receipt, receipt_data)):
        if not target.exists(): _write(target, data)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-root', type=Path, required=True)
    parser.add_argument('--catalog', type=Path, required=True)
    parser.add_argument('--files-root', type=Path, required=True)
    parser.add_argument('--wiki-root', type=Path, required=True)
    parser.add_argument('--installation-id', required=True)
    parser.add_argument('--source-revision', required=True)
    parser.add_argument('--source-schema-revision', required=True)
    parser.add_argument('--map', dest='mappings', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = generate_migration_request(args.snapshot_root, args.catalog, args.files_root, args.wiki_root,
            installation_id=args.installation_id, source_revision=args.source_revision,
            source_schema_revision=args.source_schema_revision, mappings=args.mappings,
            output=args.output, receipt=args.receipt)
    except Exception:
        print(json.dumps({'format': FORMAT, 'status': 'error', 'error_code': 'claw_migration_request_rejected', **FLAGS}))
        return 1
    print(json.dumps(result, sort_keys=True)); return 0


if __name__ == '__main__': raise SystemExit(main())
