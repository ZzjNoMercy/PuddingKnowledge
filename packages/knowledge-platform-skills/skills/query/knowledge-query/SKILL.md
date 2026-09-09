---
name: knowledge-query
description: Query a Knowledge Platform Collection through its public query contract.
operations:
  - knowledge_list
  - knowledge_search
  - knowledge_read
  - knowledge_query
  - document_rag_query
  - wiki_query
  - table_query
---

# Knowledge Query

Use `knowledge_list` to enumerate bounded Catalog metadata in an authorized
Space, `knowledge_search` to find registered Assets, and `knowledge_read` to
read a bounded range from an explicitly identified Asset. These operations
return metadata or content with the same scope and provenance fence; they do
not discover files by scanning a host directory.

For a mixed Collection, call `knowledge_query` with the user's question and
the requested Space or Collection scope. The service chooses one primary
Capability for the request and returns a bounded result with Evidence and
Provenance.

Use a specialized operation only when the user has explicitly identified the
kind of source:

- `document_rag_query` for document retrieval;
- `wiki_query` for published Wiki pages;
- `table_query` for a registered Structured Asset or logical Dataset.

Do not combine specialized operations into an unstated fusion query. Do not
send local paths, SQL, model identifiers, or host task identifiers. A result
must retain its returned Collection/Space scope, revision and content digest.

If the service reports that a source is pending, missing, stale or outside the
caller's scope, report that boundary instead of guessing another source.
