"""Pure planning for reversing owned Wiki state into an inactive legacy brain root.

Every input is an immutable fact already verified by the caller. Current state
that cannot be represented with legacy semantics rejects; nothing here reads or
writes the filesystem.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import re

from .wiki_archive import MAX_FILE, MAX_FILES, MAX_JSON, MAX_TOTAL
from ..wiki.lint import lint_workspace, _frontmatter, _canonical_raw_source
from ..wiki.patch import WikiPatch, PageChange, plan_patch, workspace_revision, canonical, digest

FORMAT = 'puddingknowledge-wiki-reverse/v1'
MAX_COMPILATIONS = 500
MAX_COMMITS = 200
MAX_PATCH_BYTES = 256 * 1024 * 1024
MAX_PATCH_JSON = 32 * 1024 * 1024
MAX_MARKDOWN = 8 * 1024 * 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TYPED_PAGE = re.compile(r'wiki/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+\.md')
_OPERATION = re.compile(r'[a-zA-Z0-9._-]{1,160}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_RECEIPT = re.compile(r'receipt_[0-9a-f]{48}')
_EVIDENCE_PREFIXES = ('.puddingclaw/jobs/', '.puddingclaw/retired/')


def _normalize(text):
    # Legacy page writes always store a trailing newline.
    return text if text.endswith('\n') else text + '\n'


def _strict_json(text):
    def hook(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=hook,
                       parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid JSON constant')))
    if canonical(value) != text: raise ValueError('Noncanonical committed request')
    return value


def _timestamp(value):
    if not isinstance(value, str) or not value.strip(): raise ValueError('Invalid publication timestamp')
    try: parsed = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    except ValueError as error: raise ValueError('Invalid publication timestamp') from error
    if parsed.tzinfo is None: raise ValueError('Publication timestamp requires a timezone')
    return parsed.astimezone(UTC)


def _job_id(*parts):
    return 'wiki-reverse-' + hashlib.sha256(canonical(list(parts)).encode()).hexdigest()[:16]


def _retire_job_id(*parts):
    return 'wiki-retire-reverse-' + hashlib.sha256(canonical(list(parts)).encode()).hexdigest()[:16]


def _consumed(markdown, selected, raw_names, *, slug):
    front, _ = _frontmatter(markdown, source=slug)
    sources = front.get('sources')
    if not isinstance(sources, list): raise ValueError('Reversed page must declare its sources')
    consumed = sorted({_canonical_raw_source(source, raw_names) for source in sources} & set(selected))
    if not consumed: raise ValueError('Reversed page cites no selected Raw')
    return consumed


def _publish_receipt(job_id, bundle, schema_id, schema_version, raw_hashes, pages, consumed, published_at, report, marker):
    receipt = {'job_id': job_id, 'status': 'published',
               'bundle_hash': bundle.bundle_hash, 'schema_id': schema_id, 'schema_version': schema_version,
               'raw_hashes': dict(raw_hashes), 'pages': pages, 'consumed_raw_by_page': consumed,
               'published_at': published_at, 'lint': report, 'reversed': True, 'reverse': marker}
    data = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if len(data) > MAX_JSON: raise ValueError('Reversed receipt exceeds budget')
    return {'job_id': job_id, 'operation': 'publish', 'synthetic_published_at': marker['synthetic_published_at'],
            'origin': marker['origin'], 'bytes': data}


def _retirement_receipt(job_id, bundle, schema_id, schema_version, raw_hashes, retired_pages, updated, published_at, report, marker):
    receipt = {'job_id': job_id, 'status': 'published', 'operation': 'page-retirement',
               'bundle_hash': bundle.bundle_hash, 'schema_id': schema_id, 'schema_version': schema_version,
               'raw_hashes': dict(raw_hashes), 'retired_pages': retired_pages, 'updated_pages': updated,
               'archive_dir': f'.puddingclaw/retired/{job_id}/wiki',
               'published_at': published_at, 'retired_at': published_at,
               'lint': report, 'reversed': True, 'reverse': marker}
    data = (json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if len(data) > MAX_JSON: raise ValueError('Reversed receipt exceeds budget')
    return {'job_id': job_id, 'operation': 'page-retirement', 'synthetic_published_at': marker['synthetic_published_at'],
            'origin': marker['origin'], 'bytes': data}


def _compilation_publications(compilations, archive_pages, archive_index, raw_hashes, raw_manifest_sha256,
                              bundle, schema_id, schema_version):
    if len(compilations) > MAX_COMPILATIONS: raise ValueError('Compilation count exceeds reverse budget')
    ordered = sorted(compilations, key=lambda row: (row['updated_at'], row['resource_uri']))
    raw_names = set(raw_hashes)
    pages = dict(archive_pages); receipts = []; added = []
    for row in ordered:
        if set(row) != {'slug', 'markdown', 'resource_uri', 'receipt_id', 'snapshot_path', 'content_digest', 'updated_at'}:
            raise ValueError('Invalid compilation publication fact')
        slug, markdown = row['slug'], row['markdown']
        if not isinstance(slug, str) or not _TYPED_PAGE.fullmatch('wiki/' + slug + '.md'):
            raise ValueError('Invalid compiled Wiki slug')
        if not isinstance(markdown, str) or len(markdown.encode()) > MAX_MARKDOWN:
            raise ValueError('Invalid compiled Wiki page')
        if not isinstance(row['resource_uri'], str) or not _RECEIPT.fullmatch(row['receipt_id'] or ''):
            raise ValueError('Invalid compiled Wiki identity')
        published = row['updated_at']; _timestamp(published)
        if slug in pages: raise ValueError('Compiled Wiki page conflicts with archive')
        path = row['snapshot_path']
        if path not in raw_names or row['content_digest'] != 'sha256:' + raw_hashes[path]:
            raise ValueError('Compiled Wiki Raw evidence mismatch')
        pages[slug] = markdown; added.append(slug)
        index = archive_index + '\n' + ''.join(f'- [[{name}]]\n' for name in sorted(added))
        report = lint_workspace(contract=bundle.lint_contract(), pages=pages, index=index, log_present=True,
                                raw_hashes=raw_hashes, raw_manifest_sha256=raw_manifest_sha256)
        if not report['ok']: raise ValueError('Compiled Wiki workspace fails legacy lint')
        marker = {'origin': 'knowledge_local_wiki_compilations', 'receipt_id': row['receipt_id'],
                  'resource_uri': row['resource_uri'], 'synthetic_published_at': False}
        receipts.append(_publish_receipt(_job_id('compilation', row['receipt_id'], row['resource_uri']),
                                         bundle, schema_id, schema_version, raw_hashes, [slug],
                                         {slug: [path]}, published, report, marker))
    return pages, receipts


def _authoring_replay(authoring, pages0, index0, log0, raw_hashes, raw_manifest_sha256,
                      bundle, schema_id, schema_version, anchor):
    commits = authoring['commits']
    if len(commits) > MAX_COMMITS: raise ValueError('Authoring commit count exceeds reverse budget')
    by_previous = {}; total = 0
    for row in commits:
        if set(row) != {'operation_id', 'request_digest', 'previous_revision', 'revision',
                        'retired_json', 'patch_json', 'before_json', 'receipt_digest'}:
            raise ValueError('Invalid authoring commit fact')
        if not isinstance(row['operation_id'], str) or not _OPERATION.fullmatch(row['operation_id']):
            raise ValueError('Invalid authoring operation identity')
        for key in ('request_digest', 'previous_revision', 'revision', 'receipt_digest'):
            if not isinstance(row[key], str) or not _DIGEST.fullmatch(row[key]):
                raise ValueError('Invalid authoring commitment digest')
        for key in ('retired_json', 'patch_json', 'before_json'):
            if not isinstance(row[key], str) or len(row[key].encode()) > MAX_PATCH_JSON:
                raise ValueError('Invalid authoring committed payload')
        total += len(row['patch_json'].encode())
        if total > MAX_PATCH_BYTES: raise ValueError('Authoring payload exceeds reverse budget')
        if row['previous_revision'] in by_previous: raise ValueError('Ambiguous authoring commit chain')
        by_previous[row['previous_revision']] = row
    revision = workspace_revision(bundle, dict(pages0), index0, log0, raw_hashes, raw_manifest_sha256)
    pages, index, log = dict(pages0), index0, log0
    receipts = []; retired_files = []; raw_names = set(raw_hashes); sequence = 0
    remaining = len(commits)
    while remaining:
        row = by_previous.get(revision)
        if row is None: raise ValueError('Broken authoring commit chain')
        request = _strict_json(row['patch_json'])
        if set(request) != {'revision', 'changes', 'selected_raw', 'index', 'log_entry'}:
            raise ValueError('Invalid authoring request shape')
        if digest(row['patch_json']) != row['request_digest']:
            raise ValueError('Authoring request commitment mismatch')
        if any(not isinstance(change, dict) or set(change) != {'slug', 'markdown', 'expected_digest', 'replacement'}
               for change in request['changes']):
            raise ValueError('Invalid authoring request payload')
        try:
            patch = WikiPatch(request['revision'],
                              tuple(PageChange(change['slug'], change['markdown'], change['expected_digest'], change['replacement'])
                                    for change in request['changes']),
                              tuple(request['selected_raw']), request['index'], request['log_entry'])
        except (TypeError, KeyError) as error:
            raise ValueError('Invalid authoring request payload') from error
        planned = plan_patch(bundle=bundle, pages=pages, index=index, log=log,
                             raw_hashes=raw_hashes, raw_manifest_sha256=raw_manifest_sha256, patch=patch)
        if planned.request_digest != row['request_digest'] or planned.revision != row['revision']:
            raise ValueError('Authoring replay commitment mismatch')
        before = canonical([(change.slug, pages.get(change.slug)) for change in patch.changes])
        if before != row['before_json'] or canonical(planned.retired) != row['retired_json']:
            raise ValueError('Authoring before-image commitment mismatch')
        if digest(canonical([row['operation_id'], planned.request_digest, revision, planned.revision,
                             row['retired_json'], before, row['patch_json']])) != row['receipt_digest']:
            raise ValueError('Authoring receipt commitment mismatch')
        after = dict(planned.pages)
        report = lint_workspace(contract=bundle.lint_contract(), pages=after, index=planned.index, log_present=True,
                                raw_hashes=raw_hashes, raw_manifest_sha256=raw_manifest_sha256)
        if not report['ok']: raise ValueError('Authoring workspace fails legacy lint')
        published = (anchor + timedelta(seconds=sequence)).isoformat(); sequence += 1
        marker = {'origin': 'knowledge_wiki_authoring_commits', 'operation_id': row['operation_id'],
                  'receipt_digest': row['receipt_digest'], 'synthetic_published_at': True}
        writes = [change for change in patch.changes if change.markdown is not None]
        if writes:
            consumed = {change.slug: _consumed(change.markdown, patch.selected_raw, raw_names, slug=change.slug)
                        for change in writes}
            receipts.append(_publish_receipt(_job_id('authoring-publish', row['operation_id'], row['receipt_digest']),
                                             bundle, schema_id, schema_version, raw_hashes,
                                             [change.slug for change in writes], consumed, published, report, marker))
        if planned.retired:
            retired_pages = {}
            for slug, before_markdown, replacement in planned.retired:
                if replacement is None:
                    raise ValueError('Legacy retirement requires an explicit replacement page')
                retired_pages[slug] = replacement
            updated = sorted(change.slug for change in writes)
            job = _retire_job_id('authoring-retirement', row['operation_id'], row['receipt_digest'])
            for slug, before_markdown, _replacement in planned.retired:
                data = _normalize(before_markdown).encode('utf-8')
                if len(data) > MAX_MARKDOWN: raise ValueError('Retired page exceeds budget')
                retired_files.append({'job_id': job, 'slug': slug, 'bytes': data})
            receipts.append(_retirement_receipt(job, bundle, schema_id, schema_version, raw_hashes,
                                                dict(sorted(retired_pages.items())), updated, published, report, marker))
        pages, index, log, revision = after, planned.index, planned.log, planned.revision
        remaining -= 1
    if (revision, pages, index, log) != (authoring['revision'], authoring['pages'], authoring['index'], authoring['log']):
        raise ValueError('Authoring chain does not reach the committed state')
    return pages, index, log, receipts, retired_files


def _delta(added, updated, removed, compilations, commits, receipts, retired):
    return {'pages_added': added, 'pages_updated': updated, 'pages_retired': removed,
            'compilations_reversed': compilations, 'authoring_commits_reversed': commits,
            'receipts_written': receipts, 'retired_pages_archived': retired}


def plan_wiki_reverse(*, source_files, source_directories, archive_pages, archive_index, archive_log,
                      raw_hashes, raw_manifest_sha256, historical_published_at,
                      compilations, authoring, bundle, schema_id, schema_version):
    if not isinstance(source_files, dict) or not isinstance(source_directories, list):
        raise ValueError('Invalid source inventory')
    if not compilations and authoring is None:
        # Pure archival roundtrip: no committed post-migration Wiki state exists.
        return {'format': FORMAT, 'files': dict(source_files), 'directories': list(source_directories),
                'writes': {}, 'removals': [], 'receipts': [],
                'delta': _delta([], [], [], 0, 0, 0, 0), 'changed': False}
    if not isinstance(archive_pages, dict) or not isinstance(archive_index, str) or not isinstance(archive_log, str):
        raise ValueError('Archived Wiki text is required for the committed delta')
    if not isinstance(raw_hashes, dict) or not isinstance(raw_manifest_sha256, str):
        raise ValueError('Archived Raw facts are required for the committed delta')
    if bundle is None or not schema_id or not schema_version or bundle.bundle_version != schema_version:
        raise ValueError('Reversed Wiki receipts require the admitted schema identity')
    pages_c, compilation_receipts = _compilation_publications(
        compilations, archive_pages, archive_index, raw_hashes, raw_manifest_sha256,
        bundle, schema_id, schema_version)
    index0 = archive_index + '\n' + ''.join(f'- [[{slug}]]\n' for slug in sorted(set(pages_c) - set(archive_pages)))
    receipts = list(compilation_receipts); retired_files = []
    if authoring is not None:
        if not authoring['commits']:
            # An initialized workspace without commits restores the archived bytes;
            # the platform's initial index newline is not a content change.
            if authoring['pages'] != pages_c or authoring['log'] != archive_log or authoring['index'] != index0:
                raise ValueError('Authoring state diverged without commits')
            expected = workspace_revision(bundle, dict(pages_c), index0, archive_log, raw_hashes, raw_manifest_sha256)
            if expected != authoring['revision']:
                raise ValueError('Authoring initial commitment mismatch')
            pages, index, log = pages_c, index0, archive_log
        else:
            candidates = []
            for value in historical_published_at:
                try: candidates.append(_timestamp(value))
                except ValueError: pass
            for row in compilations: candidates.append(_timestamp(row['updated_at']))
            anchor = max(candidates) if candidates else _EPOCH
            pages, index, log, authored, retired_files = _authoring_replay(
                authoring, pages_c, index0, archive_log, raw_hashes, raw_manifest_sha256,
                bundle, schema_id, schema_version, anchor)
            receipts.extend(authored)
    else:
        pages, index, log = pages_c, index0, archive_log
    writes = {}; removals = []
    for slug in sorted(pages):
        if slug in archive_pages and archive_pages[slug] == pages[slug]: continue
        writes['wiki/' + slug + '.md'] = _normalize(pages[slug]).encode('utf-8')
    for slug in sorted(set(archive_pages) - set(pages)):
        removals.append('wiki/' + slug + '.md')
    if index != archive_index: writes['wiki/index.md'] = index.encode('utf-8')
    if log != archive_log: writes['wiki/log.md'] = log.encode('utf-8')
    for receipt in receipts:
        writes['.puddingclaw/jobs/' + receipt['job_id'] + '.json'] = receipt['bytes']
    for entry in retired_files:
        writes[f".puddingclaw/retired/{entry['job_id']}/wiki/{entry['slug']}.md"] = entry['bytes']
    files = dict(source_files)
    for relative in removals:
        if relative not in files: raise ValueError('Retired page is not archived')
        del files[relative]
    planned = {}
    for relative, data in sorted(writes.items()):
        if relative.startswith(_EVIDENCE_PREFIXES) and relative in files:
            raise ValueError('Reverse receipt collides with archived evidence')
        if len(data) > MAX_FILE: raise ValueError('Reverse output file exceeds budget')
        fact = {'sha256': hashlib.sha256(data).hexdigest(), 'size_bytes': len(data)}
        if relative in files and files[relative] == fact:
            del writes[relative]
        else:
            planned[relative] = data
            files[relative] = fact
    writes = planned
    if len(files) > MAX_FILES or sum(fact['size_bytes'] for fact in files.values()) > MAX_TOTAL:
        raise ValueError('Reverse output exceeds budget')
    directories = set()
    for relative in files:
        parent = relative.rpartition('/')[0]
        while parent:
            directories.add(parent); parent = parent.rpartition('/')[0]
    for directory in source_directories:
        if directory not in directories and not any(
                name == directory or name.startswith(directory + '/') for name in source_files):
            directories.add(directory)
    if set(files) & directories: raise ValueError('Reverse output layout conflicts')
    added = sorted(set(pages) - set(archive_pages))
    updated = sorted(slug for slug in set(pages) & set(archive_pages) if archive_pages[slug] != pages[slug])
    removed = sorted(set(archive_pages) - set(pages))
    receipt_meta = [{'job_id': receipt['job_id'], 'operation': receipt['operation'], 'origin': receipt['origin'],
                     'synthetic_published_at': receipt['synthetic_published_at']} for receipt in receipts]
    return {'format': FORMAT, 'files': files, 'directories': sorted(directories), 'writes': writes,
            'removals': removals, 'receipts': receipt_meta,
            'delta': _delta(added, updated, removed, len(compilations),
                            len(authoring['commits']) if authoring else 0, len(receipts), len(retired_files)),
            'changed': bool(writes or removals)}
