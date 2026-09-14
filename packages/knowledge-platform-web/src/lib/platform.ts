import {
  createPlatformClient,
  type Evidence,
  type PlatformClient,
  type QueryResult,
} from "@puddingai/knowledge-platform-console-contracts";
export { isInlineImageMimeType, parseAssetResources, safeAssetResourceUrl } from "./asset-resources";
export type { AssetResource } from "./asset-resources";

export type PortableRecord = Record<string, unknown>;


export const platformClient: PlatformClient = createPlatformClient({ baseUrl: "" });

export function errorMessage(reason: unknown): string {
  if (reason instanceof Error) return reason.message;
  return String(reason || "请求失败");
}

export function dataOf(result: QueryResult): PortableRecord {
  if (result.status === "error") {
    throw new Error(result.error?.message || result.error?.code || "Platform 请求失败");
  }
  return result.data || {};
}

export function records(value: unknown): PortableRecord[] {
  return Array.isArray(value) ? value.filter((item): item is PortableRecord => Boolean(item) && typeof item === "object" && !Array.isArray(item)) : [];
}

export function stringOf(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}


export function numberOf(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

export function evidenceOf(result: QueryResult): Evidence[] {
  return Array.isArray(result.evidence) ? result.evidence : [];
}

export function decodeAssetContent(data: PortableRecord): string {
  const encoded = stringOf(data.content_base64);
  if (!encoded) return "";
  try {
    const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
    return new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  } catch {
    return "该内容不是可直接预览的 UTF-8 文本。";
  }
}

export function shortDigest(value: unknown): string {
  const text = stringOf(value);
  if (!text) return "—";
  return text.length > 22 ? `${text.slice(0, 13)}…${text.slice(-7)}` : text;
}

export function relativeTime(value: unknown): string {
  const text = stringOf(value);
  const timestamp = Date.parse(text);
  if (!Number.isFinite(timestamp)) return "—";
  const minutes = Math.max(1, Math.floor((Date.now() - timestamp) / 60000));
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前`;
  if (minutes < 43200) return `${Math.floor(minutes / 1440)} 天前`;
  return new Date(timestamp).toLocaleDateString("zh-CN");
}
