import { formatDisplayError, safeDisplayText } from "./display-boundary.mjs";

const byId = (id) => document.getElementById(id);
const add = (parent, tag, value, className = "") => {
  const node = document.createElement(tag);
  node.textContent = safeDisplayText(String(value ?? ""));
  if (className) node.className = className;
  parent.appendChild(node);
  return node;
};
const clear = (node) => { while (node?.firstChild) node.removeChild(node.firstChild); };

export function renderBitableError(root, response, fallback = "Bitable 请求失败") {
  clear(root);
  add(root, "p", formatDisplayError(response, fallback), "error");
}

export function renderBitableSources(root, response, onSelect) {
  clear(root);
  if (!response || response.status === "error") return renderBitableError(root, response);
  const sources = Array.isArray(response.data?.sources) ? response.data.sources : [];
  add(root, "p", `${sources.length} 个 Bitable Source（实时行只返回给当前查询，不由 Platform 保存）`);
  for (const source of sources) {
    if (!source || typeof source !== "object" || typeof source.source_id !== "string") continue;
    const row = document.createElement("div"); row.className = "asset-row";
    add(row, "strong", source.source_id); add(row, "span", `已配置 ${(source.tables || []).length} 张表`);
    const button = document.createElement("button"); button.type = "button"; button.className = "asset-read";
    button.textContent = "选择"; button.addEventListener("click", () => onSelect(source.source_id)); row.appendChild(button); root.appendChild(row);
  }
}

