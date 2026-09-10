"""Independent Feishu discovery and revision-consistent document snapshots.

Discovery must finish successfully before its result can authorize deletion.
Bitable entries are live links; this module never copies their rows to Catalog.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import re
import hashlib
from pathlib import PurePath
from typing import Any

from .feishu_blocks import convert_feishu_blocks_to_markdown

_TOKEN = re.compile(r'^[A-Za-z0-9_-]{1,220}$')


class FeishuSourceError(ValueError):
    pass


def token(value: Any) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise FeishuSourceError('Feishu source identifier is invalid')
    return value


@dataclass(frozen=True)
class FeishuSelection:
    kind: str
    root: str
    wiki_space: str = ''

    def __post_init__(self):
        if self.kind not in {'wiki', 'drive', 'bitable'}:
            raise FeishuSourceError('Feishu source kind is invalid')
        if self.kind == 'wiki':
            token(self.wiki_space)
            if self.root: token(self.root)
        else:
            token(self.root)
            if self.wiki_space:
                raise FeishuSourceError('Non-Wiki source cannot select a Wiki space')


@dataclass(frozen=True)
class FeishuEntry:
    external_id: str
    parent_id: str | None
    object_token: str
    kind: str
    title: str
    path: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeishuMedia:
    token: str
    block_id: str
    filename: str
    content: bytes
    mime: str
    relative_path: str

    @property
    def mime_type(self) -> str:
        return self.mime


@dataclass(frozen=True)
class FeishuDocument:
    revision: str
    title: str
    raw: bytes
    markdown: bytes
    warnings: tuple[str, ...]
    attachments: tuple[dict, ...]
    media: tuple[FeishuMedia, ...] = ()


class FeishuSource:
    def __init__(self, api, *, max_entries: int = 10000, max_depth: int = 64, bitable_tables=None):
        if not 1 <= max_entries <= 10000 or not 1 <= max_depth <= 64:
            raise FeishuSourceError('Discovery bounds are invalid')
        self.api = api
        self.bitable_tables = bitable_tables
        self.max_entries = max_entries
        self.max_depth = max_depth

    async def discover(self, selection: FeishuSelection) -> tuple[FeishuEntry, ...]:
        if selection.kind == 'bitable':
            # Discover the schema, not row snapshots. Tables remain live sources.
            tables = await self.api.list_bitable_tables(app_token=selection.root)
            if len(tables) > self.max_entries:
                raise FeishuSourceError('Feishu discovery exceeds its bound')
            seen = set(); entries = []
            for table in tables:
                identity = token(table.get('table_id'))
                if identity in seen: raise FeishuSourceError('Duplicate Bitable table identity')
                seen.add(identity)
                name = str(table.get('name') or identity)[:500]
                entries.append(FeishuEntry(f'bitable:{selection.root}:{identity}', None, selection.root, 'bitable_table', name, (name,)))
            if self.bitable_tables is not None:
                requested=set(self.bitable_tables)
                if requested-seen:
                    raise FeishuSourceError('Configured Bitable table is no longer visible')
                entries=[entry for entry in entries if entry.external_id.rsplit(':',1)[-1] in requested]
            return tuple(entries)
        result = []; seen = set(); folders = set(); queue = deque()
        if selection.kind == 'wiki' and selection.root:
            root = await self.api.get_node(node_token=selection.root)
            if token(root.get('node_token')) != selection.root or str(root.get('space_id')) != selection.wiki_space:
                raise FeishuSourceError('Wiki root is outside the selected space')
            queue.append((None, (), [root]))
        else:
            queue.append((selection.root or None, (), None))
        while queue:
            parent, path, supplied = queue.popleft()
            if len(path) >= self.max_depth:
                raise FeishuSourceError('Feishu discovery depth exceeds its bound')
            if supplied is not None:
                children = supplied
            elif selection.kind == 'wiki':
                children = await self.api.list_nodes(space_id=selection.wiki_space, parent_node_token=parent)
            else:
                children = await self.api.list_drive_files(folder_token=parent)
            for node in children:
                identity = token(node.get('node_token') if selection.kind == 'wiki' else node.get('token'))
                if identity in seen or identity in folders:
                    raise FeishuSourceError('Feishu discovery contains repeated or cyclic identity')
                kind = str(node.get('obj_type') if selection.kind == 'wiki' else node.get('type'))
                title = str((node.get('title') if selection.kind == 'wiki' else node.get('name')) or identity)[:500]
                child_path = (*path, title)
                if selection.kind == 'drive' and kind == 'folder':
                    folders.add(identity)
                    queue.append((identity, child_path, None))
                else:
                    seen.add(identity)
                    object_token = token(node.get('obj_token') if selection.kind == 'wiki' else identity)
                    prefix = f'wiki:{selection.wiki_space}:' if selection.kind == 'wiki' else f'drive:{selection.root}:'
                    result.append(FeishuEntry(prefix+identity, prefix+parent if parent else None, object_token, kind, title, child_path))
                    if selection.kind == 'wiki' and node.get('has_child') is True:
                        queue.append((identity, child_path, None))
                if len(seen) + len(folders) > self.max_entries:
                    raise FeishuSourceError('Feishu discovery exceeds its bound')
        return tuple(result)

    async def document(self, entry: FeishuEntry, *, previous_revision: str | None = None) -> FeishuDocument | None:
        if entry.kind != 'docx':
            raise FeishuSourceError('Entry is not a Docx document')
        metadata = await self.api.get_docx_document(document_id=entry.object_token)
        revision = metadata.get('revision_id')
        if type(revision) is not int or revision < 0:
            raise FeishuSourceError('Document revision is unavailable')
        if str(revision) == previous_revision:
            return None
        blocks = await self.api.list_docx_blocks(document_id=entry.object_token, document_revision_id=revision)
        converted = convert_feishu_blocks_to_markdown(blocks)
        if len(converted.assets) > 128:
            raise FeishuSourceError('Docx media attachment count exceeds 128')
        media_tokens = list(dict.fromkeys(str(asset.get('token') or '') for asset in converted.assets))
        if any(not item for item in media_tokens):
            raise FeishuSourceError('Docx media attachment token is missing')
        media = []
        markdown = converted.markdown
        if media_tokens:
            try:
                downloaded = await self.api.download_media_assets(file_tokens=media_tokens,
                    max_bytes_each=8 * 1024 * 1024, max_total_bytes=32 * 1024 * 1024)
            except Exception as exc:
                raise FeishuSourceError('Docx media download failed') from exc
            if not isinstance(downloaded, dict):
                raise FeishuSourceError('Docx media download response is invalid')
            replacements = {}
            total = 0
            for asset in converted.assets:
                media_token = str(asset.get('token') or '')
                block_id = str(asset.get('block_id') or '')
                filename = str(asset.get('filename') or f'feishu-media-{block_id}.bin')
                payload = downloaded.get(media_token)
                if not isinstance(payload, tuple) or len(payload) != 3:
                    raise FeishuSourceError('Docx media download is incomplete')
                content, mime, _provider_name = payload
                if not isinstance(content, bytes) or not isinstance(mime, str):
                    raise FeishuSourceError('Docx media payload is invalid')
                if len(content) > 8 * 1024 * 1024 or total + len(content) > 32 * 1024 * 1024:
                    raise FeishuSourceError('Docx media exceeds its size budget')
                total += len(content)
                digest = hashlib.sha256(f'{block_id}\0{media_token}'.encode()).hexdigest()[:16]
                path = str(asset.get('relative_path') or _media_path(filename, digest))
                media.append(FeishuMedia(media_token, block_id, filename, content, mime, path))
                if not asset.get('relative_path'):
                    replacements.setdefault(filename, []).append(path)
            for filename, paths in replacements.items():
                marker = f'./assets/{_quote(filename)}'
                for path in paths:
                    markdown = markdown.replace(marker, f'./{path}', 1)
        # Preserve the exact revision requested, even if the remote head changes
        # while paginating. The API client pins document_revision_id on all pages.
        raw = json.dumps({'document':metadata, 'blocks':blocks}, ensure_ascii=False, sort_keys=True).encode()
        return FeishuDocument(str(revision), str(metadata.get('title') or entry.title)[:500], raw,
                              markdown.encode(), tuple(converted.warnings), tuple(converted.assets), tuple(media))

    async def drive_document(self, entry: FeishuEntry) -> FeishuDocument:
        if entry.kind != 'file':
            raise FeishuSourceError('Entry is not a Drive file')
        suffix = PurePath(entry.title).suffix.lower()
        if suffix not in {'.md', '.markdown', '.txt', '.pdf'}:
            raise FeishuSourceError('Drive file type is not supported')
        try:
            payload = await self.api.download_drive_file(file_token=entry.object_token, max_bytes=8 * 1024 * 1024)
        except Exception as exc:
            raise FeishuSourceError('Drive file download failed') from exc
        if not isinstance(payload, tuple) or len(payload) != 3 or not isinstance(payload[0], bytes):
            raise FeishuSourceError('Drive file download response is invalid')
        content = payload[0]
        if suffix == '.pdf':
            return FeishuDocument(entry.object_token, entry.title[:500], content, b'',
                ('PDF parsing is deferred to the sync layer',), (), ())
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise FeishuSourceError('Drive text file is not valid UTF-8') from exc
        return FeishuDocument(entry.object_token, entry.title[:500], content, text.encode(), (), (), ())

    async def schema(self, entry: FeishuEntry):
        from .bitable_schema import normalize_schema
        if entry.kind != 'bitable_table' or self.bitable_tables is None:
            raise FeishuSourceError('Bitable schema requires explicitly configured tables')
        table_id = entry.external_id.rsplit(':', 1)[-1]
        if table_id not in self.bitable_tables:
            raise FeishuSourceError('Bitable table is outside the configured scope')
        fields = await self.api.list_bitable_fields(app_token=entry.object_token, table_id=table_id)
        return normalize_schema(entry.object_token, table_id, entry.title, self.bitable_tables[table_id], fields)


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value)


def _media_path(filename: str, digest: str) -> str:
    name = PurePath(filename).name.replace('\\', '_').replace('/', '_')
    name = re.sub(r'[^A-Za-z0-9._ -]', '_', name).strip(' .') or 'attachment.bin'
    return f'assets/{PurePath(name).stem or "attachment"}--{digest}{PurePath(name).suffix}'
