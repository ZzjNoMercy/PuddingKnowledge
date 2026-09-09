import { createPlatformClient } from "./contracts.mjs";
import { formatDisplayError, safeDisplayText, safeDisplayValue } from "./display-boundary.mjs";
import { assertLocalShadowBaseUrl, localShadowApiUrlFromPageUrl } from "./local-boundary.mjs";

const byId = (id) => document.getElementById(id);
const baseUrl = byId("base-url");
const spaceId = byId("space-id");
const query = byId("query");
const status = byId("status");
const discovery = byId("discovery");
const assetPreview = byId("asset-preview");
const result = byId("result");
const discoverButton = byId("discover");
const askButton = byId("ask");
const observeButton = byId("observe-freshness");
const adminStatus = byId("admin-status");
const queryResultId = byId("query-result-id");
const readQueryResultButton = byId("read-query-result");
const queryResultPreview = byId("query-result-preview");
const databaseDatasetId = byId("database-dataset-id");
const databaseSemanticAssets = byId("database-semantic-assets");
const databaseQuestion = byId("database-question");
const generateDatabasePlanButton = byId("generate-database-plan");
const readDatabaseSchemaButton = byId("read-database-schema");
const databaseSchemaPreview = byId("database-schema-preview");
const databasePlanPreview = byId("database-plan-preview");
const databaseResultPreview = byId("database-result-preview");
const mcpResources = byId("mcp-resources");
const mcpResourcePreview = byId("mcp-resource-preview");
const semanticAssets = byId("semantic-assets");
const connectors = byId("connectors");
const connectorSourceItems = byId("connector-source-items");
const connectorAuthorizations = byId("connector-authorizations");
const notifications = byId("notifications");
const assetBindingReviews = byId("asset-binding-reviews");
const refreshAuthorizationsButton = byId("refresh-authorizations");
const authorizeConnectorButton = byId("authorize-connector");
const uploadAssetButton = byId("upload-asset");
const uploadPreview = byId("upload-preview");
const importPackageButton = byId("import-package");
const packagePreview = byId("package-preview");
const rebuildIndexButton = byId("rebuild-index");
const indexPreview = byId("index-preview");
const jobId = byId("job-id");
const readJobButton = byId("read-job");
const jobPreview = byId("job-preview");

try {
  baseUrl.value = localShadowApiUrlFromPageUrl(globalThis.location.href, { fallback: baseUrl.value });
} catch {
  baseUrl.value = "";
}

let databasePlan = null;

function setStatus(message, isError = false) {
  status.textContent = message;
  status.classList.toggle("error", isError);
}

function clear(element) {
  while (element.firstChild) element.removeChild(element.firstChild);
}

function addText(parent, tag, text, className = "") {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  parent.appendChild(node);
  return node;
}

function renderDiscovery(response, onReadAsset) {
  clear(discovery);
  const data = response.data || {};
  const spaces = Array.isArray(data.spaces) ? data.spaces : [];
  const collections = Array.isArray(data.collections) ? data.collections : [];
  const assets = Array.isArray(data.assets) ? data.assets : [];
  const section = document.createElement("div");
  addText(section, "h3", "可用知识边界");
  const chips = document.createElement("div");
  chips.className = "chips";
  for (const item of [...spaces, ...collections]) {
    if (!item || typeof item !== "object") continue;
    const identity = item.id || item.name;
    if (typeof identity === "string") addText(chips, "span", identity, "chip");
  }
  section.appendChild(chips);
  addText(section, "p", `${spaces.length} Spaces · ${collections.length} Collections · ${assets.length} Assets`);
  if (assets.length) {
    const assetList = document.createElement("div");
    assetList.className = "asset-list";
    for (const item of assets) {
      if (!item || typeof item !== "object") continue;
      const row = document.createElement("div");
      row.className = "asset-row";
      addText(row, "strong", String(item.title || item.id || "Asset"));
      addText(row, "code", String(item.id || ""));
      addText(row, "span", String(item.kind || ""));
      if (typeof onReadAsset === "function" && typeof item.id === "string") {
        const readButton = document.createElement("button");
        readButton.type = "button";
        readButton.className = "asset-read";
        readButton.textContent = "读取片段";
        readButton.addEventListener("click", () => void onReadAsset(item, readButton));
        row.appendChild(readButton);
      }
      assetList.appendChild(row);
    }
    section.appendChild(assetList);
  }
  discovery.appendChild(section);
}

