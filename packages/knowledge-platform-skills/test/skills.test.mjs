import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(await readFile(path.join(ROOT, "manifest.json"), "utf8"));

test("Platform Skill manifest points only to existing portable skills", async () => {
  assert.equal(manifest.protocol, "REST-or-MCP-public-contract");
  assert.equal(manifest.path_policy, "portable-resource-uris-only");
  assert.ok(manifest.skills.length >= 7);
  for (const skill of manifest.skills) {
    const content = await readFile(path.join(ROOT, skill.path), "utf8");
    assert.ok(content.length > 0, skill.id);
    assert.doesNotMatch(content, /llamaindex_knowledge_query|\/knowledge\//i, skill.id);
    assert.doesNotMatch(content, /password\s*[:=]|token\s*[:=]|file:\/\//i, skill.id);
  }
});

test("Platform Skill bundle contains no Claw-owned worker or session contract", async () => {
  const files = [
    path.join(ROOT, "README.md"),
    path.join(ROOT, "manifest.json"),
    ...manifest.skills.map((skill) => path.join(ROOT, skill.path)),
  ];
  for (const file of files) {
    const content = await readFile(file, "utf8");
    assert.doesNotMatch(content, /PuddingClaw Worker|PuddingClaw Session|analytics_model_id/i, file);
  }
});