export function renderBitablePanel(root, state) {
  clear(root);
  const { sourceId, policy, schema, relations, query } = state;
  if (state.error) { add(root, "p", state.error, "error"); }
  if (!sourceId) { add(root, "p", "请先读取并选择 Bitable Source。 "); return; }
  add(root, "p", `数据源：${sourceId} · 实时行不会保存到 Knowledge。`);
  if (policy) {
    add(root, "h3", "允许读取的表");
    const tables = Array.isArray(policy.tables) ? policy.tables : [];
    if (!tables.length) add(root, "p", "当前范围为空：deny-all，不会读取任何表。保存空范围会保留这个明确拒绝全部表的策略。", "error");
    for (const table of tables) {
      const row = document.createElement("div"); row.className = "asset-row";
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = true; checkbox.dataset.tableId = String(table.table_id || ""); checkbox.dataset.role = "table-selected"; checkbox.setAttribute("aria-label", `允许读取 ${table.table_id}`);
      row.appendChild(checkbox); add(row, "strong", table.table_id); add(row, "span", `视图：${table.view_id || "整张表"}`); root.appendChild(row);
      const view = document.createElement("input"); view.value = String(table.view_id || ""); view.placeholder = "视图 ID（空=整张表）"; view.dataset.role = "table-view"; view.dataset.tableId = String(table.table_id || ""); view.setAttribute("aria-label", `${table.table_id} 视图`); row.appendChild(view);
      const inspect = document.createElement("button"); inspect.type = "button"; inspect.dataset.action = "load-schema"; inspect.dataset.tableId = String(table.table_id || ""); inspect.textContent = "查看 Schema"; row.appendChild(inspect);
    }
    const save = document.createElement("button"); save.type = "button"; save.dataset.action = "save-policy"; save.textContent = "保存读取范围"; save.disabled = state.busy; root.appendChild(save);
    add(root, "p", "取消勾选全部表并保存，将禁止读取所有表。新增表会由服务端验证访问权限。");
    const newTable = document.createElement("input"); newTable.dataset.role = "new-table"; newTable.placeholder = "新增表 ID"; newTable.setAttribute("aria-label", "新增表 ID"); root.appendChild(newTable);
    const newView = document.createElement("input"); newView.dataset.role = "new-view"; newView.placeholder = "视图 ID（空=整张表）"; newView.setAttribute("aria-label", "新增表视图 ID"); root.appendChild(newView);
    const append = document.createElement("button"); append.type = "button"; append.dataset.action = "add-table"; append.textContent = "添加表并保存范围"; append.disabled = state.busy; root.appendChild(append);
  }
  if (schema) {
    add(root, "h3", `表结构：${schema.table_id || ""}`);
    add(root, "p", `版本 ${String(schema.schema_revision || "").slice(0,12)}${schema.sync_required ? " · 需要同步" : " · 已同步"}`);
    for (const field of schema.schema?.fields || []) add(root, "p", `${field.field_name || field.field_id} · type ${field.type}`);
    for (const field of schema.schema?.fields || []) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.checked = state.fieldNames.includes(field.field_name); checkbox.dataset.role = "query-field"; checkbox.value = String(field.field_name || field.field_id);
      label.appendChild(checkbox); add(label, "span", ` ${field.field_name || field.field_id}`); root.appendChild(label);
    }
    const size = document.createElement("input"); size.type = "number"; size.min = "1"; size.max = "100"; size.value = String(state.pageSize || 50); size.setAttribute("aria-label", "每页行数"); size.dataset.role = "page-size"; const sizeLabel = document.createElement("label"); sizeLabel.textContent = "每页行数 "; sizeLabel.appendChild(size); root.appendChild(sizeLabel);
    const queryButton = document.createElement("button"); queryButton.type = "button"; queryButton.dataset.action = "live-query"; queryButton.disabled = schema.sync_required === true || state.busy; queryButton.textContent = schema.sync_required ? "需先同步 Schema" : "读取当前页实时行"; root.appendChild(queryButton);
    if (query?.next_cursor) { const next = document.createElement("button"); next.type = "button"; next.dataset.action = "next-page"; next.textContent = "下一页"; root.appendChild(next); }
    add(root, "p", "实时查询字段可勾选；返回的行数据不会保存。", "evidence");
  }
  if (relations) {
    add(root, "h3", "关系");
    for (const relation of relations.relations || []) {
      const status = { schema_valid: "结构有效", needs_review: "需要复核", stale_endpoint: "表或字段已失效" }[relation.validation_status || relation.status] || "尚未验证";
      add(root, "p", `${relation.name || relation.id}：${status}`);
      for (const warning of relation.warnings || []) add(root, "p", warning);
    }
    const relationEditor = document.createElement("textarea"); relationEditor.dataset.role = "relations-json"; relationEditor.setAttribute("aria-label", "关系声明 JSON"); relationEditor.value = JSON.stringify(state.policy?.relations || [], null, 2); root.appendChild(relationEditor);
    const relationSave = document.createElement("button"); relationSave.type = "button"; relationSave.dataset.action = "save-relations"; relationSave.textContent = "保存关系"; relationSave.disabled = state.busy; root.appendChild(relationSave);
    add(root, "p", "关系 JSON 可编辑后保存；端点和 cardinality 必须显式填写，Schema 不能证明 row value 唯一。", "evidence");
  }
  if (query) {
    add(root, "h3", "实时结果"); add(root, "pre", JSON.stringify(query.records || [], null, 2));
    add(root, "p", query.has_more ? "还有更多行，可继续读取下一页。" : "当前页已是最后一页。");
  }
  if (state.busy) add(root, "p", "正在读取或保存…");
}

function responseData(response) {
  if (!response || response.status !== "ok") throw new Error(formatDisplayError(response, "Bitable 请求失败"));
  if (!response.data || typeof response.data !== "object") throw new Error("Bitable 响应格式无效");
  return response.data;
}

