# AIKnowledge 项目进展

最后更新：2026-08-06<br>
当前阶段：Phase 1B 团队安全试点（OIDC/RLS/audit 与 security admin 已通过共享 CI，GitHub ACL planner 待共享 CI）<br>
当前结论：跨仓 solution snapshot 和只读 MCP 已形成端到端链路；OIDC/RLS/audit 已在共享 CI 通过 57/57，见 [run 31049508639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31049508639)；原子 security manifest、显式 membership/grant/token revoke 随后通过 61/61，见 [run 31050256639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31050256639)。GitHub effective collaborator 只读抓取、numeric user ID binding 和 direct grant 差异计划已完成本地实现；在 grant provenance 建立前，撤权只列候选、不自动执行。

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
| 3 | Phase 0C 跨仓 PoC | 让一个问题可以在一致版本下检索多个仓库 | solution snapshot、仓库路由、跨仓关系、跨仓引用 | 至少 10 个跨仓问题通过评测 | 完成：10 题指标 1.0，共享 CI 43/43 通过 |
| 4 | Phase 1A 只读 MCP | 把知识检索能力提供给现有 AI 客户端 | `/mcp/read`、scope/context tools、Cursor 和 Claude Code 接入 | 两类客户端可以稳定取得相同 Context Pack | 本地工程与 Claude Code 完成，Cursor UI 待验收 |
| 5 | Phase 1B 团队安全试点 | 支持多人安全共享 | OIDC、仓库 ACL、PostgreSQL RLS、数据出域策略、审计、增量索引 | 越权测试零泄露，索引可增量更新 | 进行中：认证/admin CI 通过，ACL planner/真实联调待完成 |
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
- 语义里程碑当时本地运行 38 项测试：33 项通过、5 项环境集成测试跳过；semantic provider/cache、证据 hash 修复和既有 PostgreSQL/pgvector + Zoekt 集成已在 [`CI 31041308974`](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31041308974) 全部通过。

### 2.4 Phase 0C 已完成的工程工作项

- SQLite catalog 升级到 schema v6，新增 `solution`、`solution_snapshot`、`solution_snapshot_member` 和状态事件；完整 canonical manifest JSON 与 SHA-256 digest 一起保存。
- 已实现 JSON manifest v1、稳定 ID、成员 repository/snapshot/state 校验，以及 `building -> validated -> active -> superseded` 原子发布；重复发布幂等，历史版本可重新激活。
- resolver 始终返回 manifest 固定的精确 repository snapshots；测试证明成员仓产生新 active snapshot 后不会混入旧 solution。
- 已实现 2～4 仓高召回路由基线：先解析 manifest/可见成员，再对每个固定 snapshot 独立运行 hybrid retrieval，最后按 repository rank 使用 `k=60` 的 RRF 公平合并。
- Context Pack 升级到 v1.3：增加 solution scope、member role/ordinal、`solution_rrf`、routing trace、跨仓 symbol links 和真实 `partial_visibility`。
- 查询命中的源码调用候选可与另一可见成员仓中的定义匹配，生成带两端 repository/revision/path/lines citation 的 `source_inferred` link，不伪装成编译器确认关系。
- 已实现 PoC repository allow-set。隐藏成员不进入检索、evidence、symbols、cross-repository links 或 trace；输出只标记通用 `partial_visibility`，不列出隐藏名称、snapshot 或数量。
- PostgreSQL 增加独立 schema v3 migration，历史 v1 metadata 不受污染；`PostgresSolutionPublisher` 使用事务 advisory lock 原子发布版本组合，PostgreSQL resolver 可直接驱动跨仓 Context Pack。
- 双仓自动 fixture 的 10 个跨仓问题 routing recall 为 `1.0`，全部 evidence 的 version combination accuracy 为 `1.0`。
- PostgreSQL/pgvector/Zoekt 真实共享 CI 已完成 migration、双仓 snapshot/solution 发布、Context Pack 与 partial visibility 验证，43/43 测试通过，见 [run 31043795023](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31043795023)。

### 2.5 Phase 1A 已完成的本地工程工作项

