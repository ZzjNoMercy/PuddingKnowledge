# Forward document dependency preservation

Document migration v3 preserves a bounded source-relative resources tree alongside
content-addressed blobs. Source bodies and original PDFs keep their independent
digest commitments. The resources tree contains the exact primary files, supported
transitive Markdown/static HTML dependencies, and explicitly bound known attachment
files/directories (including empty directories). Query bindings in the owned
workspace select resources paths, so parent-relative images and linked documents
resolve in their original context. The tree's primary hash must equal its Catalog
blob hash; a parallel unverified body cannot replace it.

Use the existing --bindings for document bodies and --original-bindings for
converted PDF originals. --attachment-bindings maps every other known absolute
metadata reference to a canonical path under the approved source-files root:
assets[*].path, original_path and multimodal.image_assets_dir. Converted PDF
original references are filled from --original-bindings; an explicit conflicting
attachment mapping rejects. Missing/extra/escaping/linked paths, inconsistent
directory/file mappings and wrong current declared attachment hashes/sizes reject.

The private plan commits full tree file facts, directories, graph and body mapping.
Each file is rechecked after discovery and before completion; declared attachment
directories are re-inventoried to detect late additions. Private copying checkpoints
support exact retry and real SIGKILL, including during resource publication.
Completed missing files or empty directories reject without repair. Package
metadata is bounded to 1 MiB and actual output bytes (including blob/tree duplicates)
count toward the existing 256 MiB budget. Unsupported CSS/dynamic content rejects;
remote URLs are recorded but never fetched. Embedded binary references are opaque.

Owned document workspace v4 validates its complete resources inventory on load.
Combined Knowledge workspaces carry this tree along with Wiki evidence. Older
workspace versions reject unregistered resources instead of silently accepting
new files. Ordinary v1/v2 packages remain readable without a tree. The outer
Claw migration request accepts optional original_bindings/attachment_bindings
objects; its receipt commits resource files and revalidates the complete tree
before completion. It still reports verified_inactive_partial, not PREPARED.

This closes supported local dependency preservation across source removal and
owned-workspace restart. It does not prove image HTTP serving, parser quality,
index rebuild, every Catalog domain, full installation cutover/thaw, credential
continuity, external stores or production release acceptance.
