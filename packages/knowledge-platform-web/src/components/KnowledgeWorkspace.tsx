"use client";

import Link from "next/link";
import { WikiAuthoring } from "./WikiAuthoring";
import { FormEvent, ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  Bell,
  BookOpen,
  Boxes,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CloudCog,
  Database,
  File,
  FileCheck2,
  Files,
  FolderSearch,
  Gauge,
  Layers3,
  Loader2,
  Menu,
  Network,
  PanelLeftClose,
  RefreshCw,
  Search,
  ServerCog,
  Sparkles,
  UploadCloud,
  X,
} from "lucide-react";

import {
  dataOf,
  decodeAssetContent,
  errorMessage,
  evidenceOf,
  isInlineImageMimeType,
  numberOf,
  parseAssetResources,
  platformClient,
  records,
  relativeTime,
  shortDigest,
  stringOf,
  type PortableRecord,
  type AssetResource,
} from "@/lib/platform";

export type WorkspaceSection = "overview" | "library" | "search" | "sources" | "schema" | "imports" | "analytics";

type Discovery = {
  spaces: PortableRecord[];
  collections: PortableRecord[];
  assets: PortableRecord[];
};

const EMPTY_DISCOVERY: Discovery = { spaces: [], collections: [], assets: [] };

const navItems: Array<{ section: WorkspaceSection; href: string; label: string; icon: typeof Files }> = [
  { section: "overview", href: "/knowledge", label: "概览", icon: Gauge },
  { section: "library", href: "/knowledge/library", label: "资料库", icon: Files },
  { section: "search", href: "/knowledge/search", label: "搜索", icon: Search },
  { section: "sources", href: "/knowledge/sources", label: "知识来源", icon: Network },
  { section: "schema", href: "/knowledge/schema", label: "LLM Wiki", icon: Sparkles },
  { section: "imports", href: "/knowledge/imports", label: "任务中心", icon: UploadCloud },
  { section: "analytics", href: "/analytics", label: "智能问数", icon: Database },
];

const sectionCopy: Record<WorkspaceSection, { eyebrow: string; title: string; description: string }> = {
  overview: { eyebrow: "KNOWLEDGE WORKSPACE", title: "知识库", description: "浏览本地沉淀的资料，按需检索、阅读并交给 Agent 使用。" },
  library: { eyebrow: "LIBRARY WORKSPACE", title: "知识库 / 资料库", description: "统一查看本地 Catalog 中的文档、Wiki、网页和结构化资产。" },
  search: { eyebrow: "KNOWLEDGE SEARCH", title: "知识库 / 搜索", description: "通过独立 Platform API 搜索真实本地知识，不经过 PuddingClaw 后端。" },
  sources: { eyebrow: "KNOWLEDGE SOURCES", title: "知识库 / 知识来源", description: "查看 Connector、同步来源与授权状态。" },
  schema: { eyebrow: "WIKI & SEMANTIC", title: "知识库 / LLM Wiki", description: "查看 Wiki 与语义资产，生成、审查并发布知识补丁。" },
  imports: { eyebrow: "PROCESSING JOBS", title: "知识库 / 任务中心", description: "按 Job ID 查询独立 Platform 处理任务及其状态。" },
  analytics: { eyebrow: "KNOWLEDGE ANALYTICS", title: "智能问数", description: "查看数据库 Schema，并通过 Vanna Collection 生成受守卫的只读查询计划。" },
};

function field(item: PortableRecord, ...keys: string[]): string {
  for (const key of keys) {
    const value = stringOf(item[key]);
    if (value) return value;
  }
  return "";
}

function assetKind(item: PortableRecord): string {
  return field(item, "kind", "source_type") || "asset";
}

function assetTitle(item: PortableRecord): string {
  return field(item, "title", "name", "id") || "未命名资料";
}

function assetIcon(kind: string) {
  if (kind.includes("wiki")) return BookOpen;
  if (kind.includes("table") || kind.includes("database")) return Database;
  return File;
}

function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: "success" | "warning" | "danger" | "neutral" | "brand" }) {
  return <span className={`status-pill status-${tone}`}>{children}</span>;
}