function renderAssetRead(response) {
  clear(assetPreview);
  const section = document.createElement("section");
  addText(section, "h3", "Asset 片段");
  if (response.status === "error") {
    addText(section, "p", formatDisplayError(response, "读取失败"), "error");
    assetPreview.appendChild(section);
    return;
  }
  const data = response.data || {};
  addText(section, "p", `${String(data.mime_type || "application/octet-stream")} · bytes ${String(data.start ?? 0)}–${String(data.end ?? 0)}`);
  if (data.content_base64 && typeof data.content_base64 === "string") {
    try {
      const bytes = Uint8Array.from(atob(data.content_base64), (character) => character.charCodeAt(0));
      const text = new TextDecoder().decode(bytes);
      const pre = document.createElement("pre");
      pre.textContent = safeDisplayText(text);
      section.appendChild(pre);
    } catch {
      addText(section, "p", "片段不是可预览文本");
    }
  }
  assetPreview.appendChild(section);
}

function renderQueryResult(response) {
  clear(queryResultPreview);
  const section = document.createElement("div");
  if (response.status === "error") {
    addText(section, "p", formatDisplayError(response, "读取失败"), "error");
    queryResultPreview.appendChild(section);
    return;
  }
  const item = response.data?.query_result;
  if (!item || typeof item !== "object") {
    addText(section, "p", "QueryResult 响应格式无效", "error");
    queryResultPreview.appendChild(section);
    return;
  }
  addText(section, "p", `${String(item.status || "unknown")} · ${String(item.row_count ?? 0)} 行 · ${String(item.artifact_format || "")}`);
  addText(section, "code", String(item.id || ""));
  addText(section, "p", `列：${Array.isArray(item.columns) ? item.columns.map(String).join("、") : ""}`);
  if (item.artifact_uri) addText(section, "code", String(item.artifact_uri));
  if (item.artifact_reference_digest) addText(section, "code", String(item.artifact_reference_digest));
  queryResultPreview.appendChild(section);
}

function renderDatabasePlan(response) {
  clear(databasePlanPreview);
  clear(databaseResultPreview);
  databasePlan = null;
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    addText(section, "p", formatDisplayError(response, "QueryPlan 生成失败"), "error");
    databasePlanPreview.appendChild(section);
    return;
  }
  const plan = response.data?.query_plan;
  const sqlHash = response.data?.sql_hash;
  if (!plan || typeof plan !== "object" || typeof plan.query_plan_id !== "string" || typeof sqlHash !== "string") {
    addText(section, "p", "QueryPlan 响应格式无效", "error");
    databasePlanPreview.appendChild(section);
    return;
  }
  databasePlan = {
    queryPlanId: plan.query_plan_id,
    sqlHash,
    spaceId: spaceId.value.trim(),
  };
  addText(section, "p", "QueryPlan 已生成；确认后才会执行只读查询。", "");
  addText(section, "code", plan.query_plan_id);
  addText(section, "p", `Collection：${String(plan.dataset_id || "")} · Dialect：${String(plan.dialect || "")} · SQL digest：${sqlHash}`);
  const executeButton = document.createElement("button");
  executeButton.type = "button";
  executeButton.textContent = "执行只读 QueryPlan";
  executeButton.addEventListener("click", () => void executeDatabasePlan(executeButton));
  section.appendChild(executeButton);
  databasePlanPreview.appendChild(section);
}

function renderDatabaseSchema(response) {
  clear(databaseSchemaPreview);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    addText(section, "p", formatDisplayError(response, "Schema 读取失败"), "error");
    databaseSchemaPreview.appendChild(section);
    return;
  }
  const tables = Array.isArray(response.data?.tables) ? response.data.tables : [];
  if (!tables.length) {
    addText(section, "p", "当前 binding 没有可展示的表", "error");
    databaseSchemaPreview.appendChild(section);
    return;
  }
  addText(section, "p", `${tables.length} 张已绑定表 · source revision：${String(response.data?.source_revision || "")}`);
  for (const table of tables.slice(0, 100)) {
    if (!table || typeof table !== "object") continue;
    const row = document.createElement("div");
    row.className = "evidence";
    addText(row, "strong", String(table.table_name || "table"));
    addText(row, "p", `列：${Array.isArray(table.columns) ? table.columns.slice(0, 200).map(String).join("、") : ""}`);
    addText(row, "code", String(table.schema_revision || ""));
    section.appendChild(row);
  }
  databaseSchemaPreview.appendChild(section);
}

function renderDatabaseResult(response) {
  clear(databaseResultPreview);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    addText(section, "p", formatDisplayError(response, "数据库查询失败"), "error");
    databaseResultPreview.appendChild(section);
    return;
  }
  const data = response.data;
  if (!data || typeof data !== "object" || !Array.isArray(data.columns) || !Array.isArray(data.rows)) {
    addText(section, "p", "数据库结果格式无效", "error");
    databaseResultPreview.appendChild(section);
    return;
  }
  addText(section, "p", `只读查询完成 · ${String(data.row_count ?? data.rows.length)} 行 · 列：${data.columns.map(String).join("、")}`);
  if (typeof data.result_id === "string") addText(section, "code", `QueryResult：${data.result_id}`);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(safeDisplayValue(data.rows), null, 2);
  section.appendChild(pre);
  databaseResultPreview.appendChild(section);
}