export function createBitableController({ api, onState = () => {} } = {}) {
  if (!api) throw new TypeError("Bitable API is required");
  const empty = () => ({ sourceId: "", policy: null, schema: null, relations: null, query: null, fieldNames: [], pageSize: 50, error: "", busy: false });
  const state = empty();
  let generation = 0;
  const emit = () => onState(state);
  const reset = () => { ++generation; Object.assign(state, empty()); emit(); };
  async function run(work, commit, invalidate = {}) {
    const current = ++generation;
    Object.assign(state, { error: "", busy: true, query: null }, invalidate); emit();
    try {
      const value = await work(() => current === generation);
      if (current !== generation) return null;
      commit(value); state.busy = false; emit(); return value;
    } catch (error) {
      if (current !== generation) return null;
      state.busy = false; state.query = null;
      state.error = safeDisplayText(error?.message || "Bitable 请求失败"); emit(); throw error;
    }
  }
  const selectSource = (sourceId) => {
    return run(async () => {
      const [policy, relations] = await Promise.all([api.getBitablePolicy(sourceId), api.getBitableRelations(sourceId)]);
      return { policy: responseData(policy), relations: responseData(relations) };
    }, (value) => Object.assign(state, value), { sourceId, policy: null, relations: null, schema: null, fieldNames: [] });
  };
  const refresh = () => {
    reset();
    return run(async () => { const response = await api.listBitableSources(); responseData(response); return response; }, () => {});
  };
  const requireSource = () => { if (!state.sourceId) throw new TypeError("请选择数据源"); return state.sourceId; };
  const syncSchema = () => {
    const sourceId = requireSource();
    return run(async (isCurrent) => {
      responseData(await api.syncSource(sourceId, { mode: "full", idempotency_key: `console-bitable-${globalThis.crypto?.randomUUID?.() || Date.now()}` }));
      if (!isCurrent()) return null;
      const [policy, relations] = await Promise.all([api.getBitablePolicy(sourceId), api.getBitableRelations(sourceId)]);
      return { policy: responseData(policy), relations: responseData(relations) };
    }, (value) => Object.assign(state, value), { schema: null, fieldNames: [] });
  };
  const loadSchema = (tableId) => {
    const sourceId = requireSource();
    return run(async () => responseData(await api.getBitableSchema(sourceId, tableId)), (schema) => {
      state.schema = schema; state.fieldNames = (schema.schema?.fields || []).map((field) => field.field_name);
    }, { schema: null, fieldNames: [] });
  };
  const savePolicy = (policy, expectedRevision) => {
    const sourceId = requireSource();
    if (!policy || !Array.isArray(policy.tables) || !Array.isArray(policy.relations)) throw new TypeError("范围和关系格式无效");
    const exact = { tables: policy.tables.map((item) => ({ table_id: item.table_id, view_id: item.view_id || "" })), relations: JSON.parse(JSON.stringify(policy.relations)) };
    return run(async (isCurrent) => {
      const response = await api.updateBitablePolicy(sourceId, exact, expectedRevision);
      const policy = responseData(response);
      if (!isCurrent()) return null;
      const relations = responseData(await api.getBitableRelations(sourceId));
      return { response, policy, relations };
    }, (value) => Object.assign(state, { policy: value.policy, relations: value.relations }), { schema: null, fieldNames: [] });
  };
  const saveRelations = (relations, expectedRevision) => savePolicy({ tables: state.policy?.tables || [], relations }, expectedRevision);
  const setQueryOptions = ({ fieldNames = state.fieldNames, pageSize = state.pageSize }) => {
    ++generation; Object.assign(state, { fieldNames: [...fieldNames], pageSize, query: null, error: "", busy: false }); emit();
  };
  const query = ({ tableId, schemaRevision, fieldNames, pageSize = 50, cursor = "" }) => {
    const sourceId = requireSource();
    if (!state.schema || state.schema.sync_required || state.schema.table_id !== tableId || state.schema.schema_revision !== schemaRevision) throw new TypeError("请先同步并读取当前表结构");
    if (!Array.isArray(fieldNames) || !fieldNames.length || !Number.isInteger(pageSize) || pageSize < 1 || pageSize > 100) throw new TypeError("请选择至少一个字段，每页行数应为 1–100");
    const fields = [...fieldNames].sort();
    const signature = JSON.stringify([sourceId, tableId, schemaRevision, fields, pageSize]);
    if (cursor && (state.query?.signature !== signature || state.query.next_cursor !== cursor)) throw new TypeError("分页条件已变化，请重新查询");
    return run(async () => responseData(await api.queryBitable(sourceId, { table_id: tableId, schema_revision: schemaRevision, field_names: fields, page_size: pageSize, cursor })), (data) => {
      state.query = { ...data, signature, fieldNames: fields, pageSize };
    }, { fieldNames: fields, pageSize });
  };
  const nextPage = () => {
    if (!state.query?.next_cursor) return null;
    return query({ tableId: state.query.table_id, schemaRevision: state.query.schema_revision, fieldNames: state.query.fieldNames, pageSize: state.query.pageSize, cursor: state.query.next_cursor });
  };
  return { state, reset, refresh, selectSource, syncSchema, loadSchema, savePolicy, saveRelations, setQueryOptions, query, nextPage };
}

