import type { NextRequest } from "next/server";

import { proxyPlatformRequest } from "@/lib/platform-proxy";

export const dynamic = "force-dynamic";

type RouteContext = { params: { path: string[] } };

function pathname(context: RouteContext): string {
  return `/v1/${context.params.path.join("/")}`;
}

export function GET(request: NextRequest, context: RouteContext) {
  return proxyPlatformRequest(request, pathname(context));
}

export function POST(request: NextRequest, context: RouteContext) {
  return proxyPlatformRequest(request, pathname(context));
}