function renderMcpResourceRead(response) {
  clear(mcpResourcePreview);
  const section = document.createElement("div");
  const content = Array.isArray(response.contents) ? response.contents[0] : null;
  if (!content) {
    addText(section, "p", "Resource 没有可显示内容");
  } else if (typeof content.text === "string") {
    const pre = document.createElement("pre");
    pre.textContent = safeDisplayText(content.text);
    section.appendChild(pre);
  } else {
    addText(section, "p", `${String(content.mimeType || "binary")} · binary Resource 已读取`);
  }
  mcpResourcePreview.appendChild(section);
}

function renderMcpResources(response, onReadResource) {
  clear(mcpResources);
  const section = document.createElement("div");
  const resources = Array.isArray(response.resources) ? response.resources : [];
  const templates = Array.isArray(response.resourceTemplates) ? response.resourceTemplates : [];
  addText(section, "p", `${resources.length} 个 Resource · ${templates.length} 个 Resource Template`);
  for (const item of resources) {
    if (!item || typeof item.uri !== "string") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.name || item.uri));
    addText(row, "code", item.uri);
    const readButton = document.createElement("button");
    readButton.type = "button";
    readButton.className = "asset-read";
    readButton.textContent = "读取 Resource";
    readButton.addEventListener("click", () => void onReadResource(item.uri, readButton));
    row.appendChild(readButton);
    section.appendChild(row);
  }
  if (templates.length) {
    addText(section, "p", "Templates：");
    for (const item of templates) {
      if (item && typeof item.uriTemplate === "string") addText(section, "code", item.uriTemplate);
    }
  }
  mcpResources.appendChild(section);
}

function renderSemanticAssets(response) {
  clear(semanticAssets);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "Semantic Asset registry 未挂载"), "error");
    semanticAssets.appendChild(section);
    return;
  }
  const assets = Array.isArray(response.data?.assets) ? response.data.assets : [];
  addText(section, "p", `${assets.length} 个 Semantic Asset`);
  if (!assets.length) {
    addText(section, "p", "当前 Space 没有可发现的 Semantic Asset。");
    semanticAssets.appendChild(section);
    return;
  }
  const list = document.createElement("div");
  list.className = "asset-list";
  for (const item of assets) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.name || item.id || "Semantic Asset"));
    addText(row, "code", String(item.id || ""));
    addText(row, "span", `${String(item.type || "")} · ${String(item.status || "unknown")}`);
    list.appendChild(row);
  }
  section.appendChild(list);
  semanticAssets.appendChild(section);
}

function renderSourceItems(response) {
  clear(connectorSourceItems);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "Source Item 读取失败"), "error");
    connectorSourceItems.appendChild(section);
    return;
  }
  const items = Array.isArray(response.data?.source_items) ? response.data.source_items : [];
  addText(section, "p", `${items.length} 个 Source Item`);
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.title || item.id || "Source Item"));
    addText(row, "code", String(item.id || ""));
    addText(row, "span", `${String(item.external_type || "")} · ${String(item.status || "unknown")}`);
    addText(row, "span", item.asset_id ? `Asset ${String(item.asset_id)}` : "未绑定 Asset");
    section.appendChild(row);
  }
  connectorSourceItems.appendChild(section);
}

function renderConnectors(response, onReadSourceItems) {
  clear(connectors);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "Connector Catalog 未挂载"), "error");
    connectors.appendChild(section);
    return;
  }
  const items = Array.isArray(response.data?.connectors) ? response.data.connectors : [];
  addText(section, "p", `${items.length} 个 Connector`);
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.name || item.id || "Connector"));
    addText(row, "code", String(item.id || ""));
    addText(row, "span", `${String(item.connector_key || "")} · ${String(item.status || "unknown")}`);
    if (typeof onReadSourceItems === "function" && typeof item.id === "string") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "asset-read";
      button.textContent = "查看 Source Items";
      button.addEventListener("click", () => void onReadSourceItems(item.id, button));
      row.appendChild(button);
    }
    section.appendChild(row);
  }
  connectors.appendChild(section);
}