export function createBitableSurface({ api, root = byId("bitable-surface") } = {}) {
  if (!api || !root) return { refresh: async () => {} };
  const controller = createBitableController({ api, onState: (state) => renderBitablePanel(root, state) });
  const { state } = controller;
  const refresh = async () => { const response = await controller.refresh(); if (response) renderBitableSources(byId("bitable-sources") || root, response, (id) => { void controller.selectSource(id).catch(() => {}); }); return response; };
  root.addEventListener("click", async (event) => {
    const action = event.target?.dataset?.action;
    if (!action || !state.sourceId || !state.policy) return;
    try {
      if (action === "save-policy" || action === "add-table") {
        const tables = [...root.querySelectorAll('[data-role="table-selected"]:checked')].map((checkbox) => ({
          table_id: checkbox.dataset.tableId,
          view_id: root.querySelector(`[data-role="table-view"][data-table-id="${CSS.escape(checkbox.dataset.tableId)}"]`)?.value?.trim() || "",
        }));
        if (action === "add-table") {
          const table_id = root.querySelector('[data-role="new-table"]').value.trim();
          const view_id = root.querySelector('[data-role="new-view"]').value.trim();
          if (!/^[A-Za-z0-9_-]{1,220}$/.test(table_id)) throw new TypeError("请输入有效表 ID");
          tables.push({ table_id, view_id });
        }
        await controller.savePolicy({ tables, relations: state.policy.relations || [] }, state.policy.policy_revision);
      } else if (action === "save-relations") {
        const relations = JSON.parse(root.querySelector('[data-role="relations-json"]')?.value || "[]");
        await controller.saveRelations(relations, state.policy.policy_revision);
      } else if (action === "load-schema") {
        await controller.loadSchema(event.target.dataset.tableId);
      } else if (action === "live-query" && state.schema) {
        const fieldNames = [...root.querySelectorAll('[data-role="query-field"]:checked')].map((field) => field.value);
        await controller.query({ tableId: state.schema.table_id, schemaRevision: state.schema.schema_revision, fieldNames, pageSize: Number(root.querySelector('[data-role="page-size"]')?.value || 50) });
      } else if (action === "next-page") {
        await controller.nextPage();
      }
    } catch (error) { add(root, "p", error?.message || "保存失败", "error"); }
  });
  root.addEventListener("change", (event) => {
    if (["query-field", "page-size"].includes(event.target?.dataset?.role)) {
      const fieldNames = [...root.querySelectorAll('[data-role="query-field"]:checked')].map((field) => field.value);
      const pageSize = Number(root.querySelector('[data-role="page-size"]')?.value || 50);
      controller.setQueryOptions({ fieldNames, pageSize });
    }
  });
  return { ...controller, refresh, state, render: () => renderBitablePanel(root, state) };
}
