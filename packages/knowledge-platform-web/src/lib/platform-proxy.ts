import type { NextRequest } from "next/server";

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);
const PLATFORM_PATH = /^\/(?:v1(?:\/[A-Za-z0-9._:+-]+)*|mcp)$/;

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
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (!Number.isFinite(contentLength) || contentLength < 0 || contentLength > 1024 * 1024) {
    return Response.json({ error: { code: "request_too_large", message: "Platform request is too large" } }, { status: 413 });
  }
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.text();
  try {
    const response = await fetch(target, {
      method: request.method,
      headers: contentType ? { "content-type": contentType } : undefined,
      body,
      cache: "no-store",
    });
    return new Response(response.body, {
      status: response.status,
      headers: { "content-type": response.headers.get("content-type") || "application/json" },
    });
  } catch {
    return Response.json(
      { error: { code: "platform_unreachable", message: "Knowledge Platform API is unavailable" } },
      { status: 502 },
    );
  }
}
