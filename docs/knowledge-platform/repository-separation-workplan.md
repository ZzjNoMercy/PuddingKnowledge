# Knowledge / Harness 完整仓库拆分执行清单

目标：两个保留相关 Git 历史、能够独立开发、安装、构建、测试和运行的完整仓库。原 PuddingClaw 保留 legacy 实现与回滚能力。本清单跟踪执行，不替换 `agent-native-knowledge-platform-spec.md` 的验收条件。

## 当前事实（2026-09-09）

- 尚无完成抽取的 `puddingknowledge` / `puddingharness` 仓库。
- Platform 已有无原 checkout 依赖的本地 Catalog/Wiki/PostgreSQL+Vanna examples 安装验证；这不是全能力 Platform 发行物。
- Harness 的旧 23-file mixed scan 不能证明完整依赖闭包。新增 `packages/puddingharness-extraction/audit.py` 扫描拟进入目标的全部 Python runtime，应用目标 overlay 后检查被排除模块引用、业务协议符号和动态 import。
- `overlays/` 只保存未来目标仓库 cleanup 内容，没有覆盖 PuddingClaw 原文件。其来源与内容摘要记录在 `overlay-provenance.json`。
- 旧 Extraction Manifest 曾错误把 legacy Knowledge/Analytics/Vanna 和旧业务页面指定进入 Harness；已改为明确 `preserve-legacy -> PuddingClaw`。ToolIntentRouter 按规范整个排除。
- `runtime_control` 的数据库时钟/租约参数已与 Knowledge queue 解耦，旧 queue 兼容导出与行为保留。
- 已准备 66 个目标 overlay。完整 FastAPI lifespan、HTTP Session/Agent 普通 Run、真实 DeepAgents 图及 Headless 边界已在开发 Python 环境和离线模型下通过。默认 Harness SQLite 版本 2 初始化与错误库只读拒绝、连接/租约释放已验证。
- Harness 前端已在隔离 stage 内独立 `npm ci`（无 node_modules 复用）、TypeScript 和 Next build 通过，生成 24 个页面。582 个交付文件和 11 个当前 overlay 的摘要已核对；可重跑入口见 `packages/puddingharness-extraction/frontend-staging.md`。这仍是开发抽取产物，没有完成 Git 历史拆仓或真实浏览器跨服务验收。
- `execute_skill` 在当前源 DeepAgents 本来不对外暴露；不能把 allowlist 中存在工厂名字记作可用能力。Skill secret/runtime 工具由 DeepAgents 显式绑定，后续目标运行测试必须证明此绑定仍在。

最新静态证据：`artifacts/repository-split/harness-python-audit-round11.json`，287 selected / 809 excluded / 4 findings，仍为 `blocked`。142 项 Python 测试通过；独立 locked/non-editable 后端安装中 7 项真实图、HTTP Agent、DB/Headless/租约边界通过；519 个安装文件与当前有效目标摘要一致，证据为 `artifacts/repository-split/harness-backend-install-round1.json`。剩余 4 项为两个只读兼容 selector 字面量及两个固定 registry 动态 import。Skill 自有 import 根已用真实启动目录验证；旧产品环境变量已同步，两个 sandbox probe 字符串确认只是本地文件标记后直接改为目标完整字面量，没有拼接隐藏扫描结果。

前端沿用已核验的独立 npm ci/TypeScript/Next build 证据，24 页面、582 文件、Evaluation hook 11 场景；本轮未修改前端。Knowledge 独立安装的结构化来源白名单、Logical Dataset author/publish/query、跨 Space 拒绝和 CLI 连续进程重启通过，53 项相关测试见 `artifacts/repository-split/knowledge-structured-install-round2.json`。它仍按显式输入生成私有 Catalog snapshot，未成为完整生产生命周期。

本轮历史读取边界：新 Evaluation 使用 protocol-2.0；protocol-1.0 experiment 仅内存投影，repository 按原始 JSON 行阻止执行、删除、更新、取消和 attempt 写入，避免 model_dump 丢失内部标记后绕过。Session 已移除 SQL 业务 ledger 接口和专用交接字段，旧本地 SQL 结果仍只读分页，不重执行；通用 evidence/validation receipt/Goal/Delegation 保留。useEvalStream 改为先创建 Session 再调用 Agent，仅非空 completed 最终答复且五维评分一致、保存成功后完成；人工输入、EOF、错误和预算截断均不能保存成功结果。