function LoadingBlock({ label = "正在读取 Knowledge Platform…" }: { label?: string }) {
  return <div className="loading-block"><Loader2 size={18} className="spin" /><span>{label}</span></div>;
}

function ErrorBanner({ message }: { message: string }) {
  return <div className="error-banner"><CircleAlert size={17} /><span>{message}</span></div>;
}

function EmptyState({ icon: Icon = FolderSearch, title, detail }: { icon?: typeof Files; title: string; detail: string }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={21} /></span><strong>{title}</strong><p>{detail}</p></div>;
}

function ProductBoundary({ status }: { status: "checking" | "connected" | "error" }) {
  const label = status === "connected" ? "独立 Platform" : status === "checking" ? "连接中" : "Platform 不可用";
  return (
    <div className={"boundary-chip boundary-" + status} title="此界面只连接独立 Platform API，不调用 PuddingClaw /api">
      <span className="live-dot" /> {label}
    </div>
  );
}

export default function KnowledgeWorkspace({ section }: { section: WorkspaceSection }) {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [discovery, setDiscovery] = useState<Discovery>(EMPTY_DISCOVERY);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedSpace, setSelectedSpace] = useState("");
  const [selectedAsset, setSelectedAsset] = useState<PortableRecord | null>(null);
  const [assetBody, setAssetBody] = useState("");
  const [assetEvidence, setAssetEvidence] = useState<ReturnType<typeof evidenceOf>>([]);
  const [assetLoading, setAssetLoading] = useState(false);
  const [assetResources, setAssetResources] = useState<AssetResource[]>([]);
  const [assetResourcesLoading, setAssetResourcesLoading] = useState(false);
  const [assetResourcesError, setAssetResourcesError] = useState("");
  const assetGeneration = useRef(0);

  const refreshGeneration = useRef(0);
  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    setLoading(true);
    setError("");
    try {
      const spacesResult = await platformClient.listSpaces();
      const spaces = records(dataOf(spacesResult).spaces);
      const nextSpace = selectedSpace || field(spaces[0] || {}, "id");
      const [collectionsResult, assetsResult] = await Promise.all([
        platformClient.listCollections({ spaceId: nextSpace || undefined }),
        platformClient.listAssets({ spaceId: nextSpace || undefined }),
      ]);
      if (generation !== refreshGeneration.current) return;
      setSelectedSpace(nextSpace);
      setDiscovery({
        spaces,
        collections: records(dataOf(collectionsResult).collections),
        assets: records(dataOf(assetsResult).assets),
      });
    } catch (reason) {
      if (generation !== refreshGeneration.current) return;
      setError(errorMessage(reason));
      setDiscovery(EMPTY_DISCOVERY);
    } finally {
      if (generation === refreshGeneration.current) setLoading(false);
    }
  }, [selectedSpace]);

  useEffect(() => { void refresh(); return () => { ++refreshGeneration.current; }; }, [refresh]);

  async function openAsset(asset: PortableRecord) {
    const generation = ++assetGeneration.current;
    const assetId = field(asset, "id");
    setSelectedAsset(asset);
    setAssetBody("");
    setAssetEvidence([]);
    setAssetResources([]);
    setAssetResourcesError("");
    setAssetLoading(true);
    setAssetResourcesLoading(true);
    const resourcesPromise = fetch(`/v1/assets/${encodeURIComponent(assetId)}/resources`, { cache: "no-store" })
      .then(async (response) => {
        if (!response.ok) throw new Error(`资源列表请求失败（${response.status}）`);
        return response.json() as Promise<unknown>;
      })
      .then((payload) => parseAssetResources(assetId, payload && typeof payload === "object" && !Array.isArray(payload) ? (payload as PortableRecord).data : payload))
      .then((resources) => ({ ok: true as const, resources }), (reason) => ({ ok: false as const, reason }));
    const bodyPromise = platformClient.readAsset(assetId, {
      resource_uri: field(asset, "source_uri") || undefined,
      start: 0,
      end: 8 * 1024 * 1024,
    });
    try {
      const result = await bodyPromise;
      if (generation === assetGeneration.current) {
        const data = dataOf(result);
        setAssetBody(decodeAssetContent(data));
        setAssetEvidence(evidenceOf(result));
      }
    } catch (reason) {
      if (generation === assetGeneration.current) setAssetBody(`无法读取正文：${errorMessage(reason)}`);
    } finally {
      if (generation === assetGeneration.current) setAssetLoading(false);
    }
    try {
      const outcome = await resourcesPromise;
      if (generation === assetGeneration.current) {
        if (outcome.ok) setAssetResources(outcome.resources);
        else setAssetResourcesError(errorMessage(outcome.reason));
      }
    } finally {
      if (generation === assetGeneration.current) setAssetResourcesLoading(false);
    }
  }

  const current = sectionCopy[section];
  const connectionStatus = loading ? "checking" : error ? "error" : "connected";
  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : "sidebar-closed"}`}>
        <div className="brand-row">
          <div className="brand-mark">P</div>
          {sidebarOpen ? <div><strong>PuddingKnowledge</strong><span>Agent-native Platform</span></div> : null}
          <button className="icon-button sidebar-toggle" onClick={() => setSidebarOpen((value) => !value)} aria-label="切换侧栏">
            {sidebarOpen ? <PanelLeftClose size={17} /> : <Menu size={17} />}
          </button>
        </div>
        <nav className="side-nav" aria-label="Knowledge Platform 导航">
          {navItems.map((item) => {
            const Icon = item.icon;
            return <Link key={item.section} href={item.href} className={section === item.section ? "active" : ""} title={item.label}><Icon size={17} />{sidebarOpen ? <span>{item.label}</span> : null}</Link>;
          })}
        </nav>
        {sidebarOpen ? (
          <div className="sidebar-footer">
            <ProductBoundary status={connectionStatus} />
            <p>本地数据 · 不依赖 Claw Runtime</p>
          </div>
        ) : null}
      </aside>

      <main className="main-column">
        <header className="topbar">
          {!sidebarOpen ? <button className="icon-button mobile-toggle" onClick={() => setSidebarOpen(true)}><Menu size={17} /></button> : null}
          <div className="space-switcher">
            <span>Space</span>
            <select value={selectedSpace} onChange={(event) => setSelectedSpace(event.target.value)} aria-label="选择 Space">
              {discovery.spaces.map((space) => <option key={field(space, "id")} value={field(space, "id")}>{field(space, "name", "id")}</option>)}
            </select>
          </div>
          <div className="topbar-actions"><ProductBoundary status={connectionStatus} /><button className="icon-button" onClick={() => void refresh()} title="刷新"><RefreshCw size={16} /></button><button className="icon-button" title="通知"><Bell size={16} /></button></div>
        </header>

        <div className="page-scroll">
          <div className="page-container">
            <section className="page-heading">
              <div><span className="eyebrow">{current.eyebrow}</span><h1>{current.title}</h1><p>{current.description}</p></div>
              <div className="heading-stat"><span>当前数据</span><strong>{discovery.assets.length}</strong><small>Catalog Assets</small></div>
            </section>
            <div className="workspace-tabs">
              {navItems.slice(0, 6).map((item) => <Link key={item.section} href={item.href} className={section === item.section ? "active" : ""}>{item.label}</Link>)}
            </div>
            {error ? <ErrorBanner message={`无法连接独立 Platform API：${error}`} /> : null}
            {loading ? <LoadingBlock /> : (
              <>
                {section === "overview" ? <Overview discovery={discovery} onOpenAsset={openAsset} connected={!error} /> : null}
                {section === "library" ? <Library assets={discovery.assets} onOpenAsset={openAsset} /> : null}
                {section === "search" ? <SearchView spaceId={selectedSpace} onOpenAsset={openAsset} /> : null}
                {section === "sources" ? <SourcesView spaceId={selectedSpace} /> : null}
                {section === "schema" ? <SchemaView spaceId={selectedSpace} assets={discovery.assets} onOpenAsset={openAsset} onPublished={() => void refresh()} /> : null}
                {section === "imports" ? <JobsView /> : null}
                {section === "analytics" ? <AnalyticsView spaceId={selectedSpace} collections={discovery.collections} /> : null}
              </>
            )}
          </div>
        </div>
      </main>

      {selectedAsset ? <AssetDrawer asset={selectedAsset} body={assetBody} evidence={assetEvidence} loading={assetLoading} resources={assetResources} resourcesLoading={assetResourcesLoading} resourcesError={assetResourcesError} onClose={() => { ++assetGeneration.current; setSelectedAsset(null); }} /> : null}
    </div>
  );
}

function Overview({ discovery, onOpenAsset, connected }: { discovery: Discovery; onOpenAsset: (asset: PortableRecord) => void; connected: boolean }) {
  const wikiCount = discovery.assets.filter((asset) => assetKind(asset).includes("wiki")).length;
  const sourceKinds = new Set(discovery.assets.map(assetKind)).size;
  const recent = discovery.assets.slice(0, 5);
  return (
    <div className="content-grid">
      <section className="metric-grid full-span">
        <Metric icon={Boxes} label="Collections" value={discovery.collections.length} detail="可查询集合" />
        <Metric icon={Files} label="Assets" value={discovery.assets.length} detail="Catalog 元数据" />
        <Metric icon={BookOpen} label="Wiki Pages" value={wikiCount} detail="可读取正文" />
        <Metric icon={Layers3} label="Asset Kinds" value={sourceKinds} detail="来源类型" />
      </section>
      <section className="card two-thirds">
        <CardHeader icon={Activity} title="最近资料" detail="来自当前 Platform Catalog" action={<Link href="/knowledge/library">查看全部 <ChevronRight size={14} /></Link>} />
        <AssetRows assets={recent} onOpenAsset={onOpenAsset} compact />
      </section>
      <section className="card one-third">
        <CardHeader icon={ServerCog} title="运行边界" detail="当前验收链路" />
        <div className="boundary-list">
          <BoundaryRow label="Frontend" value="独立 Next 应用" ok />
          <BoundaryRow label="API" value={connected ? "Platform /v1" : "连接失败"} ok={connected} />
          <BoundaryRow label="数据" value={connected ? "本地 Catalog 副本" : "未读取"} ok={connected} />
          <BoundaryRow label="Claw 3000/8888" value="不需要" ok />
          <BoundaryRow label="生产激活" value="未开放" />
        </div>
      </section>
    </div>
  );
}

function Metric({ icon: Icon, label, value, detail }: { icon: typeof Files; label: string; value: number; detail: string }) {
  return <div className="metric-card"><span className="metric-icon"><Icon size={19} /></span><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></div>;
}

function CardHeader({ icon: Icon, title, detail, action }: { icon: typeof Files; title: string; detail: string; action?: ReactNode }) {
  return <header className="card-header"><div><span className="card-icon"><Icon size={17} /></span><div><h2>{title}</h2><p>{detail}</p></div></div>{action ? <div className="card-action">{action}</div> : null}</header>;
}

function BoundaryRow({ label, value, ok = false }: { label: string; value: string; ok?: boolean }) {
  return <div><span>{label}</span><strong>{value}</strong><i className={ok ? "ok" : "pending"} /></div>;
}

function Library({ assets, onOpenAsset }: { assets: PortableRecord[]; onOpenAsset: (asset: PortableRecord) => void }) {
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("all");
  const kinds = useMemo(() => Array.from(new Set(assets.map(assetKind))).sort(), [assets]);
  const visible = useMemo(() => assets.filter((asset) => {
    if (kind !== "all" && assetKind(asset) !== kind) return false;
    const keyword = query.trim().toLowerCase();
    if (!keyword) return true;
    return [assetTitle(asset), field(asset, "description"), field(asset, "source_type"), field(asset, "id")].join(" ").toLowerCase().includes(keyword);
  }), [assets, kind, query]);
  return (
    <section className="card">
      <div className="filterbar">
        <label className="search-field"><Search size={16} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题、来源或 Asset ID" /></label>
        <select value={kind} onChange={(event) => setKind(event.target.value)}><option value="all">全部类型</option>{kinds.map((value) => <option key={value}>{value}</option>)}</select>
        <span>{visible.length} 项</span>
      </div>
      <AssetRows assets={visible} onOpenAsset={onOpenAsset} />
    </section>
  );
}

function AssetRows({ assets, onOpenAsset, compact = false }: { assets: PortableRecord[]; onOpenAsset: (asset: PortableRecord) => void; compact?: boolean }) {
  if (!assets.length) return <EmptyState title="还没有资料" detail="当前 Space 中没有可展示的 Asset。" />;
  return (
    <div className="asset-table">
      {!compact ? <div className="asset-table-head"><span>名称</span><span>来源</span><span>更新时间</span><span>状态</span></div> : null}
      {assets.map((asset) => {
        const kind = assetKind(asset);
        const Icon = assetIcon(kind);
        return (
          <button key={field(asset, "id")} className={`asset-row ${compact ? "compact" : ""}`} onClick={() => onOpenAsset(asset)}>
            <span className="asset-main"><i><Icon size={17} /></i><span><strong>{assetTitle(asset)}</strong><small>{kind} · {shortDigest(asset.revision || asset.content_digest)}</small></span></span>
            {!compact ? <><span>{field(asset, "source_type") || "Platform"}</span><span>{relativeTime(asset.updated_at || asset.created_at)}</span><span><StatusPill tone={kind === "wiki_page" ? "success" : "neutral"}>{kind === "wiki_page" ? "可阅读" : "查看详情"}</StatusPill></span></> : <ChevronRight size={16} />}
          </button>
        );
      })}
    </div>
  );
}

function SearchView({ spaceId, onOpenAsset }: { spaceId: string; onOpenAsset: (asset: PortableRecord) => void }) {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState<PortableRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    const value = input.trim();
    if (!value) return;
    setQuery(value); setLoading(true); setError("");
    try {
      const data = dataOf(await platformClient.search({ query: value, space_id: spaceId || undefined, limit: 30 }));
      setItems(records(data.assets));
    } catch (reason) { setError(errorMessage(reason)); setItems([]); }
    finally { setLoading(false); }
  }
  return (
    <div className="stack">
      <section className="card search-card">
        <form onSubmit={submit}><Search size={20} /><input value={input} onChange={(event) => setInput(event.target.value)} placeholder="搜索文章、Wiki、文件和数据资产……" autoFocus /><button disabled={!input.trim() || loading}>{loading ? <Loader2 size={16} className="spin" /> : null}搜索</button></form>
        <p>结果来自当前本地 Catalog；已有正文 binding 的 Wiki 可在右侧详情抽屉阅读。</p>
      </section>
      <section className="card">
        <CardHeader icon={FolderSearch} title={query ? `“${query}” 的结果` : "搜索本地知识"} detail={query ? `${items.length} 个匹配项` : "输入关键词开始搜索"} />
        {error ? <ErrorBanner message={error} /> : loading ? <LoadingBlock label="正在搜索…" /> : items.length ? <AssetRows assets={items} onOpenAsset={onOpenAsset} compact /> : <EmptyState title={query ? "没有找到匹配内容" : "等待搜索"} detail={query ? "换一个关键词再试试。" : "可搜索标题、描述和来源元数据。"} />}
      </section>
    </div>
  );
}

function SourcesView({ spaceId }: { spaceId: string }) {
  const [connectors, setConnectors] = useState<PortableRecord[]>([]);
  const [authorizations, setAuthorizations] = useState<PortableRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const [connectorResult, authorizationResult] = await Promise.all([
          platformClient.listConnectors({ spaceId }),
          platformClient.listConnectorAuthorizations({ spaceId }),
        ]);
        if (!active) return;
        setConnectors(records(dataOf(connectorResult).connectors));
        setAuthorizations(records(dataOf(authorizationResult).authorizations));
      } catch (reason) { if (active) setError(errorMessage(reason)); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [spaceId]);
  return (
    <div className="content-grid">
      <section className="card two-thirds"><CardHeader icon={Network} title="连接器" detail="Catalog 中已登记的知识来源" />{loading ? <LoadingBlock /> : error ? <CapabilityNotice capability="Connector Runtime" message={error} /> : connectors.length ? <RecordCards items={connectors} /> : <EmptyState title="没有连接器" detail="当前 Space 尚未登记 Connector。" />}</section>
      <section className="card one-third"><CardHeader icon={CloudCog} title="授权状态" detail="只显示逻辑状态，不暴露凭据" />{authorizations.length ? <RecordCards items={authorizations} /> : <EmptyState icon={CloudCog} title="暂无授权记录" detail="OAuth 由宿主/Vault 管理。" />}</section>
    </div>
  );
}

function SchemaView({ spaceId, assets, onOpenAsset, onPublished }: { spaceId: string; assets: PortableRecord[]; onOpenAsset: (asset: PortableRecord) => void; onPublished: () => void }) {
  const [semantic, setSemantic] = useState<PortableRecord[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    platformClient.listSemanticAssets({ spaceId }).then((result) => { if (active) setSemantic(records(dataOf(result).assets)); }).catch((reason) => { if (active) setError(errorMessage(reason)); });
    return () => { active = false; };
  }, [spaceId]);
  const wiki = assets.filter((asset) => assetKind(asset).includes("wiki"));
  return (
    <div className="content-grid">
      <WikiAuthoring key={spaceId} spaceId={spaceId} onPublished={onPublished} />
      <section className="card two-thirds"><CardHeader icon={BookOpen} title="已发布 Wiki" detail="当前 Catalog 中可查询的页面" />{wiki.length ? <AssetRows assets={wiki} onOpenAsset={onOpenAsset} compact /> : <EmptyState title="暂无 Wiki" detail="没有已发布的 Wiki Asset。" />}</section>
      <section className="card one-third"><CardHeader icon={Sparkles} title="语义资产" detail="独立 Semantic Registry" />{error ? <CapabilityNotice capability="Semantic Authoring Runtime" message={error} /> : semantic.length ? <RecordCards items={semantic} /> : <EmptyState icon={Sparkles} title="暂无语义资产" detail="当前 registry 中没有记录。" />}</section>
    </div>
  );
}

function JobsView() {
  const [jobId, setJobId] = useState("");
  const [job, setJob] = useState<PortableRecord | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); if (!jobId.trim()) return; setLoading(true); setError(""); setJob(null);
    try { setJob(dataOf(await platformClient.getJob(jobId.trim())).job as PortableRecord); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setLoading(false); }
  }
  return (
    <section className="card">
      <CardHeader icon={UploadCloud} title="任务查询" detail="当前公共契约支持按 Job ID 读取；任务列表将在 Processing 聚合接口完成后接入" />
      <form className="inline-form" onSubmit={submit}><input value={jobId} onChange={(event) => setJobId(event.target.value)} placeholder="输入 Job ID" /><button disabled={!jobId.trim() || loading}>{loading ? <Loader2 size={15} className="spin" /> : <Search size={15} />}查询</button></form>
      {error ? <ErrorBanner message={error} /> : job ? <RecordDetail item={job} /> : <EmptyState icon={FileCheck2} title="等待查询" detail="导入、编译和同步任务都会通过统一 Job contract 暴露。" />}
    </section>
  );
}

function AnalyticsView({ spaceId, collections }: { spaceId: string; collections: PortableRecord[] }) {
  const [datasetId, setDatasetId] = useState(field(collections[0] || {}, "id"));
  const [schema, setSchema] = useState<PortableRecord[]>([]);
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<PortableRecord | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const available = collections.map((collection) => field(collection, "id")).filter(Boolean);
    if (!available.includes(datasetId)) setDatasetId(available[0] || "");
    setSchema([]);
    setResult(null);
  }, [collections, datasetId, spaceId]);
  async function loadSchema() {
    setLoading(true); setError("");
    try { setSchema(records(dataOf(await platformClient.listDatabaseSchema({ spaceId, datasetId })).tables)); }
    catch (reason) { setError(errorMessage(reason)); setSchema([]); }
    finally { setLoading(false); }
  }
  async function plan(event: FormEvent) {
    event.preventDefault(); if (!question.trim()) return; setLoading(true); setError(""); setResult(null);
    try { setResult(dataOf(await platformClient.databaseNl2Sql({ space_id: spaceId, dataset_id: datasetId, question: question.trim() }))); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setLoading(false); }
  }
  return (
    <div className="content-grid">
      <section className="card one-third"><CardHeader icon={Database} title="数据库 Schema" detail="只读取已绑定的数据集" /><label className="field-label">Dataset ID<input value={datasetId} onChange={(event) => setDatasetId(event.target.value)} /></label><button className="secondary-button" onClick={() => void loadSchema()} disabled={!datasetId || loading}><RefreshCw size={15} />读取 Schema</button>{schema.length ? <RecordCards items={schema} /> : null}</section>
      <section className="card two-thirds"><CardHeader icon={Sparkles} title="Vanna 问数" detail="先生成 QueryPlan；当前页面不接受原始 SQL" /><form className="question-form" onSubmit={plan}><textarea value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：统计本地车型能源类型数量" /><button disabled={!question.trim() || loading}>{loading ? <Loader2 size={15} className="spin" /> : <Sparkles size={15} />}生成查询计划</button></form>{error ? <CapabilityNotice capability="Database / Vanna Runtime" message={error} /> : result ? <RecordDetail item={result} /> : <EmptyState icon={Database} title="等待问题" detail="连接独立数据库 Runtime 后，这里会展示可审计 QueryPlan。" />}</section>
    </div>
  );
}

function CapabilityNotice({ capability, message }: { capability: string; message: string }) {
  return <div className="capability-notice"><CircleAlert size={18} /><div><strong>{capability} 尚未挂载</strong><p>{message}</p></div></div>;
}

function RecordCards({ items }: { items: PortableRecord[] }) {
  return <div className="record-cards">{items.map((item, index) => <div key={field(item, "id") || index}><strong>{field(item, "name", "title", "table_name", "id") || `记录 ${index + 1}`}</strong><span>{field(item, "status", "type", "connector_type") || "unknown"}</span><small>{field(item, "description", "source_type", "updated_at") || shortDigest(item.revision)}</small></div>)}</div>;
}

function RecordDetail({ item }: { item: PortableRecord }) {
  return <pre className="record-detail">{JSON.stringify(item, null, 2)}</pre>;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function AssetDrawer({ asset, body, evidence, loading, resources, resourcesLoading, resourcesError, onClose }: { asset: PortableRecord; body: string; evidence: ReturnType<typeof evidenceOf>; loading: boolean; resources: AssetResource[]; resourcesLoading: boolean; resourcesError: string; onClose: () => void }) {
  return (
    <div className="drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="asset-drawer" role="dialog" aria-modal="true" aria-label="Asset 详情">
        <header><button className="icon-button" onClick={onClose}><ChevronLeft size={17} /></button><div><span>{assetKind(asset)}</span><h2>{assetTitle(asset)}</h2></div><button className="icon-button" onClick={onClose}><X size={17} /></button></header>
        <div className="drawer-meta"><div><span>Asset ID</span><code>{field(asset, "id")}</code></div><div><span>Revision</span><code>{shortDigest(asset.revision || asset.content_digest)}</code></div><div><span>Source</span><code>{field(asset, "source_type") || "Platform"}</code></div></div>
        <section className="drawer-content"><h3>正文预览</h3>{loading ? <LoadingBlock label="正在读取正文…" /> : <pre>{body || "该 Asset 没有可预览的文本内容。"}</pre>}</section>
        <section className="drawer-resources"><h3>附件与图片</h3>{resourcesLoading ? <p>正在读取资源列表…</p> : resourcesError ? <p className="resource-error">资源暂不可用：{resourcesError}</p> : resources.length ? resources.map((resource) => isInlineImageMimeType(resource.mime_type) ? <figure key={resource.id}><img src={resource.url} alt={resource.name} /><figcaption>{resource.name} · {formatBytes(resource.size_bytes)}</figcaption></figure> : <div className="resource-link" key={resource.id}><a href={resource.url} download={resource.name}>{resource.name}</a><span>{resource.mime_type} · {formatBytes(resource.size_bytes)}</span></div>) : <p>该 Asset 没有附件资源。</p>}</section>
        <section className="drawer-evidence"><h3>Portable Evidence</h3>{evidence.length ? evidence.map((item) => <div key={`${item.asset_id}-${item.resource_uri}`}><code>{item.resource_uri}</code><span>{item.matched_by?.join(" · ") || "resource_uri"}</span></div>) : <p>正文读取后会显示 knowledge:// Evidence。</p>}</section>
      </aside>
    </div>
  );
}
