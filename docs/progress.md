# AIKnowledge 项目进展

最后更新：2026-08-06<br>
当前阶段：Phase 0C 跨仓 solution snapshot（进行中）<br>
当前结论：Phase 0B 的单仓全树扫描、Zoekt/结构化 hybrid、Context Pack 和 Qwen3 有界候选语义消融均已完成。保守语义融合相对 Top-100 hybrid 候选把 File/Range Recall@10 从 `0.7222`/`0.5926` 提高到 `0.7778`/`0.6296`，File MRR 保持 `0.9000`，Range MRR 从 `0.7333` 提高到 `0.8000`；依据 ADR-0002 保留实验接口但暂不启用全量向量索引。现在开始跨仓一致版本检索，人工证据复核继续暂停。

## 维护规则

- 本文档是项目进展的唯一汇总入口；详细设计和任务定义仍以技术蓝图及实施计划为准。
- 每次完成工作项、改变技术决策、获得新评测结果或发现阻塞问题时，都要同步更新本文档。
- 只有满足阶段退出条件后，阶段才标记为“完成”；写了代码但尚未验证，仍标记为“进行中”。
- 已完成事项记录实际产出和验证证据，未完成事项记录下一动作及完成条件。

## 1. 整体的计划

| 顺序 | 阶段 | 目标 | 主要产出 | 完成条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | Phase 0A 评测基线 | 建立可以衡量检索质量的真实问题集和普通文本检索基线 | 固定源码范围、30～50 个真实问题、负样本、ripgrep baseline、Recall/MRR 报告 | 数据经过人工复核，指标可重复运行 | 暂停，保留现有数据 |
| 2 | Phase 0B 结构化检索 | 建立第一版单仓代码知识索引 | 不可变 snapshot、Tree-sitter、source-only 关系索引、Zoekt、pgvector、混合检索、Context Pack | Evidence Recall@10、版本准确率和负样本指标达到门槛 | 工程实现完成，正式验收待人工复核 |
| 3 | Phase 0C 跨仓 PoC | 让一个问题可以在一致版本下检索多个仓库 | solution snapshot、仓库路由、跨仓关系、跨仓引用 | 至少 10 个跨仓问题通过评测 | 进行中 |
| 4 | Phase 1A 只读 MCP | 把知识检索能力提供给现有 AI 客户端 | `/mcp/read`、scope/context tools、Cursor 和 Claude Code 接入 | 两类客户端可以稳定取得相同 Context Pack | 未开始 |
| 5 | Phase 1B 团队安全试点 | 支持多人安全共享 | OIDC、仓库 ACL、PostgreSQL RLS、数据出域策略、审计、增量索引 | 越权测试零泄露，索引可增量更新 | 未开始 |
| 6 | Phase 1C 知识进化闭环 | 让团队提问和反馈转化为受治理的共享知识 | feedback、gap、claim、review、publish、最小管理 UI | 至少一条真实知识完成完整审核发布流程 | 未开始 |
| 7 | Phase 2 及以后 | 支持开发态代码、更多知识源和规模化部署 | workspace overlay、ADR/Issue/PR connector、更多语言、分片和高可用 | 根据试点指标逐项决定 | 未开始 |

整体依赖顺序：

```text
评测基线
  -> 单仓结构化知识索引
  -> 跨仓一致版本检索
  -> MCP 接入
  -> 多人权限与增量更新
  -> 知识进化闭环
```

## 2. 完成的计划

### 2.1 项目设计与仓库

- 已创建 GitHub 项目和 `main` 分支。
- 已完成项目总体设计、技术蓝图和 12 周实施计划。
- 已确定首版采用模块化单体，不在初期拆微服务。
- 已确定核心技术路线：PostgreSQL/pgvector、Tree-sitter、无需编译的源码关系提取、Zoekt、MCP。
- 已接受 ADR-0001：正式索引只扫描源码，禁止执行编译，禁止依赖 `.config`、`compile_commands.json` 和 scip-clang；准确性通过条件建模、候选关系、置信度和多路证据解决。
- 已定义跨仓模型：`solution`、`solution_snapshot` 和 `solution_member`。
- 已定义知识治理原则：AI 生成内容必须经过证据校验和人工审核后才能发布。