对抗审查关闭：SQLite WAL 在错误库校验前写入、literal %2F 路径校验错库、版本历史与实际表结构不一致、Evaluation 环境变量半迁移及历史标记丢失、staging 遗漏 overlay 摘要和安装状态、Session 未进入 overlay 的旧 schema registry 调用。完整有效树复核后排除了两个数据库证据 registry、旧 mixed catalog migration 与 8 个 Platform query/admin Skill；hv-analysis/github-monitor 通用骨架保留。

下一组同步点：独立 Worker CLI/桌面/部署合同；Research/MCP 通用宿主能力继承及 Resource 互通；完整 Platform Processing/Authoring/Query 发行验证；共同来源历史抽取与升级回滚。两个最终仓库仍未完成。

## 必须关闭的验收项

| 交付/约束 | 规范来源 | 必须取得的证据 | 状态 |
| --- | --- | --- | --- |
| 保留两个目标相关历史与共同来源 | §16 Phase 10.1–2、10.5 | 源 commit/tag、filter-repo 参数、commit-map、目标 log、可重放树摘要 | 未完成 |
| 源仓完整保留 | §11.8、Phase 10.8 | 原 legacy 路径和回归、无目标 cleanup 删除反向写回 | 持续验证 |
| Knowledge 全部 Query 能力 | §11.2–11.7、§17.1–17.5 | 独立安装后的 Document/Wiki/Table/Database/Package/Workspace 与拒绝用例 | 部分本地验证 |
| Processing/Authoring 连续性 | §11.9–11.10、Phase 7–8 | Import/Read Later/Connector/Compile/Semantic/Logical Dataset 实际生命周期与恢复 | 未完成完整独立验证 |
| Harness Agent/Session/Headless/Delegation | §11.11、§17.7 | 完整 runtime 启动与真实运行，不含活动 analytics_model_id/业务 receipt | 运行链/活动SQL账本与独立安装已验证 |
| Harness Tools/MCP/Research | §11.8、11.18、§17.2 | 无业务工具/路由/虚拟挂载；动态 MCP tools/resources + Research E2E | 固定工厂已验证；Research继承及Resource互通未完成 |
| Harness 通用 Evaluation 保留 | §11.16 | 原通用 trajectory/rubric/latency/token 回归通过，业务 invariant 迁出 | 未完成 |
| 独立 Catalog/Base/SessionFactory/migrations | §17.6 | 两环境 DB 初始化、迁移与反例，无跨库 FK/共享事务/源码依赖 | Harness SQLite通过；PG/完整升级未完成 |
| 两个完整前端 | §11.12、§17.8 | 无原 checkout 的两套 build；真实浏览器流程；Harness 无业务页面 | Harness独立stage构建通过；Platform完整UI及浏览器互通待验证 |
| 两套发行/部署/基础设施 | §11.13、§17.8 | 独立锁、SBOM、镜像/桌面/CLI、deploy/status/migrate/import/export/index | 未完成 |
| Provider/credentials 独立 | §11.14–11.15、11.19 | 独立 provider/secret binding，Harness 无 gbrain/Milvus 等 lifecycle | 未完成 |
| 外部 MCP 黑盒互通 | §11.16、Phase 10.7 | 两仓独立服务之间调用及权限/断线/Resource/Evidence 负例 | 未完成 |
| 真实存量升级与有状态回退 | §11.20、§17.10 | 干净安装、真实数据副本、中断续传、幂等、凭据失败、切换后新增数据回退 | 尚仅有部分 shadow |
| 生产切流/soak/正式发行 | Phase 8、10、11、§17.9 | 实际部署观测、签名发行与明确 release decision | 未满足；不能以本地测试替代 |

## 当前实施顺序

