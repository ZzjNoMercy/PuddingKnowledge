export interface Evidence {
  asset_id: string;
  resource_uri: string;
  locator?: Record<string, string | number | boolean>;
  quote?: string;
  score?: number;
  revision?: string;
  matched_by?: string[];
}

export interface QueryError {
  code: string;
  message: string;
  retryable?: boolean;
  details?: Record<string, unknown>;
}

export interface QueryResult {
  status: "ok" | "error";
  answer?: string;
  data?: Record<string, unknown>;
  evidence?: Evidence[];
  provenance?: Record<string, unknown> | null;
  warnings?: Array<Record<string, unknown>>;
  trace_id?: string;
  error?: QueryError | null;
}

export interface DatabaseSchemaTable {
  table_name: string;
  columns: string[];
  schema_revision: string;
}

export interface McpResource {
  uri: string;
  name?: string;
  description?: string;
  mimeType?: string;
}

export interface McpResourceTemplate {
  uriTemplate: string;
  name?: string;
  description?: string;
  mimeType?: string;
}

export interface McpResourceList {
  resources: McpResource[];
  resourceTemplates: McpResourceTemplate[];
  [key: string]: unknown;
}

export interface McpResourceContent {
  uri: string;
  mimeType?: string;
  text?: string;
  blob?: string;
}

export interface McpResourceRead {
  contents: McpResourceContent[];
  structuredContent?: Record<string, unknown>;
  isError?: boolean;
  [key: string]: unknown;
}

export class PlatformApiError extends Error {
  readonly status: number;
  readonly code: string;
}

export interface PlatformClientOptions {
  baseUrl?: string;
  fetchImpl?: typeof fetch;
  headers?: Record<string, string>;
}

export interface RequestOptions {
  signal?: AbortSignal;
}

export interface PlatformClient {
  listSpaces(options?: RequestOptions): Promise<QueryResult>;
  listCollections(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  /** @deprecated Use listCollections for the top-level Knowledge resource. */
  listDatasets(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  listAssets(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  readAsset(assetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  listAssetDerivatives(assetId: string, options?: RequestOptions): Promise<QueryResult>;
  readAssetDerivative(assetId: string, kind: string, options?: RequestOptions & { start?: number; end?: number; expectedDigest?: string }): Promise<QueryResult>;
  getQueryResult(queryResultId: string, options?: RequestOptions): Promise<QueryResult>;
  getJob(jobId: string, options?: RequestOptions): Promise<QueryResult>;
  search(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  documentRagQuery(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  wikiQuery(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  tableQuery(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  query(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  knowledgeQuery(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  databaseNl2Sql(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  listDatabaseSchema(options?: RequestOptions & { spaceId?: string; datasetId?: string }): Promise<QueryResult>;
  executeDatabasePlan(queryPlanId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  listMcpResources(options?: RequestOptions): Promise<McpResourceList>;
  readMcpResource(resourceUri: string, options?: RequestOptions & { start?: number; end?: number }): Promise<McpResourceRead>;
  createDataset(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  publishDataset(datasetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  listSemanticAssets(options?: RequestOptions & { spaceId?: string; status?: string }): Promise<QueryResult>;
  listConnectors(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  listSourceItems(options?: RequestOptions & { spaceId?: string; connectorId?: string }): Promise<QueryResult>;
  listConnectorAuthorizations(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  listNotifications(options?: RequestOptions & { spaceId?: string; limit?: number }): Promise<QueryResult>;
  listAssetBindingReviews(options?: RequestOptions & { spaceId?: string }): Promise<QueryResult>;
  authorizeConnector(connectorId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  uploadAsset(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  importPackage(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  rebuildIndex(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  prepareSemanticAsset(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  decideSemanticAsset(assetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  createSemanticDimension(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  decideSemanticDimension(jobId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  processSemanticDimension(jobId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  createSqlGuardrail(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  decideSqlGuardrail(guardrailId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  bindDatabaseCollection(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  observeCollectionFreshness(body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  compileWiki(assetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  processCapture(assetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  projectGbrain(assetId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
  syncSource(connectorId: string, body?: Record<string, unknown>, options?: RequestOptions): Promise<QueryResult>;
}

export function parseQueryResult(payload: unknown): QueryResult;
export function parseMcpResourceList(payload: unknown): McpResourceList;
export function parseMcpResourceRead(payload: unknown): McpResourceRead;
export function createPlatformClient(options?: PlatformClientOptions): PlatformClient;
