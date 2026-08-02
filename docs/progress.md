# AIKnowledge 项目进展

最后更新：2026-08-02<br>
当前阶段：Phase 0A 评测基线（进行中）<br>
当前结论：已经建立可复现的检索评测工具，但尚未建立真正的知识库。

## 维护规则

- 本文档是项目进展的唯一汇总入口；详细设计和任务定义仍以技术蓝图及实施计划为准。
- 每次完成工作项、改变技术决策、获得新评测结果或发现阻塞问题时，都要同步更新本文档。
- 只有满足阶段退出条件后，阶段才标记为“完成”；写了代码但尚未验证，仍标记为“进行中”。
- 已完成事项记录实际产出和验证证据，未完成事项记录下一动作及完成条件。

## 1. 整体的计划

| 顺序 | 阶段 | 目标 | 主要产出 | 完成条件 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | Phase 0A 评测基线 | 建立可以衡量检索质量的真实问题集和普通文本检索基线 | 固定源码范围、30～50 个真实问题、负样本、ripgrep baseline、Recall/MRR 报告 | 数据经过人工复核，指标可重复运行 | 进行中 |
| 2 | Phase 0B 结构化检索 | 建立第一版单仓代码知识索引 | 不可变 snapshot、Tree-sitter、SCIP/scip-clang、Zoekt、pgvector、混合检索、Context Pack | Evidence Recall@10、版本准确率和负样本指标达到门槛 | 未开始 |
| 3 | Phase 0C 跨仓 PoC | 让一个问题可以在一致版本下检索多个仓库 | solution snapshot、仓库路由、跨仓关系、跨仓引用 | 至少 10 个跨仓问题通过评测 | 未开始 |
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
- 已确定核心技术路线：PostgreSQL/pgvector、Tree-sitter、SCIP/scip-clang、Zoekt、MCP。
- 已定义跨仓模型：`solution`、`solution_snapshot` 和 `solution_member`。
- 已定义知识治理原则：AI 生成内容必须经过证据校验和人工审核后才能发布。

### 2.2 Phase 0A 已完成的工作项

- 已选择首个实验语料：本地 Linux 6.18.40 源码。
- 已固定发行归档 SHA-256：`3712fc1ec839e4daac981176c8518912e8f452650aaedfe4381da4419613a431`。
- 已确认当前源码树没有 `.git`、`.config`、`compile_commands.json` 和 `Module.symvers`，并将这些限制写入 scope。
- 已建立评测 scope：`evals/datasets/linux-6.18.40/scope.json`。
- 已定义问题、检索词和源码证据的 JSONL 格式。
- 已实现无第三方运行时依赖的 ripgrep fixed-string baseline CLI。
- 已实现源码版本与归档 SHA-256 校验。
- 已加入 3 道合成冒烟题和 2 个单元测试。
- 已完成冒烟运行：Evidence Recall@10 = `1.0`，MRR = `0.5833`，3 个目标文件均进入前 10。
- 已验证 baseline 的主要缺陷：调用点、测试和文档可能排在函数定义之前，为后续结构化检索提供了可比较基线。
- 已从 Stack Overflow、Unix & Linux Stack Exchange 和 syzbot 整理 56 条带原始链接的真实公开候选题；这些题尚待用户筛选和证据标注，不计入已冻结黄金集。

## 3. 还未完成的计划

### 3.1 当前优先级：完成 Phase 0A

| 优先级 | 未完成事项 | 下一动作 | 完成条件 |
| --- | --- | --- | --- |
| P0 | 筛选第一批真实问题 | 从 56 条公开候选题中选出至少 10 条，也可补充团队过去实际问过的问题 | 至少 10 条候选题被标记为 `accepted` |
| P0 | 建立负样本 | 标记源码中没有答案、版本不匹配或证据不足的问题 | 负样本不少于问题总数的 20% |
| P0 | 人工标注关键证据 | 由领域工程师确认仓库、文件、符号和代码范围 | 每个正样本至少有一处经过确认的关键证据 |
| P0 | 冻结黄金集 v1 | 清理合成题与真实题边界，完成复核 | 数据格式校验通过且争议题已移除或标记 |
| P0 | 生成正式 baseline 报告 | 对黄金集运行 ripgrep baseline | Recall@K、MRR、耗时和失败分类可重复生成 |