### 2.2 Phase 0A 已完成的工作项

- 已选择首个实验语料：本地 Linux 6.18.40 源码。
- 已固定发行归档 SHA-256：`3712fc1ec839e4daac981176c8518912e8f452650aaedfe4381da4419613a431`。
- 已确认当前源码树没有 `.git`；`.config`、`compile_commands.json` 和 `Module.symvers` 不再是索引所需输入。
- 已建立评测 scope：`evals/datasets/linux-6.18.40/scope.json`。
- 已定义问题、检索词和源码证据的 JSONL 格式。
- 已实现无第三方运行时依赖的 ripgrep fixed-string baseline CLI。
- 已实现源码版本与归档 SHA-256 校验。
- 已加入 3 道合成冒烟题和 2 个单元测试。
- 已完成冒烟运行：Evidence Recall@10 = `1.0`，MRR = `0.5833`，3 个目标文件均进入前 10。
- 已验证 baseline 的主要缺陷：调用点、测试和文档可能排在函数定义之前，为后续结构化检索提供了可比较基线。
- 已从 Stack Overflow、Unix & Linux Stack Exchange 和 syzbot 整理 56 条带原始链接的真实公开候选题；这些题尚待用户筛选和证据标注，不计入已冻结黄金集。
- 已从候选集中初选 10 个覆盖宏、内存、构建、用户指针、驱动、模块和调度的问题，形成 `questions.first-batch.jsonl`。
- 用户已确认第一批 10 个问题全部保留，`review_status` 已统一更新为 `accepted`。
- 已初步标注 27 处 Linux 6.18.40 源码范围，共 18 个唯一证据文件；路径和行号自动校验结果为 0 个缺失、0 个越界。
- 已在包含 `scripts/` 的固定范围重跑第一批 ripgrep baseline：Evidence Recall@10 = `0.5000`（9/18），MRR = `0.3583`；3 题全部找回、3 题部分找回、4 题未找回。
- 已生成第一批评测报告，并确认高频调用点淹没定义、核心实现和权威文档是主要失败类型。

### 2.3 Phase 0B 已完成的工作项

