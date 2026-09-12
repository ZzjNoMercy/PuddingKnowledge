"""Portable historical Wiki lineage fields exposed by Catalog Asset reads."""
import re

_SLUG = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*')
_JOB = re.compile(r'wiki-[a-z0-9-]{1,160}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_TIME = re.compile(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z')
_FIELDS = ('historical_consumed', 'historical_compiled_pages', 'historical_job_ids',
           'historical_compiled_at', 'historical_receipt_digests', 'historical_retirements')


def public_wiki_lineage(metadata):
    if not isinstance(metadata, dict):
        raise ValueError('Invalid Wiki metadata')
    if not any(field in metadata for field in _FIELDS):
        return None  # Prior workspace versions have no historical projection.
    result = {field: metadata.get(field) for field in _FIELDS}
    if type(result['historical_consumed']) is not bool:
        raise ValueError('Invalid Wiki lineage consumption')
    for field, pattern in (('historical_compiled_pages', _SLUG), ('historical_job_ids', _JOB), ('historical_receipt_digests', _DIGEST)):
        values = result[field]
        if not isinstance(values, list) or len(values) > 100000 or any(not isinstance(v, str) or not pattern.fullmatch(v) for v in values):
            raise ValueError('Invalid Wiki lineage values')
    at = result['historical_compiled_at']
    if at is not None and (not isinstance(at, str) or not _TIME.fullmatch(at)):
        raise ValueError('Invalid Wiki lineage time')
    events = result['historical_retirements']
    if not isinstance(events, list) or len(events) > 100000:
        raise ValueError('Invalid Wiki retirement history')
    fields = {'slug': _SLUG, 'replacement': _SLUG, 'job_id': _JOB, 'retired_at': _TIME, 'receipt_digest': _DIGEST}
    for event in events:
        if not isinstance(event, dict) or set(event) != set(fields) or any(not isinstance(event[k], str) or not pattern.fullmatch(event[k]) for k, pattern in fields.items()):
            raise ValueError('Invalid Wiki retirement event')
    return result
