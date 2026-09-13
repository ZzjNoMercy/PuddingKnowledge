"""Read published bytes from the same durable Catalog used by compilation."""
from __future__ import annotations

import hashlib

from knowledge_contracts import BlobReadResult, CitationCandidate
from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
from knowledge_platform.retrieval.local import _portable_quote
from knowledge_platform.retrieval.ports import RetrievalProviderError


class PublishedWikiReader:
    def __init__(self, repository, services):
        self.repository, self.services = repository, services

    async def search(self, *, query, space_id, limit):
        # Publications are visible only after their Catalog transaction commits.
        result = []
        snapshot=getattr(self.services,'search_snapshot',None)
        try:
            items=snapshot(space_id) if snapshot is not None else ((asset,None) for asset in self.repository.list_assets(space_id=space_id))
        except (LookupError,ValueError) as error:
            raise RetrievalProviderError('Published Wiki is unavailable') from error
        for asset,content in items:
            uri = str(asset.get('source_uri') or '')
            if asset.get('kind') != 'wiki_page' or asset.get('source_type') not in getattr(self.services, 'source_types', ('local_wiki_compilation',)):
                continue
            try:
                if content is None:content = self.services.read_published(uri)
            except (LookupError, ValueError) as error:
                raise RetrievalProviderError('Published Wiki is unavailable') from error
            digest = 'sha256:' + hashlib.sha256(content).hexdigest()
            if digest != asset.get('content_digest'):
                raise RetrievalProviderError('Published Wiki digest differs from Catalog')
            text = str(asset.get('title') or '') + '\n' + content.decode('utf-8')
            position = text.casefold().find(query.casefold())
            if position < 0:
                continue
            result.append(CitationCandidate(asset_id=asset['id'], resource_uri=uri,
                quote=_portable_quote(text[max(0, position-100):position+len(query)+100]),
                locator={'section': 'published_wiki'}, score=1.0))
            if len(result) >= limit:
                break
        return tuple(result)

    async def read(self, request):
        try:
            content = self.services.read_published(request.resource_uri)
        except (LookupError, ValueError) as error:
            raise RetrievalProviderError('Published Wiki is unavailable') from error
        asset_digest = 'sha256:' + hashlib.sha256(content).hexdigest()
        end = min(len(content), request.end if request.end is not None else request.start + MAX_BLOB_READ_BYTES)
        selected = content[request.start:end]
        return BlobReadResult(resource_uri=request.resource_uri, content=selected,
            content_digest='sha256:' + hashlib.sha256(selected).hexdigest(),
            start=request.start, end=request.start + len(selected), asset_digest=asset_digest)