- 已实现 SQLite 本地 bootstrap catalog；它用于低基础设施实验，最终共享主库仍采用 PostgreSQL/pgvector。
- 已建立 `repository`、`snapshot`、`snapshot_event`、`blob`、`source_file`、`chunk` 和 `chunk_fts` schema。
- 已实现由 revision、源码摘要、实际 manifest 和索引配置共同确定的不可变 snapshot ID。
- 已实现 SHA-256 blob 去重、zlib 内容保存、文件语言识别、大小/二进制过滤和确定性行窗口切块。
- 已实现 `building -> validated -> active -> superseded` 状态与每仓唯一 active snapshot 约束。
- 已实现 `kb-ingest`、`kb-stats`、`kb-search` CLI，搜索结果带 repository、revision、路径和行范围。
- 已接入 `tree-sitter==0.25.2` 和 `tree-sitter-c==0.24.2`，提取函数、声明、类型、宏和顶层调用，并从宏导致的 `ERROR` 恢复节点继续抽取。
- 已记录依赖兼容性：0.26.0 Python binding 在当前 Windows 环境解析宏密集内核文件时发生原生异常，暂时固定 0.25.2 并以真实源码回归作为升级门槛。
- 已扩展 schema v2：chunk 保存 `kind`、`symbol`、`generator`，file 保存解析状态，snapshot 保存结构化/fallback 数量和文件级解析异常数。
- 已对真实 Linux 6.18.40 `kernel/sched` 完成结构化端到端验证：46 个文件、46 个唯一 blob、3,690 个 chunk、1,685,023 字节。
- 其中 3,687 个是 Tree-sitter 结构化 chunk，3 个是 fallback；32 个宏密集文件带解析异常标记但仍提取到结构。
- 已验证相同输入重复索引返回同一 snapshot 且 `idempotent=true`。
- 已验证搜索 `init_idle do_idle` 能返回 `idle.c` 与 `core.c` 的版本化引用。
- 已验证 `init_idle` 直接返回 `function` chunk、符号名和 `core.c:7961-8027` 引用。
- 自动化测试覆盖 snapshot 幂等、源码变化后新建 snapshot、旧 snapshot superseded、检索引用、C 函数/宏/类型抽取和非 C fallback。
- 已接受并实现 source-only 强制策略：scope 声明执行构建或依赖构建产物时，ingest 直接拒绝。
- 已升级 catalog schema v5：v3 新增 `logical_symbol`、`symbol_occurrence`、`source_condition` 和 `relation`；v4 新增按 blob 复用的 `analysis_artifact` 与 snapshot 缓存命中统计；v5 新增 seed/dependency 文件数、未解析/歧义引用数和预算截断状态，并支持旧数据库逐版原位迁移。
- 已实现 `source-relations-v2`：直接扫描 C 定义/声明/调用、include、预处理条件、Kconfig 依赖与 Kbuild 目标关系，不调用编译器或构建脚本。
- 已实现关系置信度：`source_exact`、`source_inferred`、`ambiguous_candidate`、`human_verified`；同文件静态符号优先绑定，局部符号按所属函数隔离。
- 已增加 `kb-symbol` CLI，可查询符号 occurrence、出边、入边、条件、置信度和版本化 citation。
- 已完成真实 Linux 6.18.40 `kernel/sched` source-only 验证：6,111 个 occurrence、5,285 个 logical symbol、11,758 条关系和 577 个条件范围。
- 关系分布为 11,380 条调用、372 条 include、6 条 Kbuild；置信度分布为 104 条 `source_exact`、4,755 条 `source_inferred`、6,899 条 `ambiguous_candidate`。
- 已实现 `(blob_sha, language, analysis_profile_digest)` 分析缓存；压缩保存 chunk、occurrence、condition 与 relation 原始产物，路径和 snapshot 关系仍按版本重新物化。
- 已实现有界依赖扩展：从种子文件的 C include、Kconfig source 和 Kbuild 文件引用出发，按深度、总文件数和单引用候选数预算加入依赖；不读取构建产物、不执行仓库脚本。
- `kernel/sched` 真实扩展结果：46 个种子文件增加 172 个直接依赖文件，共 218 个文件、212 个唯一 blob、13,486 个 chunk、15,960 个 occurrence、16,532 条关系和 2,206 个条件范围；另记录 5 个未解析和 9 个歧义依赖引用。
- 对同一批 `kernel/sched` 调用关系，目标解析率由 `40.11%` 提升到 `50.94%`，`ambiguous_candidate` 比例由 `60.62%` 降到 `49.78%`，没有把歧义候选伪装成唯一精确关系。
- 具体样例：`kb-symbol init_idle` 已同时返回 `include/linux/sched/task.h:64` 的声明和 `kernel/sched/core.c:7969-8027` 的定义；C header 与 C implementation 当前仍保留不同 logical symbol ID，后续必须基于语言族、签名、definition 数量和条件判断后再安全归并。
- 已修正分析缓存边界：依赖扫描预算属于 snapshot index profile，不属于 blob analysis profile。默认扩展首次扫描为 52 hit/166 miss；只把文件预算从 500 改为 499 后为 218 hit/0 miss，证明范围策略变化可以复用全部未变化源码分析。
- 默认配置 active snapshot 为 `snap_0b0e8c0e71ad7f720c31b8e2`；重复扫描返回相同 snapshot 且 `idempotent=true`，已存在的 superseded snapshot 可原子重新激活并保留事件记录。
- 已实现 Context Pack v1.2 Pydantic schema 与 `kb-context-schema`：固定 `urn:aiknowledge:schema:context-pack:v1`，拒绝额外字段，版本字段必须显式存在，并区分实际 lexical provider。
- 已实现 `kb-context`：输出不可变 snapshot、blob/chunk 哈希、稳定 citation、代码证据、symbol/关系候选、预算、coverage、gap 和确定性 retrieval trace；它不调用模型、不生成答案。
- 已实现证据条数和近似 token 预算，逐候选记录 `selected`、`item_budget` 或 `token_budget`；无证据查询返回 `evidence_status=none`。
- 已实现 `retrieval.py` 通道接口和 RRF：`lexical_fts5` 权重 1.0、`symbol_exact` 权重 2.0、`relation_source` 权重 0.75，`k=60`；每条候选保留独立通道 rank 和贡献。
- 已增加 `kb-retrieve` CLI，可独立检查 identifier terms、通道候选数、fused score、贡献和稳定 citation。
- 真实 `init_idle do_idle` 中，`init_idle` 定义由纯 FTS 第 4 提升到混合检索第 1，头文件声明第 2，`do_idle` 定义第 3。
- 已消除 symbol/relation 反查 FTS5 虚表的性能问题：改为从压缩 blob 按 chunk 行范围读取，并增加 chunk/relation 索引；真实混合检索从约 8.3 秒降到约 0.38 秒，完整 Context Pack 约 0.97 秒。
- 真实 FTS 验证生成的 Context Pack v1.1 为 `context_8f20640aa1d2f62501cdb7ec`，trace 为 `trace_5c4ec50ca3e25a95b4b186d6`；v1.2 延续相同 evidence/citation 语义并增加实际 lexical provider；第一条 `core.c:7961-8027` 同时得到三个通道支持。
- 自动化测试覆盖 schema 严格性、确定性、citation 回查、预算截断、unknown/gap、精确符号提升、关系召回和 RRF 稳定性；连同 PostgreSQL 集成测试现为 25 个并在 CI 全部通过。
- 已定义 `ReadCatalog` Protocol，Context Pack/RRF 不再依赖 SQLite 具体类型，为 PostgreSQL/Zoekt adapter 固定边界。
- 已实现 PostgreSQL/pgvector schema v1：15 张业务表覆盖不可变 snapshot、内容寻址 blob、代码结构、embedding model/vector 和最小 retrieval trace；每仓唯一 active snapshot 由 partial unique index 强制。
- 已建立 Alembic `0001_postgres_schema_v1` 和 `postgres` optional dependencies；使用 Psycopg binary 与预装 pgvector 镜像，不在开发机或 CI 编译数据库组件。
- 本地 metadata/离线 DDL 验证通过；本机运行 21 个通过、2 个 PostgreSQL 集成测试按设计跳过。GitHub Actions PostgreSQL 17 + pgvector 完整运行 23/23 通过，包括 Alembic upgrade、extension 检查和唯一 active snapshot 约束；验证 run 为 `30750390588`。
- 已增加 schema v2：`chunk.content` 提供稳定证据读取，PostgreSQL simple-text GIN index 只作为 bootstrap/故障回退，不替代 Zoekt。
- 已实现 `PostgresCatalog` read adapter：覆盖 snapshot resolve、lexical fallback、精确 symbol、relation 和 `find_symbol`，可直接供现有 RRF/Context Pack 使用。
- PostgreSQL Context Pack 集成测试已在 GitHub Actions PostgreSQL 17 上通过；完整结果 24/24，run 为 `30750652975`。
- 已实现 `PostgresSnapshotPublisher` 和 `kb-publish-postgres`：从已验证的 SQLite source-only snapshot 有界批量复制全部索引产物，不会把大型仓库整表载入内存。
- publisher 在单个事务中执行 repository advisory lock、`building -> validated -> active`、派生计数复核和旧 active supersede；重复发布幂等，superseded snapshot 可重新激活，失败整体回滚。
- PostgreSQL publisher/reader 已在真实 PostgreSQL 17 + pgvector CI 完成 25/25 测试；包含 `batch_size=1` 边界、Context Pack、多词 lexical OR、两次版本切换、历史版本恢复和注入失败回滚，run 为 `30751565583`。
- 已实现 `kb-zoekt-export`：只接受已验证 snapshot，逐 blob 校验 zlib、SHA-256、大小和计数，原子生成 `source/`、`manifest.json` 与 `zoekt.meta.json`；相同导出幂等，篡改或不安全路径会被拒绝。
- 已实现 `ZoektClient` 与 `ZoektReadCatalog`：查询限定不可变 snapshot 内部仓库名，开启 BM25，严格拒绝越界 repository/版本，再按文件和行号映射回 SQLite/PostgreSQL 权威 chunk；只有 Zoekt 不可用时回退 FTS，trace 记录真实通道。
- `kb-search`、`kb-retrieve`、`kb-context` 已支持 `AIKB_ZOEKT_URL`、`--zoekt-url` 和验收用 `--zoekt-required`；Context Pack 升级为 v1.2，允许 `lexical_zoekt`、`lexical_fts5` 和 `lexical_postgres_fts`。
- GitHub Actions 使用固定官方预构建 Zoekt 镜像且传入 `-disable_ctags`，从 source-only fixture 索引并启动 JSON API；PostgreSQL 17 + pgvector、SQLite 和 Zoekt 共 29/29 测试通过，run 为 [`30753488893`](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/30753488893)。
- 已完成 Linux 6.18.40 全树冷扫描：70,925 文件、70,848 唯一 blob、3,970,532 chunk、5,125,759 occurrence、5,098,771 relation、126,783 condition，snapshot `snap_a162172858bee6eee189963a` 通过独立计数校验后激活。
- 全树成功运行约 81 分钟，数据库约 12.64 GB，WAL 峰值约 11.92 GB，峰值工作集约 2.59 GB；全过程不编译 Linux、不执行仓库脚本。
- 已把深层 AST walker 改为显式栈，把数百万待绑定关系移到磁盘临时表，并用 basename 索引消除 include 候选的“每条关系 × 全仓路径”扫描。
- 已把 blob/analysis artifact 按 250 文件有界批次持久化；故障注入证明缓存可保留，而半成品 snapshot 不可见。
- 已实现结构化自动评测 runner 和 Markdown 报告：同时计算 file 与 evidence range Recall/MRR，并输出 complete/partial/missed 逐题状态。
- 全树 FTS 指标为 File Recall `0.5556`、Range Recall `0.2593`、File MRR `0.7000`、Range MRR `0.5167`；加入 symbol/relation RRF 后为 `0.7222`、`0.5926`、`0.8500`、`0.6833`，完整题由 0 增至 5。
- 已修复全树关系召回的跨 JOIN OR 全扫，真实关系查询约 0.010～0.022 秒；已修复多行 macro occurrence 无法映射单行 chunk，`likely`、`container_of`、`EXPORT_SYMBOL` 等定义重新进入 definition-first 通道。
- SQLite FTS 全树 10 题运行约 605 秒，确认只适合作为离线 baseline；评测 lexical cache 严格校验问题、查询、Top K 和 snapshot，只重算下游 hybrid 为 0.367 秒，为 vector/reranker 消融提供可重复输入。
- 已实现独立 semantic provider、不可变模型指纹、SQLite 内容寻址 embedding cache、Top-100 候选报告校验、纯语义与 RRF 消融 CLI；模型依赖是可选项，不影响默认索引/检索安装。
- 已固定 `Qwen/Qwen3-Embedding-0.6B@97b0c614…` 和权重 SHA-256，使用 512 维、2048 最大序列、本机 RTX 4060 Ti 完成真实推理。830 个 query/document embedding 冷运行约 31 秒，缓存复跑约 1.3 秒。
- Top-100 candidate pool 覆盖 15/18 文件、20/27 范围；保守 semantic RRF 的 File/Range Recall@10 为 `0.7778`/`0.6296`，File/Range MRR 为 `0.9000`/`0.8000`，相对深候选四项无回退。
- 已完成 [ADR-0002](decisions/0002-semantic-candidate-reranking.md)：保留候选重排 provider/cache，默认 Context Pack 暂不启用 semantic，不生成 397 万个全量 embedding，不建立 ANN，也不增加独立 reranker 模型。
- symbol/relation 从 blob 按完整行恢复证据时，返回的 `content_hash` 现绑定实际 evidence body，不再错误沿用首尾可能位于行内的 Tree-sitter AST 片段 hash。
- 当前本地运行 38 项测试：33 项通过、5 项环境集成测试跳过；semantic provider/cache、证据 hash 修复和既有 PostgreSQL/pgvector + Zoekt 集成已在 [`CI 31041308974`](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31041308974) 全部通过。

