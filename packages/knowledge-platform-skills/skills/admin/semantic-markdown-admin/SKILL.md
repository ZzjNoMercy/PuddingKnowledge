# Semantic Markdown Admin

Use only the Platform Admin API for semantic Markdown definitions.

1. Discover definitions with `GET /v1/semantic-assets?space_id=...` before changing anything.
2. Prepare with `POST /v1/semantic-assets`. The response must remain `waiting_for_confirmation`.
3. Show the proposed type, name, aliases, tags, frontmatter, Markdown body, and definition digest to the administrator.
4. Apply only an explicit `confirm` or `reject` through `POST /v1/semantic-assets/{asset_id}:decision`, including `expected_status=waiting_for_confirmation`.

Definitions are Space-scoped. Do not write host files, paths, secrets, legacy session IDs, or activate a definition implicitly. Runtime consumers may resolve only `active` definitions.