1. 修正目标归属、建立全树闭包检查，并把混合文件清理写成目标 overlay。
2. 闭合 Harness app/config/DB/MCP/DeepAgents/协议/Research/Evaluation，保留通用运行能力；不能删除整个通用域来让扫描变绿。
3. 完成两套 UI、部署/CLI/依赖锁与全部 Platform runtime capabilities；用隔离安装环境验证。
4. 根据已明确的开发拆仓顺序，用共同来源的 Git 历史抽取；生成并核对来源映射、cleanup commits 和可重放清单。生产迁移/正式发行仍按对应门禁执行。
5. 对照上表和规范逐项审计最终两个仓库。任何缺少直接证据的项都保持未完成。

## 可重跑入口

```bash
python3 packages/puddingharness-extraction/audit.py --output /private/tmp/harness-audit-new.json
PYTHONPATH=backend backend/.venv/bin/python -m pytest -q packages/puddingharness-extraction/test
node packages/puddingharness-extraction/test/frontend-boundary.test.mjs
```

审计报告的 `python_static_clean` 即使出现，也只表示选定 Python runtime 的静态检查通过，不能提升为完整仓库完成。Python tests、Skills 运行目录、前端、发行资产和运行时 import 仍分别需要证据。

2026-09-09 增量：Coordinator 目标 overlay 已同步退役选择器接口，27 overlays / 81 tests passed；审查发现的旧 Analytics 验收证据注入已修复并有反例测试。round6 仍 blocked（248 findings）；DeepAgents、Headless、Delegation、Evaluation 与完整前端/发行闭包继续保留为未完成项。

2026-09-09 运行链增量：38 overlays / 98 Python tests passed；真实图+离线模型验证普通文件读取 Run，Headless 输入等待/恢复与独立活动表 IO 通过。完整 app import 仍被旧 Chat runtime 阻断，两个最终 Git 仓库与正式发行均未完成。

2026-09-09 完整 Harness 应用增量：59 overlays / 120 Python tests；实际 HTTP Agent + 独立 SQLite 初始化、失败清理及重复 Home 占锁拒绝通过；Evaluation2.0/历史只读和活动SQL ledger退役完成本轮边界验证。Harness前端独立npm ci/tsc/Next build通过，24页面/582文件摘要已核验。round10仍blocked，两个最终Git仓库、完整Platform、MCP互通与生产验收保持未完成。

最终只读复核补充：五维评估取消不能回滚已发送的有效 completed 结果保存请求；UI不会据此误报成功，但保存阶段取消的展示语义仍可改进。`evalApi.ts`由有效前端base树携带，`api.eval_api`已注册对应通用保存端点，不能因没有overlay误判缺文件。


2026-09-09 独立安装与结构化生命周期增量：66 overlays / 142 Harness Python tests / 7 independently installed runtime tests；完整环境迁移后保持 source 59 个基线文件摘要不变。一次 env 批量替换误覆盖了四个已清理 target：DeepAgents 通过原转换脚本重放命中基线 SHA，Headless 通用 registry、等待保护与拒绝语义恢复后复验；旧 Home 负例断言恢复并增加 owned DB/Session 持久化断言。staging 目录/符号链接/敏感资源/锁文件/静态结果不得提升完整发行资格的反例已覆盖。

Knowledge 已把真实 Table Query / Logical Authoring / durable Processing 接入独立本地 CLI。对抗式实测关闭了未配置来源 author、静态绑定忽略 Space、端口 TIME_WAIT 不能重启、Wiki snapshot 重复主键四项问题。连续两个独立进程重放同一发布 job、保留 source Catalog/CSV、拒绝他源 ID 覆盖通过；完整 Processing/Authoring、最终两个 Git 仓库及生产验收仍未完成。


