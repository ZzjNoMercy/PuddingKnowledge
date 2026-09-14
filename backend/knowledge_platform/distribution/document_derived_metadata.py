"""Invalidate derived old index metadata when document files are materialized.

This does not rebuild indices. Original snapshots retain the previous values;
the receipt contains only their digests and the names of fields invalidated.
"""
import copy
from ..catalog.rehearsal_runner import _digest


DERIVED = ('llamaindex_chunks', 'vector_index')


def invalidate_derived_metadata(legacy, normalized_after, identities, body_bindings):
    prepared, normalized = copy.deepcopy(legacy), copy.deepcopy(normalized_after)
    documents = {row['id']: row for row in prepared['knowledge_documents']}
    assets = {row['metadata_json']['legacy_document_id']: row for row in normalized['knowledge_assets']}
    receipts = {}
    for native_id, document_id in identities.items():
        if native_id not in body_bindings:
            raise ValueError('Derived metadata invalidation requires verified body binding')
        row, asset = documents[document_id], assets[document_id]
        source_metadata = copy.deepcopy(row.get('doc_metadata') or {})
        current_metadata = asset['metadata_json']
        changed = {}
        for field in DERIVED:
            if field in source_metadata or field in current_metadata:
                changed[field] = {'action': 'removed_for_rebuild',
                                  'legacy_value_digest': _digest(source_metadata[field]) if field in source_metadata else None,
                                  'current_value_digest': _digest(current_metadata[field]) if field in current_metadata else None}
                source_metadata.pop(field, None); current_metadata.pop(field, None)
        if asset['mime_type'] == 'text/markdown':
            value = body_bindings[native_id]['sha256']
            changed['markdown_sha256'] = {'action': 'recomputed_from_body',
                                         'legacy_value_digest': _digest(source_metadata['markdown_sha256']) if 'markdown_sha256' in source_metadata else None,
                                         'current_value_digest': _digest(current_metadata['markdown_sha256']) if 'markdown_sha256' in current_metadata else None,
                                         'body_sha256': value}
            source_metadata['markdown_sha256'] = value
            current_metadata['markdown_sha256'] = value
        row['doc_metadata'] = source_metadata
        receipts[native_id] = {'legacy_document_id': document_id, 'fields': changed,
                               'indexes_rebuilt': False, 'activation_allowed': False}
    return prepared, normalized, receipts
