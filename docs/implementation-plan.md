# AIKnowledge 具体实施计划

状态：Draft v0.2<br>
更新日期：2026-07-28<br>
对应设计：[项目总体设计](architecture.md)
技术实现映射：[技术蓝图](technical-blueprint.md)

## 1. 计划目标与假设

本计划的目标不是在第一天搭建完整知识库平台，而是用可测量的阶段逐步验证四个核心假设：

1. 结构化混合检索比纯全文或纯向量更容易找对代码证据。
2. `solution snapshot` 能让跨仓回答保持版本一致。
3. 只读远程 MCP 能被团队现有 AI 客户端稳定调用。
4. feedback -> gap -> claim -> review -> publish 能形成可信的知识进化闭环。

排期按以下资源假设估算：

- 1 名全职后端/AI 工程师。
- 1 名兼职领域工程师负责问题标注和知识审核。
- 前端、DevOps、安全各有少量兼职支持。
- 先部署在一台 Linux 开发服务器上，使用 Docker Compose。
- 首批代码以 C/C++ 或内核类仓库为主。

在上述条件下，完成团队试点预计需要 12 周。资源减少时保持阶段顺序不变，延长周期；资源增加时可以并行 UI、认证和安全测试，但不能跳过评测基线。

## 2. 里程碑总览

| 时间 | 里程碑 | 核心产出 | 退出条件 |
| --- | --- | --- | --- |
| 第 1 周 | Phase 0A 评测基线 | 黄金问题集、lexical baseline | 指标可自动复现 |
| 第 2～4 周 | Phase 0B 结构化检索 | Tree-sitter/source-only 关系/向量、Context Pack | Recall 与版本指标达标 |
| 第 5～6 周 | Phase 0C 跨仓 PoC | solution snapshot、跨仓路由 | 10+ 跨仓问题通过评测 |
| 第 7～8 周 | Phase 1A 只读 MCP | Cursor/Claude 可用的 read tools | 两种客户端稳定调用 |
| 第 9～10 周 | Phase 1B 安全试点 | OIDC、ACL、RLS、数据策略、增量索引 | 越权测试零泄露 |
| 第 11～12 周 | Phase 1C 知识闭环 | feedback/gap/claim/review、最小 UI | 一条真实知识完成全流程 |

每个阶段只有在退出条件满足后才能进入下一阶段。未达标时优先修正检索、数据和评测，不用增加更多模型或基础设施掩盖问题。

### 2.1 架构组件—任务追踪矩阵

| 蓝图组件 | 自研工作 | 直接复用 | 主要工作项 | 首次可用阶段 |
| --- | --- | --- | --- | --- |
| C1 Repository & Snapshot | 仓库注册、不可变 snapshot、solution 版本 | Git CLI | GIT-001/002、SOL-001/002 | Phase 0A/0C |
| C2 Index Orchestrator | 幂等步骤、outbox、发布状态机 | Dramatiq、Redis、Docker | OPS-001、IDX-001、SEC-003 | Phase 0B |
| C3 Code Intelligence | occurrence、条件图、logical symbol、citation、调用候选 | Tree-sitter | IDX-002～005、DATA-002/003 | Phase 0B |
| C4 Lexical Search | ACL/scope adapter、结果标准化 | ripgrep、Zoekt | RET-001、RET-003、RET-006 | Phase 0A/0B |
| C5 Semantic Index | embedding pipeline、模型版本和安全域 | Qwen3 Embedding/Reranker、pgvector | RET-003/004、EVAL-006 | Phase 0B |
| C6 Metadata/Knowledge Store | 领域 schema、状态机、provenance、RLS | PostgreSQL、SQLAlchemy、Alembic | DATA-001～004、KB-001、AUTH-004 | Phase 0B/1C |
| C7 Hybrid Retrieval | scope resolver、四路召回、RRF、预算 | Zoekt/pgvector adapters | RET-003～006、SOL-004/005 | Phase 0B/0C |
| C8 Context/Answer | Context Pack、provider boundary | Pydantic、FastAPI | CTX-001、ANS-001 | Phase 0B/1C |
| C9 MCP Gateway | tool 语义、read/write 隔离、客户端策略 | MCP Python SDK | MCP-001～005、INT-001/002 | Phase 1A |
| C10 Identity/Policy | 权限映射、数据出域、缓存隔离 | OIDC/Keycloak、PostgreSQL RLS | AUTH-002～004、POL-001、SEC-001～004 | Phase 1B |
| C11 Knowledge Evolution | feedback/gap/claim/review/re-anchor | 不依赖完整外部产品 | KB-001～005、WEB-001 | Phase 1C |
| C12 Observability/Eval | 黄金集、指标和回归框架 | pytest、OpenTelemetry、Prometheus | EVAL-001～009、OBS-001 | Phase 0A 起 |

