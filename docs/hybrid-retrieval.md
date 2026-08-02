# 本地混合检索 v1

## 目标

本步骤把检索从“一个 FTS 查询”拆成可替换通道，并用确定性的 Reciprocal Rank Fusion（RRF）融合。它解决高频调用点、测试或文档淹没精确定义的问题，同时保留每条结果的来源和排名贡献。

整个过程只查询 source-only 索引，不编译源码，也不调用模型。

## 当前通道

| 通道 | 当前 adapter | 权重 | 作用 |
| --- | --- | ---: | --- |
| `lexical_fts5` | SQLite FTS5 | 1.0 | 普通文本和标识符召回 |
| `symbol_exact` | `symbol_occurrence` | 2.0 | 提升精确名称的定义和声明 |
| `relation_source` | `relation` | 0.75 | 补充直接调用、依赖和关系位置 |

每个通道产生独立 rank。候选的融合分数为：

```text
score(candidate) = Σ channel_weight / (60 + channel_rank)
```

相同分数按最优通道排名、repository、path、line 和 chunk ID 确定性排序。RRF 不直接比较 BM25、向量相似度等不可比原始分数，因此后续可以增加 Zoekt 和 pgvector adapter。

## CLI

```powershell
python -m aikb kb-retrieve `
  --db .aikb/linux-sched-smoke.db `
  --query "init_idle do_idle" `
  --top-k 10
```

每条结果包含 `fused_score` 和 `contributions`，可以看到它在每个通道的 rank、weight 和 reciprocal score。Context Pack v1.1 使用同一结果并把这些信息写入 retrieval trace。

## 真实源码结果

Linux 6.18.40 `kernel/sched` 活动 snapshot 上：

- 纯 FTS 中 `init_idle` 定义位于第 4；混合检索提升到第 1。
- `init_idle` 头文件声明位于第 2。
- `do_idle` 定义通过 symbol/relation 通道进入第 3。
- 第一名同时得到 `lexical_fts5`、`symbol_exact` 和 `relation_source` 支持。
- 增加 blob 行范围读取后，混合检索从约 8.3 秒降到约 0.38 秒；完整 Context Pack 约 0.97 秒。

这些数字是当前 Windows 本地 PoC 的单次观测，不作为生产 SLA。后续必须在更大范围、并发和 PostgreSQL/Zoekt 环境重新测量。

## 性能设计

FTS5 只用于 `MATCH`。symbol/relation 通道不再用 `chunk_id` 等值扫描 FTS5 虚表，而是从内容寻址 blob 解压对应文件并按 chunk 行范围读取正文。catalog 增加 `chunk(file_id,start_line,end_line)`、relation source/target 索引，为后续存储 adapter 保留相同查询语义。

## 下一步

1. 定义 retriever provider interface，使 SQLite/Zoekt/pgvector adapter 可以通过配置组合。
2. 接入 Zoekt 作为正式 lexical 通道，SQLite FTS5 保留为 bootstrap 和故障回退。
3. 将 catalog 领域模型迁移到 PostgreSQL，并通过 migration/约束测试验证。
4. 恢复真实问题集，用 Evidence Recall@10 和 MRR 决定 vector/reranker 是否值得启用。
