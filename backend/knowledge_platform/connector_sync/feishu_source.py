"""Independent Feishu discovery and revision-consistent document snapshots.

Discovery must finish successfully before its result can authorize deletion.
Bitable entries are live links; this module never copies their rows to Catalog.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import re
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


@dataclass(frozen=True)
class FeishuDocument:
    revision: str
    title: str
    raw: bytes
    markdown: bytes
    warnings: tuple[str, ...]
    attachments: tuple[dict, ...]


class FeishuSource:
    def __init__(self, api, *, max_entries: int = 10000, max_depth: int = 64):
        if not 1 <= max_entries <= 10000 or not 1 <= max_depth <= 64:
            raise FeishuSourceError('Discovery bounds are invalid')
        self.api = api
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
        # Preserve the exact revision requested, even if the remote head changes
        # while paginating. The API client pins document_revision_id on all pages.
        raw = json.dumps({'document':metadata, 'blocks':blocks}, ensure_ascii=False, sort_keys=True).encode()
        return FeishuDocument(str(revision), str(metadata.get('title') or entry.title)[:500], raw,
                              converted.markdown.encode(), tuple(converted.warnings), tuple(converted.assets))