function renderConnectorAuthorizations(response) {
  clear(connectorAuthorizations);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "授权状态未挂载"), "error");
    connectorAuthorizations.appendChild(section);
    return;
  }
  const items = Array.isArray(response.data?.authorizations)
    ? response.data.authorizations
    : (response.data?.authorization ? [response.data.authorization] : []);
  addText(section, "p", `${items.length} 个 Connector 授权状态`);
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.name || item.connector_id || "Connector"));
    addText(row, "code", String(item.connector_id || ""));
    addText(row, "span", `${String(item.auth_type || "unknown")} · ${String(item.grant_status || "no_grant")}`);
    addText(row, "span", item.authorization_required ? "需要宿主授权" : "已有有效授权");
    section.appendChild(row);
  }
  connectorAuthorizations.appendChild(section);
}

function renderNotifications(response) {
  clear(notifications);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "Platform 通知未挂载"), "error");
    notifications.appendChild(section);
    return;
  }
  const items = Array.isArray(response.data?.notifications) ? response.data.notifications : [];
  addText(section, "p", `${items.length} 条 Platform 通知事件（已读状态由消费方维护）`);
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    addText(row, "strong", String(item.title || item.event_id || "Notification"));
    addText(row, "span", String(item.category || ""));
    addText(row, "p", safeDisplayText(item.body || ""));
    addText(row, "code", String(item.resource_uri || ""));
    section.appendChild(row);
  }
  notifications.appendChild(section);
}

function renderAssetBindingReviews(response) {
  clear(assetBindingReviews);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, "Asset binding review queue 未挂载"), "error");
    assetBindingReviews.appendChild(section);
    return;
  }
  const queue = response.data?.asset_binding_review_queue;
  if (!queue || typeof queue !== "object" || !Array.isArray(queue.items)) {
    addText(section, "p", "Asset binding review queue 响应格式无效", "error");
    assetBindingReviews.appendChild(section);
    return;
  }
  const ambiguousCount = Number(queue.summary?.ambiguous_candidate_item_count ?? 0);
  addText(section, "p", `${String(queue.summary?.confirmed_candidate_item_count ?? queue.items.length)} 条有候选的待复核项 · ${String(ambiguousCount)} 条需选择副本 · ${String(queue.summary?.review_item_count ?? 0)} 条总复核项`);
  addText(section, "p", "这里只读展示 review ID、摘要和 Catalog Asset；不会批准、写入 Catalog 或显示本地路径。", "");
  const list = document.createElement("div");
  list.className = "asset-list";
  for (const item of queue.items.slice(0, 100)) {
    if (!item || typeof item !== "object") continue;
    const row = document.createElement("div");
    row.className = "asset-row";
    const metadata = Array.isArray(item.catalog_assets) ? item.catalog_assets : [];
    addText(row, "strong", metadata[0]?.title ? String(metadata[0].title) : "待复核 Asset");
    addText(row, "code", String(item.review_id || ""));
    const candidateCount = Number(item.candidate_count ?? 1);
    const candidateLabel = item.candidate_selection_required === true
      ? `${String(candidateCount)} 个同摘要本地候选 · 需人工选择`
      : "唯一摘要候选";
    addText(row, "span", `${String(item.bytes ?? 0)} bytes · ${candidateLabel} · ${String(item.catalog_asset_match_count ?? 0)} 个 Catalog Asset`);
    const copyButton = document.createElement("button");
    copyButton.type = "button";
    copyButton.className = "asset-read";
    copyButton.textContent = "复制 review ID";
    copyButton.addEventListener("click", async () => {
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") {
        copyButton.textContent = "浏览器不支持复制";
        return;
      }
      try {
        await navigator.clipboard.writeText(String(item.review_id || ""));
        copyButton.textContent = "已复制";
      } catch {
        copyButton.textContent = "复制失败";
      }
    });
    row.appendChild(copyButton);
    list.appendChild(row);
  }
  section.appendChild(list);
  assetBindingReviews.appendChild(section);
}

function renderAdminStaging(response, target, label) {
  clear(target);
  const section = document.createElement("div");
  if (!response || response.status === "error") {
    const error = response?.error;
    addText(section, "p", formatDisplayError({ error }, `${label}失败`), "error");
  } else {
    const payload = response.data?.upload || response.data?.package_import || response.data?.index;
    addText(section, "p", response.answer ? safeDisplayText(response.answer) : `${label}完成`);
    if (payload && typeof payload === "object") {
      addText(section, "code", String(payload.asset_id || payload.package_id || payload.package_ref || ""));
      addText(section, "p", `${String(payload.status || "staged")} · ${String(payload.content_digest || payload.package_revision || "")}`);
    }
  }
  target.appendChild(section);
}