2026-09-09 Research / MCP 宿主边界增量：67 overlays；最终完整 Harness Python suite 172 项通过，最新 locked/non-editable 独立安装 11 项真实图/HTTP/DB/Headless/Research/Resource 权限运行测试通过，520 个安装文件摘要一致。静态 round13 为 288 selected / 814 excluded / 4 findings，仍 blocked。证据：`artifacts/repository-split/harness-backend-install-round2.json`、`harness-python-audit-round13.json`。Research 继承父模型、workspace backend、Run 权限及 middleware，禁止递归和写工具，硬 10 次工具预算；真实 MCP 拒绝暂停和 Resource 批准后恰好执行一次已验证。Resource 显式 server 绑定、完整 URI 授权摘要、配置深复制、取消传播、ToolMessage error 状态和 SDK/ASGI Platform 互通通过。对抗审查修复了忽略 model_copy 返回造成的测试假阳性、资源失败误记 evidence、嵌套 headers 浅复制三项问题。

历史抽取已建立临时独立 source clone `/private/tmp/pudding-split-source-prep-round1`，基准 HEAD 保持 `5c03ad3f68e2789d6d5c7110995b580d00abc832`，尚未提交选定快照或执行 filter-repo。开发 source snapshot 明确纳入未跟踪 Platform 与目标 overlay，并保留相关 Git 历史；正式签名发行、生产激活及两个完整仓库仍未完成。MCP 当前 SDK fixture 验证不能替代两个独立服务的黑盒进程验收。


前端最终清单更正：原 582 个文件统计包含其他端口的 `.next-*` 缓存，不能作为纯交付源码数量。已修复 staging 为排除全部 `.next-*` 目录，新增不同端口反例；新独立 npm ci / tsc / Next build 通过，实际 258 个源码文件、11 个 overlay、0 个缓存交付文件，逐项摘要核对通过。以 `artifacts/repository-split/harness-frontend-build-round2.json` 和 `/private/tmp/puddingharness-frontend-stage-round13/harness-frontend-artifact-manifest.json` 为准。


2026-09-10 Independent target acceptance checkpoint

The Knowledge target now owns its locked backend dependencies, independent encrypted credential store, runtime staging metadata, CI commands, generated test Catalogs and Console build fixtures. Legacy source observers require an explicit external fixture under backend/tests/migration and are excluded from default traversal. The standalone contract test omitted by the original path filter was restored verbatim (SHA-256 286817441579b7c07f27dc41320bff506aa48350db43dea5a3682b467800eece).

The phase-gate manifest no longer inherits verified claims whose source inventory, Vanna provenance or source shadow artifact is absent in this target. They remain blocked. The existing provider-neutral Phase 0C claims still execute their checks; changed-evidence and failed-check rejection remain tested. This correction does not activate production.

The next runtime gap is explicit: deploy --apply currently stages metadata, while puddingknowledge-local runs only explicit local snapshots. A separately owned local start/status/stop supervisor, actual endpoint health, restart/crash recovery and executable stateful upgrade/rollback remain to implement and verify. Full Processing/Authoring and production authentication are also not yet accepted.

2026-09-10 Runtime composition audit (independent target)

The deploy/install/supervisor slice adds executable local lifecycle, but does not
close full Processing/Authoring. Source inspection identifies these concrete
remaining composition gaps:

- WikiCompilationWorker exists in wiki/compiler.py and admin compile routes
  exist, but local/app.py does not accept or construct wiki_compilation.
- Semantic authoring/decision/build services and routes exist, but local app
  composition has no semantic_authoring, semantic_decisions or semantic_processing.
- CaptureProcessingWorker and ConnectorSyncWorker have transport adapters but
  no independent configured source/job/snapshot composition. Deploy CLI
  import/export/index still stage operations instead of invoking workers.

The existing local/structured.py composition is the currently wired durable
Logical Dataset path. Next acceptance must prove actual independently started
workers can import, process, compile, publish and resume after restart using
Platform-owned persistent state, without a legacy worker or source checkout.

Executable local lifecycle checkpoint: the bundle is now copied and verified in
Home-owned content-addressed releases, installed as a locked non-editable wheel
through a disposable build directory, and supervised through authenticated local
control. Health requires the owned runtime's per-run instance identity as well
as child liveness and readiness. Supervisor death closes a lifeline pipe so the
runtime exits; startup failures preserve failed state and reap only their own
Popen handle. Home ancestors, product owner metadata, staged semantics, temporary
publication cleanup, and duplicate operations have explicit rejection tests.