- 经官方 GitHub release 与 PyPI 双重核对，MCP Python SDK `2.0.0` 已于 2026-07-28 GA；已接受 [ADR-0003](decisions/0003-mcp-v2-read-only.md)，替换旧设计中的 v1.28/等待 GA 假设。
- 新增 `mcp` optional dependency，不影响默认 source scanner、SQLite 或评测安装。
- 已实现 `aikb_scope_resolve`、`aikb_context_search`、`aikb_context_get` 三个工具，全部声明 read-only/non-destructive/idempotent/closed-world；没有写入、shell、任意 SQL、sampling 或 elicitation 工具。
- repository 与 solution 共用现有 resolver 和 Context Pack v1.3；MCP adapter 不复制检索逻辑，不执行目标仓库脚本，不编译源码。
- MCP input schema 限制问题长度、evidence/token/symbol/relation 预算；`context_get` 只接受 catalog 内 repository-relative 路径并拒绝绝对路径与 `..`。
- 已实现 stdio 与 stateless Streamable HTTP `/mcp/read`；HTTP 请求体上限 1 MiB，Phase 1A 未认证服务拒绝绑定非 loopback 地址。
- 协议测试覆盖严格 structured output、PoC partial visibility、隐藏仓库零输出、未授权 scope、路径穿越、连续 100 次稳定调用、stdio 子进程和真实 HTTP 调用。
- MCP 以 SQLite `mode=ro&immutable=1` URI 和 `query_only` 打开已冻结 catalog，只校验 schema，不创建数据库、目录、WAL 或 migration；缺失 catalog 的工具调用返回错误且零落盘。本地 MCP 不与 publisher 并发，团队常驻服务将在 Phase 1B 使用 PostgreSQL reader。
- 已提供 Cursor `.cursor/mcp.json`、Claude Code `claude mcp add` 与本机 HTTP 配置；Claude Code `2.1.222` 已在隔离配置中真实启动 `aikb-mcp` 并显示 `Connected`，用户原配置未改动。Cursor CLI 不提供等价健康检查，UI 调用验收待执行。
- 当前本地共发现 50 项测试：44 项通过，6 项依赖 PostgreSQL/Zoekt 集成环境的测试按设计跳过。

Phase 1A 提交 `e475042` 已在共享 PostgreSQL 17 + pgvector + Zoekt 环境通过 50/50 测试，见 [run 31046518729](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31046518729)。

### 2.6 Phase 1B 已完成的工程工作项