## 3. 还未完成的计划

### 3.1 Phase 0A：按用户决定暂时暂停

第一批 10 个问题已经全部保留，证据仍为 `draft`。负样本、证据复核、30～50 题黄金集和正式 baseline 暂不推进；等结构化检索需要验收时恢复，不删除现有问题、来源和报告。

### 3.2 当前优先级：推进 Phase 0C

| 优先级 | 未完成事项 | 下一动作 | 完成条件 |
| --- | --- | --- | --- |
| P0 | 实现跨仓 solution snapshot | 建 solution/member/snapshot schema 和一致版本 resolver | 一个查询 scope 只能解析到 manifest 固定的 repository snapshot 集合，禁止混入 active latest |
| P0 | 实现跨仓检索与 Context Pack | 两阶段 repository routing、分仓召回、统一 RRF 和跨仓 trace | 2～4 仓 fixture 的证据均带正确 repository/revision，顺序确定且可复现 |
| P0 | 验证 partial visibility | 加 repository ACL test double 和隐藏仓库用例 | 不可见仓库的名称、版本、片段、候选数和关系均不泄露 |
| P1 | 修复候选池外检索缺口 | 优先分析 vmalloc/kmalloc 文档、oldconfig 行范围和 module 问题 | 不靠目录硬编码，新增召回或 source-only 规则由证据和回归测试支撑 |