Validation: Deploy CLI 39 passed; supervisor 12 passed including actual external
HTTP collision, concurrent start, manager SIGKILL and same-port restart; existing
local runtime/structured/staging/distribution regressions 20 passed. The full
stage/deploy/install/start/health/stop/restart smoke passed after deleting the
source bundle, with original Catalog preserved and no legacy Home writes.
Production activation and the Processing/Authoring composition gaps above remain
unaccepted. CI now contains the executable lifecycle smoke; remote CI itself has
not been run.

2026-09-10 独立持久 Wiki Processing：新增可选 state-dir，首次复制 Catalog/Wiki，后续复用 owned Catalog；接入显式 HTTP model 的 WikiCompilationWorker 与即时 Asset read/Wiki query。请求指纹、发布正文、receipt 与输出 Catalog Asset 事务提交；同键换输入拒绝，取消/进程退出释放 flock 后可重试。真实 HTTP 模型 fixture + 两次独立服务进程验证仅生成一次，仓库外已安装包同样通过；Deploy CLI stage/deploy/install/start/stop/restart 验证持久 Catalog 修改保留。该切片不能替代尚未接入的 Capture/Connector Sync/Semantic、完整 import/export/index 和状态升级回滚验收。

2026-09-10 URL Capture / Read Later：独立服务现支持 URL 抓取、列表、失败重试、资产读取和 Wiki promotion。抓取进程有 60 秒总期限与解压后 5 MiB 边界；公共地址策略逐跳校验并固定 DNS 结果，测试私有 origin 仅由本地配置授权。原始内容和规范化 Markdown 先写入不可变对象存储，再以所有权条件更新持久任务和 Catalog；加密 URL、双重文件锁、对象目录 FD 和存储身份绑定覆盖重试及目录替换边界。迁入纯 Feishu blocks 转换器，尚不代表完整 Feishu Sync。

验证：最终相关回归 56 passed；兼容回归 28 passed；Deploy CLI 39 passed；仓库外已安装包真实 Capture 进程 1 passed。完整发行包 stage/deploy/install/start/capture/read/stop/restart/replay 通过，原 Catalog 不变，重放没有第二次抓取。运行包 manifest 为 sha256:2ff4a8f7fccd941b294079a2963e895e31b54819edc493d09c6171dcfdcecc6c。对抗式审查问题已修复并加入反例测试；远程 CI 未执行。剩余包括图片二进制缓存、阅读状态与删除、完整 Feishu 授权/发现/增量同步、Semantic 模型处理、import/export/index、升级回滚和生产连续性验收；整体拆分目标仍未完成。

2026-09-10 Feishu 独立 Docx 同步阶段：新增真实 OpenAPI provider、应用 tenant token broker（Vault 引用、缓存、认证失败单次刷新）、受限 Wiki/Drive/Bitable discovery、Docx 固定版本原始快照和 Markdown Asset 发布。独立服务增加 feishu-config 和 Admin source discover/sync；监督器与 Deploy CLI 透传配置，读取和 Wiki source provider 接入当前有效 Feishu 资产。先认领再创建绑定来源，逐次写入校验 owner/config fingerprint；全量成功后才允许 tombstone，增量缺失不删除，selection 改动要求显式迁移。同键成功重放无需远端请求。分页 malformed/循环/重定向/单页及累计体积超限均失败关闭。

验证：相关运行时与竞态回归 56 passed；额外分页累计边界与真实 HTTP 测试通过；发行边界/旧 Connector 兼容 17 passed，Deploy CLI 39 passed。仓库外锁定非 editable 安装包真实进程通过应用凭据兑换、同步、读取、无环境密钥重启、增量快路径；原 Catalog 未改变。Luna 对抗审查发现的 credential/claim 竞态、旧 Asset 指针读取、selection 漂移、异常分页误判空集合均已修复并覆盖反例。远程 CI 未执行。本阶段仍不等于完整 Feishu：用户 OAuth、Lark、媒体/PDF/Drive 解析、完整 Bitable schema/关系/live row、逐项失败隔离、断点分页、索引和 Console 仍待接入，Semantic、import/export/index 及升级回滚/生产连续性也仍是整体目标缺口。

