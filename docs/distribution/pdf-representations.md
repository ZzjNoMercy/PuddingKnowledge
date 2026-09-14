# PDF source and body representations

A converted PDF has two independently verified byte objects. Legacy source_path
and content_sha256 identify the original PDF; storage_path, MIME text/markdown,
size_bytes and metadata.markdown_sha256 identify derived Markdown. The converter
must not accept the PDF itself as the Markdown body because its original hash
matches the legacy content hash.

Document migration v3 keeps the existing --bindings JSON mapping document IDs to
snapshot-relative Markdown paths. It additionally requires --original-bindings
JSON mapping exactly the converted PDF document IDs to their snapshot-relative
original PDF paths. Both are read and hashed independently against their distinct
Catalog claims; Markdown size is checked. Incomplete PDF declarations, missing
originals, swapped representations and wrong hashes reject. No parser or network
service is called; the existing parsed Markdown is migrated as data.

Canonical converted-PDF Asset content_digest/revision now identify Markdown.
Original identity remains in metadata and a distinct content-addressed blob. The
v3 package plan records original_bindings by Asset ID; its complete inventory
commits both representations. Source files are rechecked and completed missing
originals reject without repair. Owned document workspace version 4 preserves
original blobs and verifies their Catalog identity on every restart. Query file
bindings still select Markdown. Combined Knowledge workspaces accept this version.

Ordinary v1 packages and document workspace v2 remain readable. Previously
produced PDF packages/workspaces lacking a separately verified original reject;
regenerate from immutable source snapshots with the current tool rather than silently upgrading
a potentially misidentified body. Previous output plans remain immutable.

Document reverse v4 supports existing and new native PDFs with current Markdown
body bindings plus --attachment-bindings for metadata.original_path (and other
known attachments). It preserves original PDF digest in legacy content_sha256 and
binds source_path to the candidate original file. storage_path and size describe
Markdown. Both representations can change after migration; current verified
bytes and metadata are restored while original snapshots remain untouched.

Validation uses an actual 18-table legacy schema and a complete generated PDF,
independent Markdown bytes, owned-workspace restart and old-schema file reads.
This proves byte identity and representation preservation, not PDF parsing
quality, virtual routing, index rebuild, production activation or audited thaw.

The v3 resources tree also preserves source-relative PDF image dependencies;
see forward-document-dependencies.md for explicit attachment bindings and budgets.