本地扫描和 PostgreSQL 共享存储的首条链路已经打通，但以下仍未完成：

- scanner 当前仍先生成 SQLite snapshot 再显式发布；后台 index orchestrator、队列重试、自动发布和生产部署尚未完成。
- Kconfig/Kbuild 第一版规则只覆盖常见语法，复杂变量展开和跨文件条件仍需扩展。
- C header 声明与 C implementation 定义尚未做受约束的 logical symbol 归并；不能仅按同名强制合并，否则配置互斥实现或多个定义会被错误标成唯一目标。
- Zoekt adapter 和可重复容器流程已完成；团队环境的常驻服务、分片调度、监控与滚动更新尚未部署。
- lexical（Zoekt/FTS）、symbol、relation RRF 已实现；semantic 候选重排只作为离线实验能力保留，vector 全仓召回和团队知识通道尚未接入。
- Context Pack v1 已实现，但团队知识、ACL partial visibility 和 provider tokenizer 尚未接入。

只有完成这些工作并通过 Phase 0B 指标后，才可以称为“成功建立了第一版单仓代码知识库”。

### 3.3 后续阶段

- 跨仓 solution snapshot、两阶段仓库路由和部分可见性正在实现。
- Cursor、Claude Code 等客户端的只读 MCP 接入尚未实现。
- OIDC、ACL、RLS、数据出域策略、审计和安全测试尚未实现。
- feedback、gap、claim、review、publish 知识进化闭环尚未实现。
- Web 管理控制台、workspace overlay 和更多知识源 connector 尚未实现。