function renderJob(response) {
  clear(jobPreview);
  const section = document.createElement("div");
  if (response.status === "error") {
    addText(section, "p", formatDisplayError(response, "读取失败"), "error");
    jobPreview.appendChild(section);
    return;
  }
  const item = response.data?.job;
  if (!item || typeof item !== "object") {
    addText(section, "p", "Job 响应格式无效", "error");
    jobPreview.appendChild(section);
    return;
  }
  addText(section, "p", `${String(item.status || "unknown")} · ${String(item.current_step || "")} · ${String(item.progress ?? 0)}%`);
  addText(section, "code", String(item.id || ""));
  addText(section, "p", `类型：${String(item.kind || "")} · 尝试：${String(item.attempt ?? 0)} · 重试：${String(item.retry_count ?? 0)}`);
  if (item.asset_id) addText(section, "p", `Asset：${String(item.asset_id)}`);
  if (item.dimension_id) addText(section, "p", `Dimension：${String(item.dimension_id)}`);
  if (item.connector_id) addText(section, "p", `Connector：${String(item.connector_id)}`);
  jobPreview.appendChild(section);
}

function renderResult(response) {
  clear(result);
  const section = document.createElement("div");
  if (response.status === "error") {
    addText(section, "p", formatDisplayError(response, "查询失败"), "error");
    result.appendChild(section);
    return;
  }
  if (response.answer) addText(section, "p", safeDisplayText(response.answer));
  const evidence = Array.isArray(response.evidence) ? response.evidence : [];
  if (evidence.length) {
    const evidenceSection = document.createElement("div");
    evidenceSection.className = "evidence";
    addText(evidenceSection, "h3", "Evidence");
    for (const item of evidence) {
      if (!item || typeof item !== "object") continue;
      const row = document.createElement("div");
      addText(row, "strong", String(item.asset_id || "asset"));
      addText(row, "p", item.quote ? safeDisplayText(item.quote) : "");
      addText(row, "code", String(item.resource_uri || ""));
      evidenceSection.appendChild(row);
    }
    section.appendChild(evidenceSection);
  }
  if (response.data && Object.keys(response.data).length) {
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(safeDisplayValue(response.data), null, 2);
    section.appendChild(pre);
  }
  result.appendChild(section);
}

function client() {
  return createPlatformClient({ baseUrl: assertLocalShadowBaseUrl(baseUrl.value) });
}

async function discover() {
  discoverButton.disabled = true;
  setStatus("正在读取 Platform Catalog…");
  try {
    const api = client();
    const selectedSpaceId = spaceId.value.trim();
    const [spaces, collections, assets, resources, semantic, connectorList, authorizationList, notificationList, bindingReviews] = await Promise.all([
      api.listSpaces(),
      api.listCollections({ spaceId: selectedSpaceId || undefined }),
      api.listAssets({ spaceId: selectedSpaceId || undefined }),
      api.listMcpResources(),
      selectedSpaceId
        ? api.listSemanticAssets({ spaceId: selectedSpaceId }).catch((error) => ({
          status: "error",
          error: { code: "capability_unavailable", message: "Semantic Asset registry 未挂载" },
        }))
        : Promise.resolve({ status: "error", error: { code: "space_required", message: "请输入 Space ID 后再发现 Semantic Assets" } }),
      selectedSpaceId
        ? api.listConnectors({ spaceId: selectedSpaceId }).catch((error) => ({
          status: "error",
          error: { code: "capability_unavailable", message: "Connector Catalog 未挂载" },
        }))
        : Promise.resolve({ status: "error", error: { code: "space_required", message: "请输入 Space ID 后再发现 Connectors" } }),
      selectedSpaceId
        ? api.listConnectorAuthorizations({ spaceId: selectedSpaceId }).catch((error) => ({
          status: "error",
          error: { code: "capability_unavailable", message: "授权状态未挂载" },
        }))
        : Promise.resolve({ status: "error", error: { code: "space_required", message: "请输入 Space ID 后再读取授权状态" } }),
      selectedSpaceId
        ? api.listNotifications({ spaceId: selectedSpaceId, limit: 20 }).catch((error) => ({
          status: "error",
          error: { code: "capability_unavailable", message: "Platform 通知未挂载" },
        }))
        : Promise.resolve({ status: "error", error: { code: "space_required", message: "请输入 Space ID 后再读取通知" } }),
      selectedSpaceId
        ? api.listAssetBindingReviews({ spaceId: selectedSpaceId }).catch((error) => ({
          status: "error",
          error: { code: "capability_unavailable", message: "Asset binding review queue 未挂载" },
        }))
        : Promise.resolve({ status: "error", error: { code: "space_required", message: "请输入 Space ID 后再读取 Asset binding review queue" } }),
    ]);
    if (spaces.status === "error") throw new Error("Spaces 请求失败");
    if (collections.status === "error") throw new Error("Collections 请求失败");
    if (assets.status === "error") throw new Error("Assets 请求失败");
    renderMcpResources(resources, async (uri, button) => {
      button.disabled = true;
      try {
        renderMcpResourceRead(await api.readMcpResource(uri, { start: 0, end: 512 }));
      } catch (error) {
        clear(mcpResourcePreview);
        addText(mcpResourcePreview, "p", "Resource 读取失败", "error");
      } finally {
        button.disabled = false;
      }
    });
    renderSemanticAssets(semantic);
    renderConnectors(connectorList, async (connectorId, button) => {
      button.disabled = true;
      try {
        renderSourceItems(await api.listSourceItems({ spaceId: selectedSpaceId, connectorId }));
      } catch (error) {
        renderSourceItems({ status: "error", error: { code: "read_failed", message: "Source Item 读取失败" } });
      } finally {
        button.disabled = false;
      }
    });
    renderConnectorAuthorizations(authorizationList);
    renderNotifications(notificationList);
    renderAssetBindingReviews(bindingReviews);
    renderDiscovery({ data: {
      spaces: Array.isArray(spaces.data?.spaces) ? spaces.data.spaces : [],
      collections: Array.isArray(collections.data?.collections) ? collections.data.collections : [],
      assets: Array.isArray(assets.data?.assets) ? assets.data.assets : [],
    } }, async (asset, button) => {
      button.disabled = true;
      try {
        const response = await api.readAsset(String(asset.id), {
          resource_uri: typeof asset.source_uri === "string" ? asset.source_uri : undefined,
          start: 0,
          end: 512,
        });
        renderAssetRead(response);
      } catch (error) {
        renderAssetRead({ status: "error", error: { code: "read_failed", message: "读取失败" } });
      } finally {
        button.disabled = false;
      }
    });
    setStatus("Catalog 读取完成");
  } catch (error) {
    setStatus("Catalog 读取失败", true);
  } finally {
    discoverButton.disabled = false;
  }
}

