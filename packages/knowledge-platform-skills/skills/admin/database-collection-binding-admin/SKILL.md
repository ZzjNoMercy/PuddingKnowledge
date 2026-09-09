# Database Collection Binding Admin

Use `POST /v1/database/bindings` to bind one existing Database Dataset to one
Collection. The request contains only `collection_id`, `collection_version`,
`space_id`, and `dataset_id`; host paths, connection URLs, usernames,
passwords, credential references, and SQL are forbidden. The server validates
the host-owned source before persisting the `database_nl2sql` provider binding.

The operation requires Admin or Processing authorization and the matching
Space scope. A successful response is still an administrative binding result;
it does not activate a production cutover.
