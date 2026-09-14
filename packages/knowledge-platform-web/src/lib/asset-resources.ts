export type AssetResource = { id: string; name: string; mime_type: string; size_bytes: number; url: string };

const RESOURCE_ID = /^[a-f0-9]{64}$/;
// Keep this in sync with backend/knowledge_platform/router/ports.py::_ID_RE.
const ASSET_ID = /^[A-Za-z0-9._:-]{1,160}$/;
const INLINE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

function stringOf(value: unknown): string { return typeof value === "string" ? value : ""; }

export function isInlineImageMimeType(value: string): boolean { return INLINE_IMAGE_TYPES.has(value.toLowerCase()); }

export function assetResourcePath(assetId: string, resourceId: string): string | null {
  if (!ASSET_ID.test(assetId) || !RESOURCE_ID.test(resourceId)) return null;
  return `/v1/assets/${assetId}/resources/${resourceId}`;
}

export function safeAssetResourceUrl(assetId: string, resourceId: string, value: unknown, origin = typeof window === "undefined" ? "http://localhost" : window.location.origin): string | null {
  const path = assetResourcePath(assetId, resourceId);
  if (!path || typeof value !== "string") return null;
  try {
    const url = new URL(value, origin);
    const expected = new URL(path, origin);
    return url.origin === expected.origin && !url.username && !url.password && url.pathname === expected.pathname && !url.search && !url.hash ? url.href : null;
  } catch { return null; }
}

export function parseAssetResources(assetId: string, value: unknown, origin = typeof window === "undefined" ? "http://localhost" : window.location.origin): AssetResource[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) return [];
  const resources = (value as Record<string, unknown>).resources;
  if (!Array.isArray(resources)) return [];
  return resources.flatMap((item): AssetResource[] => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const record = item as Record<string, unknown>;
    const id = stringOf(record.id).toLowerCase();
    const name = stringOf(record.name);
    const mime_type = stringOf(record.mime_type).toLowerCase();
    const size_bytes = typeof record.size_bytes === "number" && Number.isFinite(record.size_bytes) ? record.size_bytes : -1;
    const url = safeAssetResourceUrl(assetId, id, record.url, origin);
    if (!url || !name || !mime_type || !Number.isInteger(size_bytes) || size_bytes < 0) return [];
    return [{ id, name, mime_type, size_bytes, url }];
  });
}