2026-09-10 Feishu 用户 OAuth：独立服务新增显式 auth_type=user、配置回调 allowlist 和 scope、一次性 PKCE 授权/回调、持久用户 grant、版本化刷新和明确的本地授权撤销。State hash/会话归属/redirect digest 在 Catalog，verifier 与全部token在自有Vault；网络前持久化 exchanging/refreshing，远端交换期间不持有数据库事务。真实进程在 callback/refresh 中 SIGKILL 后分别拒绝code重放和旧refresh重试；未知旋转结果明确 needs_reauth。撤销递增授权generation，抢占旧callback/refresh/sync提交。user与tenant必须显式配置，不能静默切换。

验证：最终相关回归 64 passed；安装包在仓库外真实进程 3 passed，覆盖授权、401后单次刷新、无环境密钥重启、撤销后拒绝访问、callback/refresh SIGKILL。发行边界/打包验证通过；Luna对抗审查反例覆盖wrong principal、过期state、redirect限制、scope缺失、并发force refresh和撤销抢占。此前HTTP fixture未读取POST body导致TCP reset，修正fixture并补安全传输异常包装后回归通过。远程CI和真实生产飞书授权尚未执行。本地撤销明确 provider_revocation=false，不宣称远端同意已撤销；旧加密token留存待GC。整体缺口仍包括生产会话认证、远端撤销、Lark/附件/Drive/Bitable完整处理、索引和Console、Semantic与import/export/index、升级回滚及生产连续性。

### Feishu media / local PDF derivative closure (2026-09-10)

- Added authenticated, bounded Docx media and Drive text/PDF downloads, immutable
  parent-owned media publication, distinct original/normalized/image Assets, and
  dynamic authorized derivative reads. Parent replacement/deletion and connector
  disable invalidate stale reads; failed parsing keeps the old publication.
- MinerU is an explicit Knowledge-owned loopback HTTP adapter, with cancellable
  whole-request timeouts, bounded ZIP/JSON output, safe paths and AST-validated
  image rewrites. No Claw global config, shared output scan, or scratch cleanup.
- Fixed actual integration failures: per-file versus combined budget mismatch,
  OAuth transport compatibility, Bitable method preservation, same-name media
  references, incomplete HTTP bodies, parse output identity drift, and MCP
  derivative URI Space mismatch.
- Related backend regression: 171 passed. Non-editable installed package tested
  from outside the checkout: 14 passed, including real HTTP/standalone process
  media parsing, image/derivative REST+MCP reads, restart, Bitable and OAuth.
- Remaining: general/cloud ParserRegistry and remote task recovery, Office input,
  full indexing/semantic lifecycle, production authentication and stateful release
  gates. This does not complete the full repository separation goal or activate
  production. See persistent-local-runtime.md for explicit supported scope.

### Persistent file ingestion / local full-text index closure (2026-09-10)

- Reused the Admin `assets:upload` contract with explicit host bindings and
  persistent SQLite ingestion jobs. Full request/principal/config fingerprints,
  anchored file reads, per-job/Asset locks, and lease fencing protect publication.
- Added configured async ParserRegistry, native UTF-8/CSV/TSV/DOCX processing
  including internal DOCX images, and the existing explicit local MinerU route.
- Original/normalized/image Assets, derivative mappings, versioned Collection
  provider binding and FTS5 chunks activate in one Catalog transaction. File
  retrieval now uses an actual document provider rather than Wiki-only search.
- Real process tests prove REST/MCP/unified query, source replacement and stale
  read/index revocation, restart replay, and SIGKILL during parsing with a fenced
  second attempt. Missing index rows fail closed instead of reporting no matches.
- Final related backend regression: 197 passed. Source-external non-editable
  installation: 15 passed. Deploy CLI: 40 passed. Actual stage/deploy/install/
  supervisor/start/import/query/stop/restart/replay succeeds without altering seed
  Catalog; the job stays at attempt 1 during successful replay.
- Remaining: cloud/general parser breadth and remote task checkpoints, package
  import/export, vector indexing/reranking, object GC and production identity,
  stateful upgrade/rollback. The full two-repository objective stays incomplete.