- PostgreSQL schema v4 新增 `security_domain`、`principal`、`security_team`、`security_team_member`、`repository_grant`，OIDC issuer + subject 唯一映射 principal。
- repository grant 支持 principal 或 team 二选一、read/write/admin、到期和撤销；暂停 domain/principal/team 均不会通过授权函数。
- 新增 NOLOGIN/NOSUPERUSER/NOBYPASSRLS `aikb_reader`。远程读取事务必须 `SET LOCAL ROLE`，再以事务级 GUC 传入服务端认证后的 principal/domain。
- repository/snapshot、blob/file/chunk、symbol/relation、embedding、solution/member 和 retrieval trace 已启用面向 reader 的 RLS；reader 不可直接读取 principal/team/grant 表。
- `PostgresCatalog` 接受 `PostgresPrincipalContext` 后对每次查询强制 reader role 和 principal/domain，应用查询即使遗漏仓库条件也由数据库先过滤。
- PostgreSQL solution resolver 使用同一安全事务，只返回可见 member；通过不可变 `member_count` 判断 partial visibility，不输出隐藏名称或数量。
- 已编写真实非 owner 集成用例：Alice/team 只能读取 visible repository/chunk/member，Bob/direct grant 只能读取 hidden 一侧，错误 domain 为零行，trace 只能按当前 principal/domain 写入和读取。
- schema v4、RLS 与双 principal 非 owner 越权测试已在 PostgreSQL 17 + pgvector + Zoekt 完成 51/51 共享 CI，见 [run 31047764753](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31047764753)。
- schema v5 增加 `tokens_valid_after`、NOLOGIN/NOSUPERUSER/NOBYPASSRLS `aikb_authenticator` 与 `mcp_audit_event`；认证角色只有 principal/domain 指定列读取权限。
- 已固定 `PyJWT[crypto]==2.13.0`，只允许显式非对称算法与 access-token type；强制 signature、issuer、精确 audience/resource、scope、sub/client ID、exp/iat/nbf。
- JWT 通过后以 issuer + subject 查询 active principal/domain；token iat 早于 `tokens_valid_after`、身份未知或任一状态暂停时统一拒绝。
- MCP SDK auth middleware 已返回标准 HTTP 401、`WWW-Authenticate resource_metadata` 和 RFC 9728 Protected Resource Metadata；无效 token 不进入 tool。
- 已认证的 PostgreSQL MCP 从服务端 token context 取得 principal/domain，每个查询继续强制 `aikb_reader` RLS；客户端不能传 allow-set。
- 每次 tool success/error 写 `mcp_audit_event`，只保存 request/tool/outcome、query/scope SHA-256、trace ID 和结果计数，不保存 token、问题、仓库名、路径或源码正文；成功调用若无法写审计会 fail closed。
- OIDC/RLS/audit 全链路已在 PostgreSQL 17 + pgvector + Zoekt 完成 57/57 共享 CI，错误 identity/domain/audience 均零泄露，见 [run 31049508639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31049508639)。
- 新增严格 JSON security manifest 与 `kb-security-apply`：domain/principal/team/membership/grant 在一个事务中增量 upsert，未声明对象不删除，principal 不允许跨域或替换 OIDC identity；`--dry-run` 运行相同校验并回滚。
- repository grant 支持 manifest 显式撤销/恢复；`kb-security-revoke-tokens` 单调推进 `tokens_valid_after`。管理员连接与 MCP service login 分离，不向普通用户开放 SQL。
- security admin 已在共享 PostgreSQL 完成 61/61：dry-run 零落库、重复 apply 幂等、显式移出/恢复 membership、grant revoke、token revoke 与 `aikb_reader` RLS 可见性均通过，见 [run 31050256639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31050256639)。
- 新增 GitHub ACL 只读 planner：固定 REST API version、HTTPS/no-redirect、同 origin/endpoint 分页校验；把 effective read/write/admin 映射到已绑定 principal，分离 activate/update、人工 revoke candidate、未绑定 collaborator 和 stale binding，整个 planner 不写数据库。
- 当前本地共发现 65 项测试：56 项通过，9 项依赖 PostgreSQL/Zoekt 集成环境的测试按设计跳过；新增 GitHub ACL planner 真实数据库只读用例等待共享 CI。

## 3. 还未完成的计划

### 3.1 Phase 0A：按用户决定暂时暂停

第一批 10 个问题已经全部保留，证据仍为 `draft`。负样本、证据复核、30～50 题黄金集和正式 baseline 暂不推进；等结构化检索需要验收时恢复，不删除现有问题、来源和报告。

### 3.2 当前优先级：完成 Cursor 验收并进入 Phase 1B

| 优先级 | 未完成事项 | 下一动作 | 完成条件 |
| --- | --- | --- | --- |
| P0 | 完成实际客户端矩阵 | 在本机 Cursor UI 按文档连接 stdio 并调用三个工具；Claude Code 已完成真实健康检查 | Cursor 与 Claude Code 取得相同 solution scope、evidence 与 citation |
| P0 | 完成真实 IdP/客户端矩阵 | 配置试点 OIDC issuer/JWKS/resource，Cursor/Claude 走标准 discovery | 两类客户端以不同 principal 只取得各自授权 Context Pack |
| P0 | 终验 GitHub ACL planner | 在共享 PostgreSQL 验证 effective collaborator 到 direct grant 的只读差异 | 数据库零写入，新增/权限变化/撤权候选/未绑定四类准确分离 |
| P0 | 建立 grant provenance | schema 区分 manifest/manual/GitHub 授权来源和同步 revision | 外部撤权只能影响同一 connector 管理的 grant，不能误伤人工授权 |
| P1 | 修复候选池外检索缺口 | 优先分析 vmalloc/kmalloc 文档、oldconfig 行范围和 module 问题 | 不靠目录硬编码，新增召回或 source-only 规则由证据和回归测试支撑 |

本地扫描和 PostgreSQL 共享存储的首条链路已经打通，但以下仍未完成：

