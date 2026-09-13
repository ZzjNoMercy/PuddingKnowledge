const ID_RE = /^[A-Za-z0-9._:-]{1,160}$/;
const URI_RE = /^knowledge:\/\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}(?:\/[A-Za-z0-9][A-Za-z0-9._:-]{0,159})+$/;
const PATH_RE = /^(?:\/mcp|\/v1\/[A-Za-z0-9._:/{}?=&%+-]+)$/;
const MCP_RESOURCE_TEMPLATE_RE = /^knowledge:\/\/[A-Za-z0-9][A-Za-z0-9._-]{0,95}(?:\/[A-Za-z0-9{}._:-]{1,160})+$/;
const NON_PORTABLE_CONTENT_RE = /(?:file:\/\/|(?:^|[^A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|(?:^|[\s(=])\/(?:[^\s]+)|(?:^|[\s(])~\/)/i;
const NON_PORTABLE_EVIDENCE_TEXT_RE = /(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|(?:https?:\/\/|file:|(?:^|[^A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|(?:^|[\s(=])\/(?:[^\s]+)|(?:^|[\s(])~\/)/i;
const NON_PORTABLE_KEYS = new Set(["source_path", "file_path", "physical_path", "artifact_path", "raw_markdown", "published_markdown"]);
const EVIDENCE_LOCATOR_KEYS = new Set(["page", "line_start", "line_end", "section", "chunk_id"]);
const DIGEST_RE = /^sha256:[0-9a-f]{64}$/;
const WIKI_AUTHORING_ACTIONS = new Set([
  "context", "preview", "apply", "generate", "proposal", "abandon",
  "enqueue", "queue", "run_queue", "control_queue",
]);

function assertObject(value, label) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`);
  }
  return value;
}

function assertId(value, label) {
  if (typeof value !== "string" || !ID_RE.test(value)) throw new TypeError(`${label} is invalid`);
  return value;
}

function assertPath(path) {
  if (typeof path !== "string" || !PATH_RE.test(path)) throw new TypeError("Platform API path is invalid");
  return path;
}

function assertMcpResourceUri(uri, label = "MCP resource URI") {
  if (typeof uri !== "string" || !URI_RE.test(uri)) throw new TypeError(`${label} is not portable`);
  return uri;
}

function assertPortableText(value, label) {
  if (typeof value !== "string"
    || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)
    || NON_PORTABLE_CONTENT_RE.test(value)) {
    throw new TypeError(`${label} is not portable`);
  }
  return value;
}

function assertPortableEvidenceText(value, label, maxLength) {
  if (typeof value !== "string"
    || value.length > maxLength
    || [...value].some((character) => {
      const code = character.codePointAt(0);
      return code !== undefined && code < 32 && !"\t\n\r".includes(character);
    })
    || NON_PORTABLE_EVIDENCE_TEXT_RE.test(value)) {
    throw new TypeError(`${label} is not portable`);
  }
  return value;
}

function assertEvidenceLocatorValue(value) {
  if (typeof value === "string") {
    assertPortableEvidenceText(value, "Evidence.locator", 256);
    if (!value.trim()) throw new TypeError("Evidence.locator is not portable");
    return value;
  }
  if (typeof value === "number") {
    if (Number.isFinite(value)) return value;
    throw new TypeError("Evidence.locator is not portable");
  }
  if (typeof value === "boolean") return value;
  throw new TypeError("Evidence.locator is not portable");
}

function assertPortableStructuredContent(value) {
  if (typeof value === "string") {
    assertPortableText(value, "MCP structuredContent");
  } else if (Array.isArray(value)) {
    value.forEach(assertPortableStructuredContent);
  } else if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, child]) => {
      if (NON_PORTABLE_KEYS.has(key)) throw new TypeError("MCP structuredContent contains a non-portable field");
      assertPortableText(key, "MCP structuredContent key");
      assertPortableStructuredContent(child);
    });
  }
}

function parseMcpResourceList(payload) {
  const result = assertObject(payload, "MCP resources/list result");
  if (!Array.isArray(result.resources) || !Array.isArray(result.resourceTemplates)) {
    throw new TypeError("MCP resources/list result is invalid");
  }
  const resources = result.resources.map((resource) => {
    const item = assertObject(resource, "MCP resource");
    assertMcpResourceUri(item.uri);
    if (item.name !== undefined) assertPortableText(item.name, "MCP resource name");
    if (item.description !== undefined) assertPortableText(item.description, "MCP resource description");
    if (item.mimeType !== undefined && typeof item.mimeType !== "string") throw new TypeError("MCP resource mimeType is invalid");
    return item;
  });
  const resourceTemplates = result.resourceTemplates.map((template) => {
    const item = assertObject(template, "MCP resource template");
    if (typeof item.uriTemplate !== "string" || !MCP_RESOURCE_TEMPLATE_RE.test(item.uriTemplate)) {
      throw new TypeError("MCP resource template is not portable");
    }
    if (item.name !== undefined) assertPortableText(item.name, "MCP resource template name");
    if (item.description !== undefined) assertPortableText(item.description, "MCP resource template description");
    if (item.mimeType !== undefined && typeof item.mimeType !== "string") throw new TypeError("MCP resource template mimeType is invalid");
    return item;
  });
  return { ...result, resources, resourceTemplates };
}

function parseMcpResourceRead(payload) {
  const result = assertObject(payload, "MCP resources/read result");
  if (!Array.isArray(result.contents)) throw new TypeError("MCP resources/read contents are invalid");
  const contents = result.contents.map((content) => {
    const item = assertObject(content, "MCP resource content");
    assertMcpResourceUri(item.uri, "MCP resource content URI");
    if (item.mimeType !== undefined && typeof item.mimeType !== "string") throw new TypeError("MCP resource content mimeType is invalid");
    if (item.text !== undefined) assertPortableText(item.text, "MCP resource text");
    if (item.blob !== undefined && typeof item.blob !== "string") throw new TypeError("MCP resource blob is invalid");
    if (item.text === undefined && item.blob === undefined) throw new TypeError("MCP resource content is empty");
    return item;
  });
  if (result.structuredContent !== undefined) {
    assertObject(result.structuredContent, "MCP structuredContent");
    assertPortableStructuredContent(result.structuredContent);
  }
  if (result.isError !== undefined && typeof result.isError !== "boolean") throw new TypeError("MCP isError is invalid");
  return { ...result, contents };
}

function urlFor(baseUrl, path) {
  assertPath(path);
  if (baseUrl === "") return path;
  if (baseUrl.startsWith("/")) {
    if (baseUrl.includes("?") || baseUrl.includes("#") || baseUrl.includes("//")) {
      throw new TypeError("relative Platform API baseUrl is invalid");
    }
    return `${baseUrl.replace(/\/$/, "")}${path}`;
  }
  const parsed = new URL(baseUrl);
  if (!(parsed.protocol === "http:" || parsed.protocol === "https:")
    || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new TypeError("Platform API baseUrl is invalid");
  }
  return new URL(path.slice(1), `${parsed.toString().replace(/\/$/, "")}/`).toString();
}

function queryPath(path, values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null) query.set(key, String(value));
  }
  const suffix = query.toString();
  return suffix ? `${path}?${suffix}` : path;
}

function validateEvidence(evidence) {
  if (!Array.isArray(evidence)) throw new TypeError("QueryResult.evidence must be an array");
  for (const item of evidence) {
    assertObject(item, "Evidence");
    assertId(item.asset_id, "Evidence.asset_id");
    if (typeof item.resource_uri !== "string" || !URI_RE.test(item.resource_uri)) {
      throw new TypeError("Evidence.resource_uri must be portable");
    }
    if (item.locator !== undefined) {
      assertObject(item.locator, "Evidence.locator");
      for (const [key, value] of Object.entries(item.locator)) {
        if (!EVIDENCE_LOCATOR_KEYS.has(key)) throw new TypeError("Evidence.locator contains an unsupported field");
        assertEvidenceLocatorValue(value);
      }
    }
    if (item.quote !== undefined) assertPortableEvidenceText(item.quote, "Evidence.quote", 1200);
    if (item.revision !== undefined && (typeof item.revision !== "string" || (item.revision !== "" && !DIGEST_RE.test(item.revision)))) {
      throw new TypeError("Evidence.revision is invalid");
    }
    if (item.score !== undefined && item.score !== null && (typeof item.score !== "number" || !Number.isFinite(item.score) || item.score < 0 || item.score > 1)) {
      throw new TypeError("Evidence.score is invalid");
    }
    if (item.matched_by !== undefined && (!Array.isArray(item.matched_by) || item.matched_by.some((value) => {
      try {
        assertId(value, "Evidence.matched_by");
        return false;
      } catch {
        return true;
      }
    }))) {
      throw new TypeError("Evidence.matched_by is invalid");
    }
  }
}

/** Validate the public result envelope without interpreting provider data. */
export function parseQueryResult(payload) {
  const result = assertObject(payload, "QueryResult");
  if (result.status !== "ok" && result.status !== "error") throw new TypeError("QueryResult.status is invalid");
  if (result.status === "error") {
    assertObject(result.error, "QueryResult.error");
    if (typeof result.error.code !== "string" || typeof result.error.message !== "string") {
      throw new TypeError("QueryResult.error is invalid");
    }
    assertPortableText(result.error.message, "QueryResult.error.message");
  }
  if (result.evidence !== undefined) validateEvidence(result.evidence);
  if (result.answer !== undefined) assertPortableText(result.answer, "QueryResult.answer");
  if (result.data !== undefined) {
    assertObject(result.data, "QueryResult.data");
    assertPortableStructuredContent(result.data);
  }
  return result;
}

export { parseMcpResourceList, parseMcpResourceRead };

export class PlatformApiError extends Error {
  constructor(status, code = "platform_request_failed") {
    super(`Platform API request failed (${status})`);
    this.name = "PlatformApiError";
    this.status = status;
    this.code = code;
  }
}

async function decodeResponse(response) {
  let body;
  try {
    body = await response.json();
  } catch {
    throw new PlatformApiError(response.status, "invalid_json_response");
  }
  if (!response.ok) {
    const code = body && typeof body === "object" && body.error && typeof body.error.code === "string"
      ? body.error.code
      : "platform_request_failed";
    throw new PlatformApiError(response.status, code);
  }
  return parseQueryResult(body);
}

function bodyFor(value) {
  if (value === undefined) return undefined;
  assertObject(value, "Platform API request body");
  const forbidden = new Set(["source_path", "file_path", "physical_path", "published_markdown", "raw_markdown"]);
  const inspect = (item) => {
    for (const [key, child] of Object.entries(item)) {
      if (forbidden.has(key)) throw new TypeError(`Platform API request field ${key} is not portable`);
      if (Array.isArray(child)) {
        for (const entry of child) {
          if (entry && typeof entry === "object" && !Array.isArray(entry)) inspect(entry);
        }
      } else if (child && typeof child === "object") {
        inspect(child);
      }
    }
  };
  inspect(value);
  return JSON.stringify(value);
}

/** Create a browser/Node-compatible client for the independent Platform API. */
export function createPlatformClient({ baseUrl = "", fetchImpl = globalThis.fetch, headers = {} } = {}) {
  if (typeof baseUrl !== "string" || typeof fetchImpl !== "function") throw new TypeError("Platform client options are invalid");
  assertObject(headers, "Platform client headers");
  const request = async (method, path, body, signal) => {
    const response = await fetchImpl(urlFor(baseUrl, path), {
      method,
      headers: { ...headers, ...(body === undefined ? {} : { "content-type": "application/json" }) },
      ...(body === undefined ? {} : { body: bodyFor(body) }),
      signal,
    });
    return decodeResponse(response);
  };
  let mcpRequestId = 0;
  const mcpRequest = async (method, params, signal) => {
    const response = await fetchImpl(urlFor(baseUrl, "/mcp"), {
      method: "POST",
      headers: { ...headers, "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: `console-mcp-${++mcpRequestId}`, method, params }),
      signal,
    });
    let body;
    try {
      body = await response.json();
    } catch {
      throw new PlatformApiError(response.status, "invalid_json_response");
    }
    if (!response.ok || !body || typeof body !== "object") {
      throw new PlatformApiError(response.status, "platform_request_failed");
    }
    if (body.error && typeof body.error === "object") {
      throw new PlatformApiError(response.status, typeof body.error.code === "string" ? body.error.code : "mcp_request_failed");
    }
    if (!body.result || typeof body.result !== "object") {
      throw new PlatformApiError(response.status, "invalid_mcp_response");
    }
    return body.result;
  };
  return Object.freeze({
    listSpaces: ({ signal } = {}) => request("GET", "/v1/spaces", undefined, signal),
    listCollections: ({ spaceId, signal } = {}) => request("GET", queryPath("/v1/collections", { space_id: spaceId }), undefined, signal),
    // Compatibility alias. New callers should use listCollections.
    listDatasets: ({ spaceId, signal } = {}) => request("GET", queryPath("/v1/datasets", { space_id: spaceId }), undefined, signal),
    listAssets: ({ spaceId, signal } = {}) => request("GET", queryPath("/v1/assets", { space_id: spaceId }), undefined, signal),
    readAsset: (assetId, body = {}, { signal } = {}) => request("POST", `/v1/assets/${assertId(assetId, "assetId")}:read`, body, signal),
    listAssetDerivatives: (assetId, { signal } = {}) => request(
      "GET", `/v1/assets/${assertId(assetId, "assetId")}/derivatives`, undefined, signal,
    ),
    readAssetDerivative: (assetId, kind, { start = 0, end, expectedDigest, signal } = {}) => request(
      "GET",
      queryPath(`/v1/assets/${assertId(assetId, "assetId")}/derivatives/${assertId(kind, "kind")}`, {
        start, end, expected_digest: expectedDigest,
      }),
      undefined,
      signal,
    ),
    getQueryResult: (queryResultId, { signal } = {}) => request(
      "GET", `/v1/query-results/${assertId(queryResultId, "queryResultId")}`, undefined, signal,
    ),
    getJob: (jobId, { signal } = {}) => request(
      "GET", `/v1/jobs/${assertId(jobId, "jobId")}`, undefined, signal,
    ),
    search: (body = {}, { signal } = {}) => request("POST", "/v1/search", body, signal),
    documentRagQuery: (body = {}, { signal } = {}) => request("POST", "/v1/document-rag/query", body, signal),
    wikiQuery: (body = {}, { signal } = {}) => request("POST", "/v1/wiki/query", body, signal),
    wikiAuthoring: (action, body = {}, { signal } = {}) => {
      if (typeof action !== "string" || !WIKI_AUTHORING_ACTIONS.has(action)) {
        throw new TypeError("Wiki authoring action is invalid");
      }
      return request("POST", `/v1/wiki/authoring/${action}`, body, signal);
    },
    tableQuery: (body = {}, { signal } = {}) => request("POST", "/v1/table/query", body, signal),
    query: (body = {}, { signal } = {}) => request("POST", "/v1/query", body, signal),
    knowledgeQuery: (body = {}, { signal } = {}) => request("POST", "/v1/knowledge/query", body, signal),
    databaseNl2Sql: (body = {}, { signal } = {}) => request("POST", "/v1/database/nl2sql", body, signal),
    listDatabaseSchema: ({ spaceId, datasetId, signal } = {}) => request(
      "GET", queryPath("/v1/database/schema", { space_id: spaceId, dataset_id: datasetId }), undefined, signal,
    ),
    executeDatabasePlan: (queryPlanId, body = {}, { signal } = {}) => request(
      "POST",
      `/v1/database/query-plans/${assertId(queryPlanId, "queryPlanId")}:execute`,
      body,
      signal,
    ),
    listMcpResources: ({ signal } = {}) => mcpRequest("resources/list", {}, signal).then(parseMcpResourceList),
    readMcpResource: (resourceUri, { start = 0, end = 8 * 1024 * 1024, signal } = {}) => {
      assertMcpResourceUri(resourceUri);
      if (!Number.isInteger(start) || start < 0 || !Number.isInteger(end) || end <= start || end - start > 8 * 1024 * 1024) {
        throw new TypeError("MCP resource range is invalid");
      }
      return mcpRequest("resources/read", { uri: resourceUri, start, end }, signal).then(parseMcpResourceRead);
    },
    createDataset: (body = {}, { signal } = {}) => request("POST", "/v1/datasets", body, signal),
    publishDataset: (datasetId, body = {}, { signal } = {}) => request("POST", `/v1/datasets/${assertId(datasetId, "datasetId")}:publish`, body, signal),
    listSemanticAssets: ({ spaceId, status: assetStatus, signal } = {}) => request(
      "GET",
      queryPath("/v1/semantic-assets", { space_id: spaceId, status: assetStatus }),
      undefined,
      signal,
    ),
    listConnectors: ({ spaceId, signal } = {}) => request(
      "GET", queryPath("/v1/connectors", { space_id: spaceId }), undefined, signal,
    ),
    listSourceItems: ({ spaceId, connectorId, signal } = {}) => request(
      "GET", queryPath("/v1/source-items", { space_id: spaceId, connector_id: connectorId }), undefined, signal,
    ),
    listConnectorAuthorizations: ({ spaceId, signal } = {}) => request(
      "GET", queryPath("/v1/connector-authorizations", { space_id: spaceId }), undefined, signal,
    ),
    listNotifications: ({ spaceId, limit, signal } = {}) => request(
      "GET", queryPath("/v1/notifications", { space_id: spaceId, limit }), undefined, signal,
    ),
    listAssetBindingReviews: ({ spaceId, signal } = {}) => request(
      "GET", queryPath("/v1/asset-binding-reviews", { space_id: spaceId }), undefined, signal,
    ),
    authorizeConnector: (connectorId, body = {}, { signal } = {}) => request(
      "POST", `/v1/connectors/${assertId(connectorId, "connectorId")}:authorize`, body, signal,
    ),
    uploadAsset: (body = {}, { signal } = {}) => request("POST", "/v1/assets:upload", body, signal),
    importPackage: (body = {}, { signal } = {}) => request("POST", "/v1/packages:import", body, signal),
    rebuildIndex: (body = {}, { signal } = {}) => request("POST", "/v1/indexes:rebuild", body, signal),
    prepareSemanticAsset: (body = {}, { signal } = {}) => request("POST", "/v1/semantic-assets", body, signal),
    decideSemanticAsset: (assetId, body = {}, { signal } = {}) => request("POST", `/v1/semantic-assets/${assertId(assetId, "assetId")}:decision`, body, signal),
    createSemanticDimension: (body = {}, { signal } = {}) => request("POST", "/v1/semantic-dimensions", body, signal),
    decideSemanticDimension: (jobId, body = {}, { signal } = {}) => request("POST", `/v1/semantic-dimensions/jobs/${assertId(jobId, "jobId")}:decision`, body, signal),
    processSemanticDimension: (jobId, body = {}, { signal } = {}) => request("POST", `/v1/semantic-dimensions/jobs/${assertId(jobId, "jobId")}:process`, body, signal),
    createSqlGuardrail: (body = {}, { signal } = {}) => request("POST", "/v1/database/guardrails", body, signal),
    decideSqlGuardrail: (guardrailId, body = {}, { signal } = {}) => request("POST", `/v1/database/guardrails/${assertId(guardrailId, "guardrailId")}:decision`, body, signal),
    bindDatabaseCollection: (body = {}, { signal } = {}) => request("POST", "/v1/database/bindings", body, signal),
    observeCollectionFreshness: (body = {}, { signal } = {}) => request("POST", "/v1/collections/freshness", body, signal),
    compileWiki: (assetId, body = {}, { signal } = {}) => request("POST", `/v1/wiki/assets/${assertId(assetId, "assetId")}:compile`, body, signal),
    processCapture: (assetId, body = {}, { signal } = {}) => request("POST", `/v1/captures/assets/${assertId(assetId, "assetId")}:process`, body, signal),
    projectGbrain: (assetId, body = {}, { signal } = {}) => request("POST", `/v1/wiki/assets/${assertId(assetId, "assetId")}:project-gbrain`, body, signal),
    syncSource: (connectorId, body = {}, { signal } = {}) => request("POST", `/v1/sources/${assertId(connectorId, "connectorId")}:sync`, body, signal),
    listBitableSources: ({ signal } = {}) => request("GET", "/v1/bitable/sources", undefined, signal),
    getBitablePolicy: (sourceId, { signal } = {}) => request("GET", `/v1/sources/${assertId(sourceId, "sourceId")}/bitable/policy`, undefined, signal),
    updateBitablePolicy: (sourceId, policy, expectedRevision, { signal } = {}) => request("PUT", `/v1/sources/${assertId(sourceId, "sourceId")}/bitable/policy`, { policy, expected_revision: expectedRevision }, signal),
    getBitableSchema: (sourceId, tableId, { signal } = {}) => request("GET", `/v1/sources/${assertId(sourceId, "sourceId")}/bitable/tables/${assertId(tableId, "tableId")}/schema`, undefined, signal),
    getBitableRelations: (sourceId, { signal } = {}) => request("GET", `/v1/sources/${assertId(sourceId, "sourceId")}/bitable/relations`, undefined, signal),
    queryBitable: (sourceId, body = {}, { signal } = {}) => request("POST", `/v1/sources/${assertId(sourceId, "sourceId")}/bitable/query`, body, signal),
  });
}
