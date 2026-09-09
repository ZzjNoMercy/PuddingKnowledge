const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

function normalizedHostname(value) {
  return String(value || "").toLowerCase().replace(/^\[|\]$/g, "");
}

/** Return whether a Console endpoint is safe for the local shadow build. */
export function isLocalShadowBaseUrl(value, { pageHostname = globalThis.location?.hostname } = {}) {
  if (typeof value !== "string") return false;
  const baseUrl = value.trim();
  if (baseUrl === "") return LOOPBACK_HOSTS.has(normalizedHostname(pageHostname));
  if (baseUrl.startsWith("/")) return false;
  let parsed;
  try {
    parsed = new URL(baseUrl);
  } catch {
    return false;
  }
  return parsed.protocol === "http:"
    && LOOPBACK_HOSTS.has(normalizedHostname(parsed.hostname))
    && !parsed.username
    && !parsed.password
    && !parsed.search
    && !parsed.hash
    && (parsed.pathname === "" || parsed.pathname === "/");
}

export function assertLocalShadowBaseUrl(value, options) {
  if (!isLocalShadowBaseUrl(value, options)) {
    throw new TypeError("本地 shadow Console 仅允许 HTTP loopback Platform API（127.0.0.1、localhost 或 ::1）");
  }
  return typeof value === "string" ? value.trim() : value;
}

/** Resolve an optional launcher-provided API URL without trusting page input. */
export function localShadowApiUrlFromPageUrl(pageUrl, { fallback = "" } = {}) {
  if (typeof pageUrl !== "string") throw new TypeError("本地 Console 页面地址无效");
  let page;
  try {
    page = new URL(pageUrl);
  } catch {
    throw new TypeError("本地 Console 页面地址无效");
  }
  const values = page.searchParams.getAll("api");
  if (values.length > 1) throw new TypeError("本地 Console API 地址不能重复");
  return assertLocalShadowBaseUrl(values.length === 1 ? values[0] : fallback, {
    pageHostname: page.hostname,
  });
}