- scanner 当前仍先生成 SQLite snapshot 再显式发布；后台 index orchestrator、队列重试、自动发布和生产部署尚未完成。
- Kconfig/Kbuild 第一版规则只覆盖常见语法，复杂变量展开和跨文件条件仍需扩展。
- C header 声明与 C implementation 定义尚未做受约束的 logical symbol 归并；不能仅按同名强制合并，否则配置互斥实现或多个定义会被错误标成唯一目标。
- Zoekt adapter 和可重复容器流程已完成；团队环境的常驻服务、分片调度、监控与滚动更新尚未部署。
- lexical（Zoekt/FTS）、symbol、relation RRF 已实现；semantic 候选重排只作为离线实验能力保留，vector 全仓召回和团队知识通道尚未接入。
- Context Pack v1.3 已实现跨仓 partial visibility；ACL/RLS/OIDC/audit 已通过共享 CI，真实 IdP 联调、ACL 自动同步、团队知识和 provider tokenizer 尚未接入。

只有完成这些工作并通过 Phase 0B 指标后，才可以称为“成功建立了第一版单仓代码知识库”。

### 3.3 后续阶段

- 跨仓 solution snapshot、路由基线和 PoC 部分可见性已完成；真实团队问题人工验收及大规模选择性路由尚未完成。
- Claude Code 只读 MCP 接入已验证；Cursor UI 与 VS Code/Copilot 兼容验证尚未完成。
- principal/team/grant/RLS、OIDC/JWT、MCP audit 和安全管理 CLI 已实现；GitHub ACL 只读规划完成，真实 IdP、grant provenance/受控 apply、数据出域策略和更广安全测试尚未完成。
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
| 14 | 实现跨仓 solution snapshot | Codex | schema、固定成员版本、resolver、两阶段检索、partial visibility、跨仓 Context Pack | 已完成：10 题 routing recall 与版本准确率均为 1.0，共享 CI 43/43 通过 |
| 15 | 实现只读 MCP MVP | Codex | 固定 SDK、read-only tools、stdio/Streamable HTTP、协议测试 | 本地工程与 Claude Code 真实连接完成；Cursor UI 验收待执行 |
| 16 | 实现团队身份与 ACL 边界 | Codex | principal、repository grant、RLS、审计、OAuth verifier | 已完成工程终验：共享 PostgreSQL CI 57/57；真实 IdP 联调待完成 |
| 17 | 建立安全管理入口 | Codex | 严格 manifest、原子增量 apply/dry-run、membership/grant/token revoke | 已完成：共享 PostgreSQL CI 61/61 通过 |
| 18 | 建立 GitHub ACL 只读规划 | Codex | effective collaborator 分页、numeric ID binding、grant diff、撤权候选 | 本地 65 项发现、56 通过、9 个环境跳过；共享 CI 待完成 |

### 现在立即要做的动作

下一项工作先让共享 PostgreSQL 17 终验 GitHub ACL planner 的零写入与四类差异；通过后为 grant 增加来源/provenance 和同步 revision，再考虑受控 apply。并行准备真实 IdP/Cursor/Claude 配置矩阵。远程部署必须同时具备精确 token audience 与非 owner 数据库角色，不提供降级到静态 header 的开关。

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
- [团队身份与仓库授权管理](security-admin.md)
- [GitHub repository ACL 只读差异计划](github-acl-planning.md)
- [Zoekt source-only 索引与检索适配器](zoekt-adapter.md)
- [Linux 6.18.40 全树 source-only 验证](full-tree-validation.md)
- [结构化检索自动评测报告](../evals/reports/linux-6.18.40-structured.md)
- [Qwen3 语义候选重排消融](semantic-ablation.md)
- [语义候选重排自动报告](../evals/reports/linux-6.18.40-semantic-qwen3-512.md)
- [ADR-0002：保留语义候选重排，暂不启用全仓向量索引](decisions/0002-semantic-candidate-reranking.md)
- [跨仓 solution snapshot、检索与部分可见性](cross-repo-solution.md)
- [只读 MCP 服务与客户端配置](mcp-read-server.md)
- [ADR-0003：MCP v2 只读接入](decisions/0003-mcp-v2-read-only.md)
- [团队身份、仓库授权与 RLS](team-security.md)
- [ADR-0004：principal、团队授权与 PostgreSQL RLS](decisions/0004-principal-acl-rls.md)
- [远程 MCP：OIDC、RLS 与审计](remote-mcp-auth.md)
- [ADR-0005：远程 MCP 作为 OIDC Resource Server](decisions/0005-oidc-resource-server.md)