## 4. 下一步要做的事情

单仓 Phase 0B 工程路径已经验证，人工证据复核仍暂停；按照以下顺序继续推进：

| 顺序 | 要做的事情 | 负责人 | 具体产出 | 完成判断 |
| --- | --- | --- | --- | --- |
| 1 | 完成本地知识目录骨架 | Codex | SQLite catalog、不可变 snapshot、ingest/search CLI | 已完成，真实 `kernel/sched` 验证通过 |
| 2 | 接入 Tree-sitter C | Codex | AST parser、结构化 chunk、解析覆盖率 | 已完成，`kernel/sched` 结构化验证通过 |
| 3 | 建立 source-only 关系模型 | Codex | occurrence、logical symbol、relation、condition、confidence schema | 已完成，关系模型及当前 catalog schema v5 真实扫描通过 |
| 4 | 直接扫描 Linux 源码关系 | Codex | 定义/声明、include、Kconfig/Kbuild、调用候选提取器 | 已完成第一版，`kernel/sched` 可重复生成相同关系 |
| 5 | 实现 blob 级增量分析复用 | Codex | analysis artifact cache、命中/未命中统计 | 已完成，真实冷/热缓存对照通过 |
| 6 | 实现依赖引导的范围扩展 | Codex | 有界依赖发现、解析统计、snapshot profile | 已完成，46 个种子扩展到 218 个文件且歧义率下降 |
| 7 | 输出 Context Pack v1 | Codex | evidence、citation、snapshot、trace、预算 | 已完成，真实 Linux 快照和 unknown 查询验证通过 |
| 8 | 建立 retriever 接口与本地 lexical/symbol/relation RRF | Codex | 通道契约、统一候选、RRF、分通道 trace | 已完成，`init_idle` 定义由第 4 提升到第 1 |
| 9 | 建立 PostgreSQL/pgvector schema 与 migration | Codex | provider boundary、15 表 metadata、Alembic、CI service | 已完成，GitHub Actions PostgreSQL 17 上 23/23 测试通过 |
| 10 | 实现 PostgreSQL read/write adapter | Codex | Context Pack read、分批写入、计数校验、原子发布、回滚 | 已完成，GitHub Actions PostgreSQL 17 上 25/25 测试通过 |
| 11 | 接入 Zoekt 正式 lexical adapter | Codex | immutable export、预构建 Zoekt index job、provider adapter、FTS fallback、live CI | 已完成，29/29 测试通过，run `30753488893` |
| 12 | 恢复评测问题集 | Codex | 自动评测 runner、FTS/RRF 对比、失败分类；人工复核继续暂停 | 已完成：hybrid Range Recall@10 `0.5926`、Range MRR `0.6833`，形成 vector 决策输入 |
| 13 | 实验 vector/reranker | Codex | provider 边界、可缓存 embedding/score、与当前 hybrid 的消融报告 | 已完成：候选融合四项无回退；保留实验接口，按 ADR-0002 暂缓全量向量索引 |
| 14 | 实现跨仓 solution snapshot | Codex | schema、固定成员版本、resolver、两阶段检索、partial visibility、跨仓 Context Pack | 2～4 仓 fixture 上版本组合准确、无越权泄露、结果可复现 |

