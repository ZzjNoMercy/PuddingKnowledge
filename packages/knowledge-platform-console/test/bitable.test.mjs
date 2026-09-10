import test from "node:test";
import assert from "node:assert/strict";
import { createBitableController } from "../src/bitable.mjs";

const ok = (data) => ({ status: "ok", data });
const policy = { tables: [{ table_id: "t1", view_id: "v1" }], relations: [], policy_revision: "p1" };
const schema = { table_id: "t1", schema_revision: "s1", sync_required: false, schema: { fields: [{ field_name: "Name", type: 1 }] } };

test("late source response cannot overwrite newer source", async () => {
  const waits = [];
  const api = { getBitablePolicy: (id) => new Promise((resolve) => waits.push([id, resolve])), getBitableRelations: async () => ok({ relations: [] }) };
  const controller = createBitableController({ api });
  const first = controller.selectSource("old");
  const second = controller.selectSource("new");
  waits.find(([id]) => id === "new")[1](ok({ ...policy, source_id: "new" }));
  waits.find(([id]) => id === "old")[1](ok({ ...policy, source_id: "old" }));
  await Promise.all([first, second]);
  assert.equal(controller.state.sourceId, "new");
});

test("policy save sends exact tables and relations and permits deny-all", async () => {
  let call;
  const api = { getBitablePolicy: async () => ok(policy), getBitableRelations: async () => ok({ relations: [] }), updateBitablePolicy: async (...args) => { call = args; return ok({ tables: [], relations: [], policy_revision: "p2" }); } };
  const controller = createBitableController({ api });
  await controller.selectSource("source");
  await controller.savePolicy({ tables: [], relations: [] }, "p1");
  assert.deepEqual(call, ["source", { tables: [], relations: [] }, "p1"]);
});

test("query keeps cursor only for same fields/page size and supports next page", async () => {
  const requests = [];
  const api = { getBitablePolicy: async () => ok(policy), getBitableRelations: async () => ok({ relations: [] }), getBitableSchema: async () => ok(schema), queryBitable: async (_id, body) => { requests.push(body); return ok({ table_id: "t1", schema_revision: "s1", records: [{ id: requests.length }], has_more: requests.length === 1, next_cursor: requests.length === 1 ? "c1" : "", row_storage: false }); } };
  const controller = createBitableController({ api });
  await controller.selectSource("source"); await controller.loadSchema("t1");
  await controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"], pageSize: 2 });
  await controller.nextPage();
  assert.equal(requests[1].cursor, "c1");
  await controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"], pageSize: 3 });
  assert.equal(requests[2].cursor, "");
});

test("error response clears previous live rows", async () => {
  let fail = false;
  const api = { getBitablePolicy: async () => ok(policy), getBitableRelations: async () => ok({ relations: [] }), getBitableSchema: async () => ok(schema), queryBitable: async () => fail ? { status: "error", error: { message: "denied" } } : ok({ table_id: "t1", schema_revision: "s1", records: [{ id: 1 }], has_more: false, next_cursor: "" }) };
  const controller = createBitableController({ api });
  await controller.selectSource("source"); await controller.loadSchema("t1"); await controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"] });
  fail = true;
  await assert.rejects(() => controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"] }));
  assert.equal(controller.state.query, null);
});

function readyApi(overrides = {}) {
  return { getBitablePolicy: async () => ok(policy), getBitableRelations: async () => ok({ relations: [] }), getBitableSchema: async () => ok(schema), ...overrides };
}

test("changing fields invalidates both displayed rows and a pending response", async () => {
  let resolve;
  const controller = createBitableController({ api: readyApi({ queryBitable: () => new Promise((r) => { resolve = r; }) }) });
  await controller.selectSource("source"); await controller.loadSchema("t1");
  const pending = controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"] });
  controller.setQueryOptions({ fieldNames: [], pageSize: 2 });
  resolve(ok({ table_id: "t1", records: ["old rows"], next_cursor: "old" }));
  await pending;
  assert.equal(controller.state.query, null);
  assert.deepEqual(controller.state.fieldNames, []);
});

test("overlapping queries only publish the newest response", async () => {
  const waits = [];
  const controller = createBitableController({ api: readyApi({ queryBitable: () => new Promise((r) => waits.push(r)) }) });
  await controller.selectSource("source"); await controller.loadSchema("t1");
  const args = { tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"] };
  const old = controller.query(args); const fresh = controller.query(args);
  waits[1](ok({ records: ["new"], next_cursor: "" })); await fresh;
  waits[0](ok({ records: ["old"], next_cursor: "" })); await old;
  assert.deepEqual(controller.state.query.records, ["new"]);
});

test("schema failures and source refresh immediately discard old selections", async () => {
  let fail = false;
  const controller = createBitableController({ api: readyApi({ listBitableSources: async () => ok({ sources: [] }), getBitableSchema: async () => fail ? { status: "error", error: { message: "unavailable" } } : ok(schema) }) });
  await controller.selectSource("source"); await controller.loadSchema("t1");
  fail = true; await assert.rejects(controller.loadSchema("other"));
  assert.equal(controller.state.schema, null);
  await controller.refresh(); assert.equal(controller.state.sourceId, ""); assert.equal(controller.state.query, null);
});

test("scope can be expanded after deny-all and schema-required query is refused", async () => {
  let sent;
  const controller = createBitableController({ api: readyApi({ updateBitablePolicy: async (_id, value) => { sent = value; return ok({ ...value, policy_revision: "p2" }); }, getBitableSchema: async () => ok({ ...schema, sync_required: true }) }) });
  await controller.selectSource("source"); await controller.savePolicy({ tables: [], relations: [] }, "p1");
  await controller.savePolicy({ tables: [{ table_id: "new_table", view_id: "" }], relations: [] }, "p2");
  assert.equal(sent.tables[0].table_id, "new_table");
  await controller.loadSchema("t1");
  assert.throws(() => controller.query({ tableId: "t1", schemaRevision: "s1", fieldNames: ["Name"] }));
});
