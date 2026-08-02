# Context Pack v1

## 目标

Context Pack 是 AIKnowledge 提供给 Cursor、Claude Code、MCP 和未来托管回答服务的稳定证据契约。它只组织检索证据，不生成答案，也不把某个模型或检索引擎写进客户端协议。

当前 schema 标识为 `urn:aiknowledge:schema:context-pack:v1`，版本为 `1.1`。Pydantic 模型位于 `src/aikb/context_pack.py`，可通过以下命令输出 JSON Schema：

```powershell
python -m aikb kb-context-schema
```

## 核心约束

1. 每条代码证据都必须包含 repository、snapshot、revision、blob ID、chunk ID、内容哈希、路径和行范围。
2. citation 使用 `repository@revision:path:start-end`，展示行号与对应 snapshot 绑定。
3. 同一 query、scope、snapshot、候选集合和预算生成相同的 context ID 与 trace ID；schema 中不放当前时间等非确定性字段。
4. `complete` 默认是 `false`。当前检索层只能证明“找到证据”，不能自行证明答案完整。
5. 没有证据时返回 `evidence_status=none` 和明确 gap，不生成猜测答案。
6. 源码和关系仍来自 source-only 扫描；构建、生成器和仓库脚本不会因为 Context Pack 被执行。

## 顶层结构

| 字段 | 含义 |
| --- | --- |
| `schema_uri/schema_version` | 版本化契约标识 |
| `id` | 根据 trace、snapshot 和 evidence 确定性生成的 Context Pack ID |
| `query` | 去除首尾空白并折叠连续空白后的确定性问题文本 |
| `scope` | 请求范围及实际使用的不可变 snapshot |
| `evidence` | 带稳定引用的代码 chunk |
| `symbols` | 查询标识符及其定义、声明、关系候选和条件 |
| `team_knowledge` | 已发布团队知识；当前 bootstrap 阶段为空 |
| `coverage/gaps` | 证据可用性、未覆盖内容和截断提示 |
| `budget` | 证据条数、近似 token、symbol 和 relation 预算及实际使用量 |
| `retrieval_trace` | retriever 版本、候选顺序、选择结果和省略原因 |

v1.1 在 v1.0 的证据契约上增加 RRF 通道贡献：每条 evidence 和 trace candidate 都记录 `lexical_fts5`、`symbol_exact`、`relation_source` 的独立 rank、weight 和 reciprocal score。

## 预算语义

当前 bootstrap 没有绑定模型 tokenizer，按 4 个字符约等于 1 token 计算 `estimated_evidence_tokens`。这个预算只约束代码证据正文；结构化引用、trace 和 symbol 元数据分别受条数预算约束。接入具体模型时可以增加 provider tokenizer，但不得改变 v1 字段含义。

候选可能因为以下原因被省略：

- `item_budget`：证据条数达到上限。
- `token_budget`：剩余正文预算不足 64 个字符。
- 单条内容超过剩余预算时会被确定性截断并标记 `content_truncated=true`。

## 本地运行

```powershell
python -m aikb kb-context `
  --db .aikb/linux-sched-smoke.db `
  --query "init_idle do_idle" `
  --max-evidence-items 6 `
  --evidence-token-budget 1200
```

真实 Linux 6.18.40 验证生成：

- Context ID：`context_8f20640aa1d2f62501cdb7ec`
- Trace ID：`trace_5c4ec50ca3e25a95b4b186d6`
- 活动 snapshot：`snap_0b0e8c0e71ad7f720c31b8e2`
- 6 条代码证据；第一条是 `kernel/sched/core.c:7961-8027` 的 `init_idle` 定义
- 同一命令重复构建得到相同 ID
- 第一条证据同时由 lexical、symbol 和 relation 三个通道支持

## 当前限制与后续兼容

- 当前 lexical adapter 是 SQLite FTS5，只用于本地 bootstrap；精确 symbol、relation 和 RRF 已独立实现。
- `team_knowledge` 尚未接入已审核知识条目。
- `partial_visibility` 当前为 `false`；进入团队服务后必须由 ACL 层计算。
- Zoekt 和 vector 检索将作为新的 retrieval adapter 接入 builder，不改变 Context Pack v1 的核心消费方式。
- 跨仓阶段会让 `scope.snapshots` 同时包含 solution snapshot 的多个仓库版本。