### 现在立即要做的动作

下一项开发工作是跨仓 solution snapshot：先扩展领域 schema，显式保存 solution、不可变 solution snapshot 和 repository snapshot member；再让 scope resolver 只返回 manifest 固定版本，随后实现两阶段仓库路由、统一检索和 partial visibility。首个自动 fixture 至少包含两个仓库，并验证隐藏其中一个仓库时不会泄露名称、版本、候选统计或正文。全过程继续遵守 source-only，不编译任何目标仓库。

## 相关文档

- [项目总体设计](architecture.md)
- [技术蓝图](technical-blueprint.md)
- [具体实施计划](implementation-plan.md)
- [Linux 6.18.40 Phase 0A 实验](../evals/datasets/linux-6.18.40/README.md)
- [第一批真实问题基线报告](../evals/reports/linux-6.18.40-first-batch.md)
- [Phase 0B 本地知识库启动骨架](phase-0b-bootstrap.md)
- [Context Pack v1](context-pack-v1.md)
- [本地混合检索 v1](hybrid-retrieval.md)
- [PostgreSQL/pgvector schema v1](postgres-schema-v1.md)
- [Zoekt source-only 索引与检索适配器](zoekt-adapter.md)
- [Linux 6.18.40 全树 source-only 验证](full-tree-validation.md)
- [结构化检索自动评测报告](../evals/reports/linux-6.18.40-structured.md)
- [Qwen3 语义候选重排消融](semantic-ablation.md)
- [语义候选重排自动报告](../evals/reports/linux-6.18.40-semantic-qwen3-512.md)
- [ADR-0002：保留语义候选重排，暂不启用全仓向量索引](decisions/0002-semantic-candidate-reranking.md)