async function ask() {
  if (!query.value.trim()) {
    setStatus("请输入查询内容", true);
    return;
  }
  askButton.disabled = true;
  setStatus("正在查询…");
  try {
    const response = await client().knowledgeQuery({ query: query.value.trim(), space_id: spaceId.value.trim() || undefined });
    renderResult(response);
    setStatus(response.status === "ok" ? "查询完成" : "查询返回错误", response.status !== "ok");
  } catch (error) {
    setStatus("查询失败", true);
  } finally {
    askButton.disabled = false;
  }
}

async function observeFreshness() {
  const collectionId = byId("collection-id").value.trim();
  const collectionVersion = byId("collection-version").value.trim();
  const currentSpaceId = byId("freshness-space-id").value.trim();
  const capability = byId("freshness-capability").value.trim();
  const state = byId("freshness-state").value;
  const mode = byId("freshness-mode").value.trim();
  if (!collectionId || !collectionVersion || !currentSpaceId || !capability) {
    adminStatus.textContent = "Collection、Version、Space 和 Capability 不能为空";
    adminStatus.classList.add("error");
    return;
  }
  observeButton.disabled = true;
  adminStatus.classList.remove("error");
  adminStatus.textContent = "正在提交 freshness 观测…";
  try {
    const response = await client().observeCollectionFreshness({
      collection_id: collectionId,
      collection_version: collectionVersion,
      space_id: currentSpaceId,
      capability,
      state,
      observed_at: new Date().toISOString(),
      ...(mode ? { mode } : {}),
    });
    adminStatus.textContent = response.status === "ok"
      ? "freshness 观测已提交"
      : formatDisplayError(response, "提交失败");
    adminStatus.classList.toggle("error", response.status !== "ok");
  } catch (error) {
    adminStatus.textContent = "freshness 观测提交失败";
    adminStatus.classList.add("error");
  } finally {
    observeButton.disabled = false;
  }
}

async function readQueryResult() {
  const id = queryResultId.value.trim();
  if (!id) {
    queryResultPreview.textContent = "请输入 QueryResult ID";
    queryResultPreview.classList.add("error");
    return;
  }
  readQueryResultButton.disabled = true;
  queryResultPreview.classList.remove("error");
  queryResultPreview.textContent = "正在读取 QueryResult 元数据…";
  try {
    renderQueryResult(await client().getQueryResult(id));
  } catch (error) {
    queryResultPreview.textContent = "QueryResult 读取失败";
    queryResultPreview.classList.add("error");
  } finally {
    readQueryResultButton.disabled = false;
  }
}

