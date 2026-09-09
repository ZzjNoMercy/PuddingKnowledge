import type { NextRequest } from "next/server";

import { proxyPlatformRequest } from "@/lib/platform-proxy";

export const dynamic = "force-dynamic";

export function POST(request: NextRequest) {
  return proxyPlatformRequest(request, "/mcp");
}