这张表是排期入口。新增技术或工作项时必须先说明它属于哪个组件、替代什么、改善哪个验收指标；无法建立映射的组件不进入 MVP。

## 3. Phase 0A：评测基线（第 1 周）

### 工作项

| ID | 工作 | 产出 |
| --- | --- | --- |
| EVAL-001 | 选择首个目标仓库和固定 commit/归档摘要 | `evals/datasets/v1/scope.yaml` |
| EVAL-002 | 收集 30～50 个历史真实问题 | 原始问题清单及问题来源 |
| EVAL-003 | 标注关键仓库、文件、符号、代码范围和答案类型 | `questions.jsonl` |
| EVAL-004 | 加入至少 20% 负样本/证据不足问题 | unknown 样本集合 |
| EVAL-005 | 定义评测 schema、指标和命令行入口 | `aikb eval run` 设计 |
| RET-001 | 用 ripgrep/简单全文搜索实现 lexical baseline | baseline 检索结果 |
| RET-002 | 生成 Recall@K、MRR、版本准确率和延迟报告 | `baseline-report.md` |

### 问题集最低覆盖

- 精确符号定义与引用。
- caller/callee 或回调链路。
- 宏、Kconfig、Makefile 和条件表达式差异。
- “为什么这样设计”的方案问题。
- 跨目录模块关系。
- 仓库中没有答案的负样本。
- 对错误版本提问的版本陷阱样本。

### 验收

- 至少 30 个问题，其中至少 6 个负样本。
- 每个正样本至少有一个人工确认的关键证据位置。
- 相同命令重复执行得到相同 baseline 结果。
- 领域工程师抽查通过率达到 100%，存在争议的题目不进入首版黄金集。

## 4. Phase 0B：结构化检索 PoC（第 2～4 周）

### 第 2 周：数据与索引骨架

| ID | 工作 | 产出 |
| --- | --- | --- |
| DATA-001 | 建立 repository、snapshot、blob、file、chunk schema | Alembic migration |
| DATA-002 | 建立 symbol occurrence、logical symbol、relation schema | 版本化代码模型 |
| DATA-003 | 建立 citation、derived artifact、provenance schema | 稳定引用模型 |
| GIT-001 | 实现只读 bare mirror、固定 commit checkout | Git connector |
| IDX-001 | 实现文件过滤、语言识别、内容哈希与去重 | 基础 ingest worker |
| IDX-002 | 接入 Tree-sitter 结构化切块 | AST chunk artifact |
| OPS-001 | 建立 PostgreSQL/pgvector/Redis Docker Compose | 本地可复现环境 |

### 第 3 周：代码智能与检索

| ID | 工作 | 产出 |
| --- | --- | --- |
| IDX-003 | 直接扫描标识符、声明和 occurrence，建立 logical symbol 候选 | source symbol index |
| IDX-004 | 解析 include/import、Kconfig/Kbuild 和预处理条件 | condition/dependency graph |
| IDX-005 | 用 AST + 作用域/签名/注册模式产生带置信度的关系候选 | relation extractor |
| IDX-006 | 从种子范围沿显式源码关系做有深度和文件预算的扩展 | bounded dependency expansion + diagnostics |
| RET-003 | 实现 lexical、vector、symbol 三路召回 | retriever interfaces |
| RET-004 | 实现 RRF 融合、过滤和 token budget | hybrid retrieval |
| RET-005 | 实现稳定 citation 和源片段读取 | evidence service |
| RET-006 | 用 `zoekt-git-index` 和内部 Zoekt API 替换正式 lexical 通道 | Zoekt adapter + index job |

### 第 4 周：Context Pack 与对比评测

| ID | 工作 | 产出 |
| --- | --- | --- |
| CTX-001 | 定义并实现 Context Pack v1 schema | JSON schema + builder |
| TRACE-001 | 记录 query、scope、召回项、分数和耗时 | retrieval trace |
| EVAL-006 | 比较四组检索方案 | 对比报告 |
| EVAL-007 | 执行 citation、unknown 和版本测试 | 质量报告 |
| ADR-001 | 根据数据决定 Qwen3 embedding/reranker 是否达到启用门槛 | 已完成：[ADR-0002](decisions/0002-semantic-candidate-reranking.md)，保留候选重排、暂缓全量向量索引 |

### 验收门槛

