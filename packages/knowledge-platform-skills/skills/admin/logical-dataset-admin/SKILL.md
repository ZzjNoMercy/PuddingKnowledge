---
name: logical-dataset-admin
description: Create and publish a validated virtual logical Dataset through Platform Admin and Processing APIs.
operations:
  - POST /v1/datasets
  - POST /v1/datasets/{dataset_id}:publish
---

# Logical Dataset Admin

Install or expose this Skill only to a caller with explicit Admin and Space
scope.

Create a virtual Dataset by naming registered Structured Asset identifiers,
the canonical columns and an explicit schema strategy. The Dataset definition
records source lineage; creation does not silently read arbitrary files.

Publishing resolves source bindings owned by the Platform host. Do not put
`source_paths`, `source_bindings` or filesystem paths in an API body. The
Processing endpoint must revalidate every source's Space, status, capability,
content digest and schema before a ready publication. Pending or failed Jobs
remain non-queryable.
