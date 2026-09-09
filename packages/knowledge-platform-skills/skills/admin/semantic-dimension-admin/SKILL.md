---
name: semantic-dimension-admin
description: Create and explicitly decide Platform semantic-dimension Processing Jobs.
operations:
  - POST /v1/semantic-dimensions
  - POST /v1/semantic-dimensions/jobs/{job_id}:decision
---

# Semantic Dimension Admin

Install or expose this Skill only to a caller with explicit Admin and Space
scope.

Creating a definition queues a Platform Job. The request may include only
portable Asset identifiers and bounded JSON metadata. Source lineage is
validated by the Platform Catalog; do not provide local paths or source
contents through the endpoint.

When a Job reaches `waiting_for_publish_confirmation`, show its summary and
the unresolved business choice. A `confirm` or `reject` decision must include
the exact Job identifier, Space and expected status. Confirmation re-queues a
Job; it does not itself claim that a publication exists. Never publish a
staging artifact by editing files or by inventing a decision.