- Evidence Recall@10 >= 0.85。
- Version Accuracy = 1.0。
- 负样本错误确定性回答率 < 10%。
- 相对 lexical baseline 的 Recall@10 提升 >= 15%；若 baseline 已很高，改用 MRR 和所需交互轮次证明收益。
- 每条 Context Pack evidence 都能解析到有效 blob 和显示范围。
- 同一 snapshot 的重复索引结果在结构上可复现。

## 5. Phase 0C：跨仓方案 PoC（第 5～6 周）

### 工作项

| ID | 工作 | 产出 |
| --- | --- | --- |
| SOL-001 | 建立 solution、solution member、solution snapshot schema | 跨仓版本模型 |
| SOL-002 | 手工定义首个包含 2～4 仓库的方案 | `solution.yaml` |
| SOL-003 | 实现 manifest/接口/外部符号产生的跨仓边 | cross-repo relations |
| SOL-004 | 实现先选仓库再分仓检索的两阶段路由 | solution router |
| SOL-005 | 实现跨仓统一重排和仓库限定 citation | multi-repo Context Pack |
| AUTH-001 | 实现 PoC 级仓库可见性过滤和 partial visibility | ACL test double |
| EVAL-008 | 新增至少 10 个跨仓问题 | cross-repo golden set |

### 验收

- 不允许把未列入 solution snapshot 的最新分支混入结果。
- 所有引用包含仓库、commit、路径和稳定 anchor。
- 隐藏任一仓库后，系统不泄露其名称、片段、摘要和关系。
- 跨仓 Evidence Recall@10 >= 0.80；版本组合准确率 = 1.0。
- 能明确回答“证据不足”或 `partial_visibility`。

## 6. Phase 1A：只读 MCP MVP（第 7～8 周）

### 工作项

| ID | 工作 | 产出 |
| --- | --- | --- |
| MCP-001 | 锁定 `mcp==1.28.0` 并建立 v2/客户端兼容测试 | dependency lock + tests |
| MCP-002 | 实现 `scope.resolve` | read tool |
| MCP-003 | 实现 `context.search` 和分页/大小限制 | read tool |
| MCP-004 | 实现 `context.get` 和 token budget | read tool |
| MCP-005 | 发布 stateless Streamable HTTP `/mcp/read` | MCP service |
| CLI-001 | 提供本地调试 CLI 和 MCP Inspector 配置 | 开发工具 |
| INT-001 | 完成 Cursor 配置和端到端测试 | integration guide |
| INT-002 | 完成 Claude Code 配置和端到端测试 | integration guide |
| OBS-001 | 为 MCP、检索、索引增加 trace_id 和核心指标 | OpenTelemetry traces |

### 验收

- Cursor 和 Claude Code 均能在不修改服务端的情况下调用同一 MCP。
- 连续 100 次标准查询无协议错误，错误响应可分类。
- Context Pack 严格满足条数、字节数、token 和深度限制。
- MCP 响应不会暴露数据库 ID、内部路径、凭据或无权限 scope。
- P95 检索延迟目标先设为 2 秒以内；索引规模扩大后重新基准化。

## 7. Phase 1B：团队安全试点（第 9～10 周）

### 工作项

| ID | 工作 | 产出 |
| --- | --- | --- |
| AUTH-002 | 接入 OIDC，建立用户/团队/角色模型 | authentication |
| AUTH-003 | 同步或映射 GitHub/GitLab repository ACL | authorization sync |
| AUTH-004 | PostgreSQL RLS、查询前过滤和安全域向量分区 | DB enforcement |
| POL-001 | 实现模型出域、日志保留、缓存和导出策略 | policy engine |
| SEC-001 | MCP token audience、scope、过期和撤销校验 | token verifier |
| SEC-002 | webhook 签名、重放保护、URL/SSRF 防护 | secure connectors |
| SEC-003 | 索引 worker 沙箱、默认禁网、资源限制 | hardened worker |
| SEC-004 | Prompt Injection、恶意仓库和越权检索测试 | adversarial suite |
| GIT-002 | webhook 增量索引和原子 snapshot 发布 | fresh indexing |
| DATA-004 | provenance 失效、re-anchor 和 snapshot 保留 | lifecycle jobs |

### 验收

- 越权测试和跨安全域查询泄露为零。
- 禁止的模型提供商无法获得受限仓库 Context Pack。
- 伪造 webhook、过期 token、错误 audience 和内网 URL 请求均被拒绝。
- 恶意仓库无法执行任意命令或访问 worker 网络。
- push 到可查询 snapshot 的 Freshness Lag 在试点规模下 P95 < 10 分钟。

