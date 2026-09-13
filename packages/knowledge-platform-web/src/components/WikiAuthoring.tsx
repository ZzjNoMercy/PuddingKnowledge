"use client";

import { useEffect, useRef, useState } from "react";
import { dataOf, errorMessage, platformClient, PortableRecord, records, stringOf } from "../lib/platform";

type Action = "context" | "preview" | "apply" | "generate" | "proposal" | "abandon" | "enqueue" | "queue" | "control_queue";
type Intent = { action: Action; body: PortableRecord };
type Change = { slug: string; markdown: string | null; expected_digest: string | null; replacement?: string | null };
type Patch = { expected_revision: string; changes: Change[]; selected_raw: string[]; index: string; log_entry: string };
const states: Record<string, string> = { queued: "等待执行", ready: "待审查", failed: "生成失败", unsettled: "执行结果未确定", abandoned: "已放弃", cancelled: "已取消", rejected: "请求已失效" };
const api = async (action: Action, body: PortableRecord) => dataOf(await platformClient.wikiAuthoring(action, body));

// Mounted per Space: responses from an old Space cannot reach this component's successor.
export function WikiAuthoring({ spaceId, onPublished }: { spaceId: string; onPublished: () => void }) {
  const [context, setContext] = useState<PortableRecord | null>(null);
  const [raw, setRaw] = useState<PortableRecord[]>([]);
  const [rawAfter, setRawAfter] = useState<string | null>(null);
  const [selectedRaw, setSelectedRaw] = useState<string[]>([]);
  const [slugs, setSlugs] = useState<string[]>([]);
  const [instruction, setInstruction] = useState("");
  const [due, setDue] = useState("");
  const [queue, setQueue] = useState<PortableRecord[]>([]);
  const [queueAfter, setQueueAfter] = useState<string | null>(null);
  const [worker, setWorker] = useState<PortableRecord>({});
  const [proposalId, setProposalId] = useState("");
  const [proposal, setProposal] = useState<PortableRecord | null>(null);
  const [patch, setPatch] = useState<Patch | null>(null);
  const [validated, setValidated] = useState("");
  const [pending, setPending] = useState<Intent | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reason, setReason] = useState("");
  const [storageReady, setStorageReady] = useState(false);
  const alive = useRef(true);
  const locked = useRef(false);
  const storageKey = `knowledge.wiki.intent.v1:${spaceId}`;
  const disabled = busy || !!pending || !storageReady;

  async function refreshContext() {
    const result = await api("context", { space_id: spaceId });
    if (!alive.current) return;
    setContext(result); setRaw(records(result.raw_inventory)); setRawAfter(stringOf(result.raw_next_after) || null);
  }
  async function refreshQueue(after?: string) {
    const result = await api("queue", { space_id: spaceId, ...(after ? { after } : {}) });
    if (!alive.current) return;
    setQueue(previous => after ? [...previous, ...records(result.items)] : records(result.items));
    setQueueAfter(stringOf(result.next_after) || null); setWorker((result.worker || {}) as PortableRecord);
  }
  useEffect(() => {
    alive.current = true;
    try {
      const saved = sessionStorage.getItem(storageKey);
      if (saved) {
        const intent = JSON.parse(saved) as Intent;
        const body = intent.action === "enqueue" ? intent.body.request as PortableRecord : intent.body;
        if (!["generate", "enqueue", "apply", "abandon", "control_queue"].includes(intent.action) || body?.space_id !== spaceId) throw new Error("待恢复请求格式不匹配，请保留此标签页并检查浏览器存储。");
        setPending(intent);
      }
      setStorageReady(true);
      const lastProposal = sessionStorage.getItem(storageKey + ":proposal");
      if (lastProposal) setProposalId(lastProposal);
    } catch (e) { setError(errorMessage(e)); }
    void refreshContext().catch(e => { if (alive.current) setError(errorMessage(e)); });
    void refreshQueue().catch(e => { if (alive.current) setError(errorMessage(e)); });
    return () => { alive.current = false; };
    // The parent keys this component by Space.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function read(task: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true; setBusy(true); setError("");
    try { await task(); } catch (e) { if (alive.current) setError(errorMessage(e)); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function showProposal(result: PortableRecord) {
    sessionStorage.setItem(storageKey + ":proposal", stringOf(result.operation_id));
    setProposalId(stringOf(result.operation_id));
    setProposal(result); setPatch(result.state === "ready" ? result.patch as Patch : null); setValidated(""); setNotice("");
  }
  async function mutate(intent: Intent, retry = false) {
    if (locked.current || (!retry && pending)) return;
    locked.current = true; setBusy(true); setError(""); setNotice("");
    try {
      // Persist the exact operation before dispatch. Quota/storage failure prevents dispatch.
      sessionStorage.setItem(storageKey, JSON.stringify(intent));
      setPending(intent);
      const response = await platformClient.wikiAuthoring(intent.action, intent.body);
      // A transport error can hide a committed write. Keep the exact request for retry.
      if (response.status === "error") throw new Error(response.error?.message || "请求未确认，请使用原请求重试。");
      const result = dataOf(response);
      if (intent.action === "generate" || intent.action === "abandon") sessionStorage.setItem(storageKey + ":proposal", stringOf(result.operation_id));
      sessionStorage.removeItem(storageKey);
      if (!alive.current) return;
      setPending(null);
      if (intent.action === "generate" || intent.action === "abandon") showProposal(result);
      else if (intent.action === "apply") {
        setPatch(null); setProposal(null); setValidated(""); setNotice("补丁已发布。"); onPublished();
        await refreshContext();
      } else setNotice(intent.action === "enqueue" ? "已加入队列；生成结果仍需人工审查。" : "队列操作已记录。");
      await refreshQueue();
    } catch (e) { if (alive.current) setError(errorMessage(e)); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  function generate(enqueue: boolean) {
    if (!context) return;
    const request = { space_id: spaceId, operation_id: crypto.randomUUID(), expected_revision: context.revision, slugs, selected_raw: selectedRaw, instruction };
    const notBefore = due ? Math.floor(new Date(due).getTime() / 1000) : 0;
    if (!Number.isFinite(notBefore) || notBefore < 0 || notBefore > 4102444800) { setError("请选择有效的执行时间。"); return; }
    void mutate(enqueue ? { action: "enqueue", body: { request, not_before: notBefore } } : { action: "generate", body: request });
  }
  function edit(next: Patch) { setPatch(next); setValidated(""); setNotice(""); }
  function toggle(value: string, selected: string[], setter: (v: string[]) => void) {
    if (selected.includes(value)) setter(selected.filter(item => item !== value));
    else if (selected.length < 100) setter([...selected, value]);
    else setError("一次最多选择 100 项。");
  }
  function control(item: PortableRecord, action: "cancel" | "reschedule") {
    const notBefore = due ? Math.floor(new Date(due).getTime() / 1000) : NaN;
    if (action === "reschedule" && (!Number.isFinite(notBefore) || notBefore < 0 || notBefore > 4102444800)) { setError("重新排期需要有效的执行时间。"); return; }
    void mutate({ action: "control_queue", body: { space_id: spaceId, operation_id: item.operation_id, command_id: crypto.randomUUID(), expected_receipt: item.admission_receipt, reason, action, ...(action === "reschedule" ? { not_before: notBefore } : {}) } });
  }

  return <section className="card wiki-authoring" aria-label="Wiki 编辑与审查">
    <h2>Wiki 编辑与审查</h2>
    <p>选择来源和已有页面，描述修改意图。生成的补丁经过预检和明确发布后才会进入当前 Wiki。</p>
    {error && <p role="alert" className="error-banner">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {pending && <div className="capability-notice"><p>存在尚未确认的 {pending.action} 请求。重试会保留原操作编号和内容。</p><button disabled={busy} onClick={() => void mutate(pending, true)}>重试原请求</button><p>若服务明确拒绝了请求，可解除本地待确认状态后修正；这不会撤销服务端操作。</p><button disabled={busy} onClick={() => { try { sessionStorage.removeItem(storageKey); setPending(null); setNotice("已解除本地待确认状态，请刷新队列核实服务端结果。"); } catch (e) { setError(errorMessage(e)); } }}>解除本地待确认状态</button></div>}
    {!storageReady && <button disabled={busy} onClick={() => { try { sessionStorage.removeItem(storageKey); setPending(null); setStorageReady(true); setError(""); setNotice("已清除无法恢复的本地记录。请先刷新队列核实已有操作。"); } catch (e) { setError(errorMessage(e)); } }}>清除无法恢复的本地记录</button>}
    {!context ? <p>编辑上下文尚不可用。请确认当前 Space 已启用 Wiki Authoring。</p> : <>
      <fieldset disabled={disabled}>
        <legend>生成范围</legend>
        <div className="wiki-selection"><div><h3>来源快照 ({selectedRaw.length}/100)</h3>{raw.map(item => { const path = stringOf(item.snapshot_path); return <label key={path}><input type="checkbox" checked={selectedRaw.includes(path)} onChange={() => toggle(path, selectedRaw, setSelectedRaw)} />{path}</label>; })}{rawAfter && <button onClick={() => void read(async () => { const r = await api("context", { space_id: spaceId, raw_after: rawAfter }); if (alive.current) { setRaw(items => [...items, ...records(r.raw_inventory)]); setRawAfter(stringOf(r.raw_next_after) || null); } })}>更多来源</button>}</div>
        <div><h3>允许修改的已有页面 ({slugs.length}/100)</h3>{records(context.inventory).map(item => { const slug = stringOf(item.slug); return <label key={slug}><input type="checkbox" checked={slugs.includes(slug)} onChange={() => toggle(slug, slugs, setSlugs)} />{slug}</label>; })}</div></div>
        <label>修改意图<textarea aria-label="修改意图" value={instruction} onChange={e => setInstruction(e.target.value)} placeholder="说明需要创建、更新或退役的知识页面" /></label>
        <label>队列执行时间（本地时间，留空立即）<input type="datetime-local" value={due} onChange={e => setDue(e.target.value)} /></label>
        <div className="wiki-actions"><button disabled={!instruction.trim()} onClick={() => generate(false)}>生成待审补丁</button><button disabled={!instruction.trim()} onClick={() => generate(true)}>加入队列</button><button onClick={() => void read(refreshContext)}>刷新编辑上下文</button></div>
      </fieldset>
    </>}
    <div className="wiki-actions"><label>提案操作编号<input aria-label="提案操作编号" value={proposalId} disabled={disabled} onChange={e => setProposalId(e.target.value)} /></label><button disabled={disabled || !proposalId.trim()} onClick={() => void read(async () => { const result = await api("proposal", { space_id: spaceId, operation_id: proposalId.trim() }); if (alive.current) showProposal(result); })}>读取提案</button></div>
    {proposal && <div><h3>提案：{states[stringOf(proposal.state)] || stringOf(proposal.state)}</h3><p>操作编号：{stringOf(proposal.operation_id)}</p>{proposal.state === "unsettled" && <><p>不能据此判断执行进程是否已停止。放弃后，迟到的结果不会被采用。</p><button disabled={disabled || !reason.trim()} onClick={() => void mutate({ action: "abandon", body: { space_id: spaceId, operation_id: proposal.operation_id, expected_receipt: proposal.receipt_digest, reason } })}>放弃未确定提案</button></>}</div>}
    {patch && <fieldset disabled={disabled}><legend>审查补丁</legend>{patch.changes.map((change, i) => <div key={change.slug}><h3>{change.markdown === null ? "退役" : change.expected_digest ? "更新" : "创建"}：{change.slug}</h3>{change.markdown === null ? <p>替代页面：{change.replacement || "无"}</p> : <textarea aria-label={`页面 ${change.slug}`} value={change.markdown} onChange={e => edit({ ...patch, changes: patch.changes.map((c, j) => j === i ? { ...c, markdown: e.target.value } : c) })} />}</div>)}<label>完整索引<textarea aria-label="完整索引" value={patch.index} onChange={e => edit({ ...patch, index: e.target.value })} /></label><label>变更日志<textarea aria-label="变更日志" value={patch.log_entry} onChange={e => edit({ ...patch, log_entry: e.target.value })} /></label><div className="wiki-actions"><button onClick={() => void read(async () => { const exact = JSON.stringify(patch); await api("preview", { space_id: spaceId, patch }); if (alive.current) { setValidated(exact); setNotice("预检通过，可以发布当前补丁。"); } })}>预检补丁</button><button disabled={validated !== JSON.stringify(patch)} onClick={() => void mutate({ action: "apply", body: { space_id: spaceId, operation_id: crypto.randomUUID(), patch } })}>发布当前补丁</button></div></fieldset>}
    <h3>生成队列</h3><p>{worker.running ? "后台处理正在运行" : "后台处理未运行；排队不会自动发布"}</p>
    <label>取消、排期或放弃原因<input value={reason} disabled={disabled} onChange={e => setReason(e.target.value)} /></label>
    <button disabled={busy} onClick={() => void read(() => refreshQueue())}>刷新队列</button>
    {queue.map(item => <div className="wiki-queue-row" key={stringOf(item.operation_id)}><span>{stringOf(item.operation_id)} · {states[stringOf(item.state)] || stringOf(item.state)} · {Number(item.not_before) ? new Date(Number(item.not_before) * 1000).toLocaleString() : "立即"}</span><div className="wiki-actions">{["ready", "failed", "unsettled", "abandoned"].includes(stringOf(item.state)) && <button disabled={disabled} onClick={() => void read(async () => { const r = await api("proposal", { space_id: spaceId, operation_id: item.operation_id }); if (alive.current) showProposal(r); })}>查看提案</button>}{item.state === "queued" && <><button disabled={disabled || !reason.trim()} onClick={() => control(item, "cancel")}>取消排队</button><button disabled={disabled || !reason.trim() || !due} onClick={() => control(item, "reschedule")}>重新排期</button></>}</div></div>)}
    {queueAfter && <button disabled={busy} onClick={() => void read(() => refreshQueue(queueAfter))}>更多队列记录</button>}
  </section>;
}
