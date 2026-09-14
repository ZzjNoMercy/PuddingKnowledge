import type { NextRequest } from "next/server";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);
const PLATFORM_PATH = /^\/(?:v1(?:\/[A-Za-z0-9._:+-]+)*|mcp)$/;
const DEFAULT_REQUEST_BODY_LIMIT = 1024 * 1024;
const AUTHORING_REQUEST_BODY_LIMIT = 32 * 1024 * 1024;
const AUTHORING_ACTION_PATHS = new Set([
  "/v1/wiki/authoring/context",
  "/v1/wiki/authoring/preview",
  "/v1/wiki/authoring/apply",
  "/v1/wiki/authoring/generate",
  "/v1/wiki/authoring/proposal",
  "/v1/wiki/authoring/abandon",
  "/v1/wiki/authoring/enqueue",
  "/v1/wiki/authoring/queue",
  "/v1/wiki/authoring/run_queue",
  "/v1/wiki/authoring/control_queue",
]);
const RESPONSE_HEADER_ALLOWLIST = [
  "cache-control",
  "content-disposition",
  "content-security-policy",
  "content-type",
  "referrer-policy",
  "x-content-type-options",
] as const;

export class RequestBodyTooLargeError extends Error {
  constructor() {
    super("Platform request is too large");
    this.name = "RequestBodyTooLargeError";
  }
}

export function requestBodyLimit(pathname: string): number {
  return AUTHORING_ACTION_PATHS.has(pathname)
    ? AUTHORING_REQUEST_BODY_LIMIT
    : DEFAULT_REQUEST_BODY_LIMIT;
}

/** Read a request body while enforcing its actual UTF-8 byte length. */
export async function readRequestBodyWithinLimit(
  request: Pick<Request, "body">,
  maxBytes: number,
): Promise<string | undefined> {
  if (!request.body) return undefined;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = value instanceof Uint8Array ? value : new Uint8Array(value);
      totalBytes += chunk.byteLength;
      if (totalBytes > maxBytes) {
        await reader.cancel();
        throw new RequestBodyTooLargeError();
      }
      chunks.push(chunk);
    }
  } finally {
    reader.releaseLock();
  }
  const body = new Uint8Array(totalBytes);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(body);
}

function platformOrigin(): string {
  const raw = process.env.PLATFORM_API_URL || "http://127.0.0.1:8889";
  const parsed = new URL(raw);
  if (
    parsed.protocol !== "http:" ||
    !LOOPBACK_HOSTS.has(parsed.hostname) ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash ||
    parsed.pathname !== "/"
  ) {
    throw new Error("PLATFORM_API_URL must be an explicit loopback HTTP origin");
  }
  return parsed.origin;
}

export async function proxyPlatformRequest(request: NextRequest, pathname: string): Promise<Response> {
  if (!PLATFORM_PATH.test(pathname)) {
    return Response.json({ error: { code: "invalid_platform_path", message: "Platform path is invalid" } }, { status: 400 });
  }
  const target = new URL(pathname, `${platformOrigin()}/`);
  if (target.pathname !== pathname || (pathname !== "/mcp" && !target.pathname.startsWith("/v1/"))) {
    return Response.json({ error: { code: "invalid_platform_path", message: "Platform path is invalid" } }, { status: 400 });
  }
  target.search = request.nextUrl.search;
  const contentType = request.headers.get("content-type");
  if (contentType && contentType !== "application/json") {
    return Response.json({ error: { code: "unsupported_media_type", message: "Only JSON requests are supported" } }, { status: 415 });
  }
  const maxBodyBytes = requestBodyLimit(pathname);
  const contentLengthHeader = request.headers.get("content-length");
  const contentLength = contentLengthHeader === null ? 0 : Number(contentLengthHeader);
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > maxBodyBytes) {
    return Response.json({ error: { code: "request_too_large", message: "Platform request is too large" } }, { status: 413 });
  }
  let body: string | undefined;
  try {
    body = request.method === "GET" || request.method === "HEAD"
      ? undefined
      : await readRequestBodyWithinLimit(request, maxBodyBytes);
  } catch (error) {
    if (error instanceof RequestBodyTooLargeError) {
      return Response.json({ error: { code: "request_too_large", message: "Platform request is too large" } }, { status: 413 });
    }
    throw error;
  }
  try {
    const response = await fetch(target, {
      method: request.method,
      headers: contentType ? { "content-type": contentType } : undefined,
      body,
      cache: "no-store",
    });
    const headers = new Headers();
    for (const name of RESPONSE_HEADER_ALLOWLIST) {
      const value = response.headers.get(name);
      if (value) headers.set(name, value);
    }
    if (!headers.has("content-type")) headers.set("content-type", "application/json");
    return new Response(response.body, { status: response.status, headers });
  } catch {
    return Response.json(
      { error: { code: "platform_unreachable", message: "Knowledge Platform API is unavailable" } },
      { status: 502 },
    );
  }
}