在以上事项完成前，Phase 0A 不能标记为完成。

### 3.2 Phase 0B：真正的第一版知识库

- 尚未实现 repository/snapshot/blob/chunk/citation 持久化模型。
- 尚未部署 PostgreSQL 和 pgvector。
- 尚未使用 Tree-sitter 生成结构化代码块。
- 尚未生成固定 build profile 和 `compile_commands.json`。
- 尚未接入 SCIP/scip-clang 的定义、引用和实现索引。
- 尚未部署 Zoekt 正式代码文本索引。
- 尚未实现 lexical、symbol、vector 和团队知识的混合召回及 RRF。
- 尚未实现稳定 citation、retrieval trace 和 Context Pack。

只有完成这些工作并通过 Phase 0B 指标后，才可以称为“成功建立了第一版单仓代码知识库”。

### 3.3 后续阶段

- 跨仓 solution snapshot、两阶段仓库路由和部分可见性尚未实现。
- Cursor、Claude Code 等客户端的只读 MCP 接入尚未实现。
- OIDC、ACL、RLS、数据出域策略、审计和安全测试尚未实现。
- feedback、gap、claim、review、publish 知识进化闭环尚未实现。
- Web 管理控制台、workspace overlay 和更多知识源 connector 尚未实现。

## 4. 下一步要做的事情

当前只推进 Phase 0A，下一步按照以下顺序执行：

| 顺序 | 要做的事情 | 负责人 | 具体产出 | 完成判断 |
| --- | --- | --- | --- | --- |
| 1 | 审阅第一批真实问题 | 用户 | 审阅 `questions.candidates.jsonl` 中的 56 条公开候选题，标记保留或删除；也可补充团队问题 | 至少 10 条被确认保留 |
| 2 | 整理问题并分类 | Codex + 用户确认 | 为每个问题分配 ID、category、原始来源和初始 query terms | 用户确认问题含义没有被改写 |
| 3 | 标注关键源码证据 | Codex 初查，用户复核 | 每个正样本的文件、符号、行范围和解释 | 领域判断通过；有争议的问题单独标记 |
| 4 | 增加负样本 | Codex + 用户 | 当前源码没有答案、版本不匹配或证据不足的问题 | 负样本达到问题总数的至少 20% |
| 5 | 扩充并冻结黄金集 v1 | Codex + 用户 | `questions.jsonl`，共 30～50 个真实问题 | 格式校验通过，所有正样本有证据，争议题已处理 |
| 6 | 运行正式 lexical baseline | Codex | Recall@K、MRR、耗时、失败类型和逐题结果报告 | 相同命令可以重复得到相同结果 |
| 7 | 评审 Phase 0A | Codex + 用户 | Phase 0A 结论和 Phase 0B 输入清单 | 满足 Phase 0A 退出条件后，状态改为完成 |
| 8 | 启动第一版知识库实现 | Codex | repository/snapshot 数据模型、Tree-sitter ingest 和持久化索引骨架 | Phase 0B 第一个可查询 snapshot 建立 |

### 现在立即要做的动作

用户先审阅 `evals/datasets/linux-6.18.40/questions.candidates.jsonl`。建议把满意题目的 `review_status` 改为 `accepted`，不满意的改为 `rejected`；也可以直接删除不满意的行。

如果要补充团队自己的问题，可以直接使用口语化描述，例如：

```text
1. 某个内核问题当时是怎么问的？
2. 当时涉及哪个模块、现象或函数？不知道也可以不写。
3. 当时是否已经找到答案？如果有，可以附上记得的文件或结论。
```

收到问题后，Codex 负责整理格式、检索 Linux 6.18.40、提出证据标注，并交给用户确认。现阶段不要求用户手工编写 JSONL。

## 相关文档

- [项目总体设计](architecture.md)
- [技术蓝图](technical-blueprint.md)
- [具体实施计划](implementation-plan.md)
- [Linux 6.18.40 Phase 0A 实验](../evals/datasets/linux-6.18.40/README.md)