## 8. Phase 1C：知识进化闭环（第 11～12 周）

### 工作项

| ID | 工作 | 产出 |
| --- | --- | --- |
| KB-001 | knowledge item、claim、citation 和状态机 | domain model |
| KB-002 | feedback 与 gap API | learning inputs |
| KB-003 | 高频 gap 聚合和 claim proposal worker | proposal pipeline |
| KB-004 | 机械校验、claim 级审核和发布 | governance workflow |
| KB-005 | 引用 re-anchor、stale、superseded | lifecycle workflow |
| WEB-001 | 查询 trace、gap、claim 审核最小页面 | review console |
| ANS-001 | 可选托管回答 API 与模型 provider interface | managed answer service |
| EVAL-009 | 托管回答的 groundedness/citation 评测 | answer report |

### 验收

- 至少一条真实问题完成 `query -> gap -> proposal -> validate -> review -> publish`。
- 每个发布知识由 claim 组成，每个 claim 至少有一条有效 citation。
- 机械校验不会被展示成“人工验证”。
- 引用代码改变后能重新定位或标记 stale，并保留历史版本。
- MCP 上下文模式和托管回答模式的可观察指标明确分开。

## 9. Phase 2 候选工作

以下工作只有在 Phase 1 指标证明需要后排期：

- workspace overlay：PR、branch 和短期本地 diff。
- 自动读取 release manifest、CI/BOM 生成 solution snapshot。
- ADR、Issue、PR、commit message、故障记录和会议纪要 connector。
- 更大规模 Zoekt 分片、专用向量库或图数据库；reranker 是否启用由 Phase 0B 指标决定。
- 更多 source-only 语言解析器和跨语言/跨仓符号映射。
- 运行时 trace、测试覆盖和生产遥测形成 observed runtime edge。
- Copilot cloud agent 的只读短期服务凭据与企业 allowlist 集成。

## 10. 必须建立的 ADR

| ADR | 决策问题 | 最迟完成时间 |
| --- | --- | --- |
| ADR-001 | Qwen3 embedding/reranker 是否达到进入 MVP 的收益门槛 | 已完成：[ADR-0002](decisions/0002-semantic-candidate-reranking.md) |
| ADR-002 | Context Pack v1 schema 和兼容策略 | Phase 0B 结束 |
| ADR-003 | symbol identity 与 citation re-anchor 算法 | Phase 0B 第 2 周 |
| ADR-004 | solution snapshot 来源与版本选择策略 | Phase 0C 第 1 周 |
| ADR-005 | MCP read/write 边界和客户端兼容矩阵 | Phase 1A 第 1 周 |
| ADR-006 | OIDC、ACL、RLS 与安全域分区 | Phase 1B 第 1 周 |
| ADR-007 | 模型出域和日志保留策略 | Phase 1B 第 1 周 |
| ADR-008 | AIKnowledge 上下文模式与托管回答模式边界 | 开工前确认 |

## 11. 工程 Definition of Done

任一工作项完成必须同时满足：

- 有自动化测试，失败路径被覆盖。
- 有结构化日志和 trace_id，不记录不必要的源码或密钥。
- 数据模型变更包含 migration 和回滚说明。
- API/MCP schema 有版本和兼容说明。
- 涉及权限时包含至少一个拒绝测试。
- 涉及检索时在黄金问题集上运行回归。
- 涉及派生产物时记录输入哈希、生成器/模型版本和 provenance。
- 文档、部署样例和操作步骤与代码同步更新。

## 12. 每周协作节奏

- 周一：冻结本周工作项、风险和评测集版本。
- 周中：运行离线评测，检查召回失败案例，不只看平均分。
- 周五：演示可运行产物，更新指标、ADR 和风险清单。
- 每个阶段结束：由领域工程师复核失败案例并决定继续、调整或停止。

黄金问题集分为公开开发集和隐藏回归集，避免为固定问题过拟合。新增的真实高价值问题经过脱敏和审核后进入下一版本评测集。

## 13. 开工前需要确认的输入

1. 首个目标仓库及只读访问方式。
2. 固定 commit 或发行归档摘要，并确认源码包含/排除范围；不需要构建环境。
3. 30～50 个历史真实问题及能够审核答案的领域工程师。
4. 是否允许代码或 embedding 发送到云端模型；若允许，批准的提供商列表。
5. 首个跨仓 solution 涉及哪些仓库，以及是否存在 release manifest/BOM。
6. 试点团队人数、使用的 Cursor/Claude/Copilot 客户端和部署网络环境。

上述输入未齐时仍可搭建项目骨架，但不能宣称 Phase 0 质量验证完成。