async function generateDatabasePlan() {
  const selectedSpaceId = spaceId.value.trim();
  const datasetId = databaseDatasetId.value.trim();
  const question = databaseQuestion.value.trim();
  const semanticAssetIds = databaseSemanticAssets.value.split(",").map((item) => item.trim()).filter(Boolean);
  if (!selectedSpaceId || !datasetId || !question) {
    renderDatabasePlan({ status: "error", error: { code: "invalid_request", message: "Space、Collection 和 Analytics Question 不能为空" } });
    return;
  }
  generateDatabasePlanButton.disabled = true;
  try {
    renderDatabasePlan(await client().databaseNl2Sql({
      space_id: selectedSpaceId,
      dataset_id: datasetId,
      question,
      ...(semanticAssetIds.length ? { semantic_asset_ids: semanticAssetIds } : {}),
    }));
  } catch (error) {
    renderDatabasePlan({ status: "error", error: { code: "request_failed", message: "QueryPlan 生成失败" } });
  } finally {
    generateDatabasePlanButton.disabled = false;
  }
}

async function readDatabaseSchema() {
  const selectedSpaceId = spaceId.value.trim();
  const datasetId = databaseDatasetId.value.trim();
  if (!selectedSpaceId || !datasetId) {
    renderDatabaseSchema({ status: "error", error: { code: "invalid_request", message: "Space 和 Collection 不能为空" } });
    return;
  }
  readDatabaseSchemaButton.disabled = true;
  try {
    renderDatabaseSchema(await client().listDatabaseSchema({ spaceId: selectedSpaceId, datasetId }));
  } catch (error) {
    renderDatabaseSchema({ status: "error", error: { code: "request_failed", message: "Schema 读取失败" } });
  } finally {
    readDatabaseSchemaButton.disabled = false;
  }
}

async function executeDatabasePlan(button) {
  if (!databasePlan) {
    renderDatabaseResult({ status: "error", error: { code: "invalid_request", message: "请先生成 QueryPlan" } });
    return;
  }
  button.disabled = true;
  try {
    renderDatabaseResult(await client().executeDatabasePlan(databasePlan.queryPlanId, {
      space_id: databasePlan.spaceId,
      page_size: 100,
      expected_sql_hash: databasePlan.sqlHash,
    }));
  } catch (error) {
    renderDatabaseResult({ status: "error", error: { code: "request_failed", message: "数据库查询失败" } });
  } finally {
    button.disabled = false;
  }
}

async function readJob() {
  const id = jobId.value.trim();
  if (!id) {
    jobPreview.textContent = "请输入 Job ID";
    jobPreview.classList.add("error");
    return;
  }
  readJobButton.disabled = true;
  jobPreview.classList.remove("error");
  jobPreview.textContent = "正在读取 Job 状态…";
  try {
    renderJob(await client().getJob(id));
  } catch (error) {
    jobPreview.textContent = "Job 读取失败";
    jobPreview.classList.add("error");
  } finally {
    readJobButton.disabled = false;
  }
}

async function uploadAsset() {
  const fields = {
    asset_id: byId("upload-asset-id").value.trim(),
    space_id: spaceId.value.trim(),
    title: byId("upload-title").value.trim(),
    filename: byId("upload-filename").value.trim(),
    mime_type: byId("upload-mime-type").value.trim(),
    binding_id: byId("upload-binding-id").value.trim(),
    content_digest: byId("upload-digest").value.trim(),
    idempotency_key: byId("upload-idempotency").value.trim(),
  };
  if (Object.values(fields).some((value) => !value)) {
    renderAdminStaging({ status: "error", error: { code: "invalid_request", message: "Upload 字段不能为空" } }, uploadPreview, "Upload");
    return;
  }
  uploadAssetButton.disabled = true;
  try {
    renderAdminStaging(await client().uploadAsset(fields), uploadPreview, "Upload");
  } catch (error) {
    renderAdminStaging({ status: "error", error: { code: "request_failed", message: "Upload 失败" } }, uploadPreview, "Upload");
  } finally {
    uploadAssetButton.disabled = false;
  }
}

async function importPackage() {
  const packageRef = byId("package-ref").value.trim();
  const idempotencyKey = byId("package-idempotency").value.trim();
  if (!packageRef || !idempotencyKey) {
    renderAdminStaging({ status: "error", error: { code: "invalid_request", message: "Package binding 和幂等键不能为空" } }, packagePreview, "Package Import");
    return;
  }
  importPackageButton.disabled = true;
  try {
    renderAdminStaging(await client().importPackage({ package_ref: packageRef, idempotency_key: idempotencyKey }), packagePreview, "Package Import");
  } catch (error) {
    renderAdminStaging({ status: "error", error: { code: "request_failed", message: "Package Import 失败" } }, packagePreview, "Package Import");
  } finally {
    importPackageButton.disabled = false;
  }
}

