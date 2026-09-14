# Verified document resources over HTTP

Migrated document workspaces now expose the dependency graph preserved by the v4 owned manifest. The local application composes this service for standalone and combined workspaces. GET `/v1/assets/{asset_id}/resources` lists verified reachable resources; GET `/v1/assets/{asset_id}/resources/{resource_id}` returns the bytes. The caller supplies an opaque digest identifier, never a filesystem path.

Every request requires current read permission for the parent document and every migrated document whose dependency closure contains the resource. Current Catalog space, content digest and source URI must match the owned snapshot; a Catalog revision change during the request rejects the result. Shared dependencies use the intersection of all owner permissions. Listing conceals inaccessible dependencies, while direct reads reject them.

Each read validates the private file path, rejects symlinks/hardlinks, and compares size and SHA256 against the owned manifest. Raster signatures select PNG/JPEG/GIF/WebP preview MIME types. All other bytes, including Markdown, HTML and SVG, are download-only. Responses carry no-store, nosniff, sandbox CSP and no-referrer headers. The same-origin Web proxy streams binary bytes and forwards only the selected response headers.

The Asset drawer reads document bodies, shows raster resources and offers downloads for other resources. Resource request errors remain separate from the body result; stale or closed drawer requests cannot overwrite the current selection. Resource URLs must match the current origin and exact parent/opaque identifier route without credentials, query or fragment.

This is a read-only migrated dependency surface. Detached metadata attachments and original PDFs that are not reachable in the document graph remain preserved but are not exposed by these routes. Live resource replacement, Markdown link rewriting/rendering, full migration activation and production release remain separate work.

Validation: focused HTTP tests cover principal/space/tenant rejection, shared cross-space dependencies, symlink replacement, content tampering and download-only behavior. The installed CLI proof exercises real loopback HTTP, instance identity, removal of source and candidate, owned-state restart and post-start tamper rejection. Web acceptance checks production build and actual browser image decoding through the binary proxy.