async function rebuildIndex() {
  const fields = {
    space_id: spaceId.value.trim(),
    collection_id: byId("index-collection-id").value.trim(),
    collection_version: byId("index-collection-version").value.trim(),
    capability: byId("index-capability").value.trim(),
    provider_id: byId("index-provider-id").value.trim(),
    idempotency_key: byId("index-idempotency").value.trim(),
  };
  if (Object.values(fields).some((value) => !value)) {
    renderAdminStaging({ status: "error", error: { code: "invalid_request", message: "Index Candidate 字段不能为空" } }, indexPreview, "Index Rebuild");
    return;
  }
  rebuildIndexButton.disabled = true;
  try {
    renderAdminStaging(await client().rebuildIndex(fields), indexPreview, "Index Rebuild");
  } catch (error) {
    renderAdminStaging({ status: "error", error: { code: "request_failed", message: "Index Rebuild 失败" } }, indexPreview, "Index Rebuild");
  } finally {
    rebuildIndexButton.disabled = false;
  }
}

async function refreshAuthorizations() {
  const selectedSpaceId = spaceId.value.trim();
  if (!selectedSpaceId) {
    renderConnectorAuthorizations({ status: "error", error: { code: "space_required", message: "请输入 Space ID" } });
    return;
  }
  refreshAuthorizationsButton.disabled = true;
  try {
    renderConnectorAuthorizations(await client().listConnectorAuthorizations({ spaceId: selectedSpaceId }));
  } catch (error) {
    renderConnectorAuthorizations({ status: "error", error: { code: "request_failed", message: "授权状态读取失败" } });
  } finally {
    refreshAuthorizationsButton.disabled = false;
  }
}

async function authorizeConnector() {
  const connectorId = byId("authorization-connector-id").value.trim();
  const mode = byId("authorization-mode").value;
  const idempotencyKey = byId("authorization-idempotency").value.trim();
  const selectedSpaceId = spaceId.value.trim();
  if (!selectedSpaceId || !connectorId || !idempotencyKey) {
    renderConnectorAuthorizations({ status: "error", error: { code: "invalid_request", message: "Space、Connector 和幂等键不能为空" } });
    return;
  }
  authorizeConnectorButton.disabled = true;
  try {
    renderConnectorAuthorizations(await client().authorizeConnector(connectorId, {
      space_id: selectedSpaceId,
      mode,
      idempotency_key: idempotencyKey,
    }));
  } catch (error) {
    renderConnectorAuthorizations({ status: "error", error: { code: "request_failed", message: "授权意图创建失败" } });
  } finally {
    authorizeConnectorButton.disabled = false;
  }
}

discoverButton.addEventListener("click", discover);
askButton.addEventListener("click", ask);
observeButton.addEventListener("click", observeFreshness);
readQueryResultButton.addEventListener("click", readQueryResult);
generateDatabasePlanButton.addEventListener("click", generateDatabasePlan);
readDatabaseSchemaButton.addEventListener("click", readDatabaseSchema);
readJobButton.addEventListener("click", readJob);
uploadAssetButton.addEventListener("click", uploadAsset);
importPackageButton.addEventListener("click", importPackage);
rebuildIndexButton.addEventListener("click", rebuildIndex);
refreshAuthorizationsButton.addEventListener("click", refreshAuthorizations);
authorizeConnectorButton.addEventListener("click", authorizeConnector);
byId("clear").addEventListener("click", () => {
  clear(discovery);
  clear(assetPreview);
  clear(mcpResources);
  clear(mcpResourcePreview);
  clear(semanticAssets);
  clear(connectors);
  clear(connectorSourceItems);
  clear(connectorAuthorizations);
  clear(notifications);
  clear(assetBindingReviews);
  clear(uploadPreview);
  clear(packagePreview);
  clear(indexPreview);
  clear(result);
  clear(databasePlanPreview);
  clear(databaseResultPreview);
  clear(databaseSchemaPreview);
  databasePlan = null;
  setStatus("");
});

export {
  observeFreshness,
  readJob,
  readQueryResult,
  generateDatabasePlan,
  readDatabaseSchema,
  executeDatabasePlan,
  renderAssetRead,
  renderDiscovery,
  renderJob,
  renderMcpResourceRead,
  renderMcpResources,
  renderSemanticAssets,
  renderSourceItems,
  renderConnectors,
  renderQueryResult,
  renderDatabasePlan,
  renderDatabaseSchema,
  renderDatabaseResult,
  renderResult,
  renderAdminStaging,
  uploadAsset,
  importPackage,
  rebuildIndex,
  renderConnectorAuthorizations,
  renderNotifications,
  refreshAuthorizations,
  authorizeConnector,
};
