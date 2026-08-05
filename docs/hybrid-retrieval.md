# 本地混合检索 v1

## 目标

本步骤把检索从“一个 FTS 查询”拆成可替换通道，并用确定性的 Reciprocal Rank Fusion（RRF）融合。它解决高频调用点、测试或文档淹没精确定义的问题，同时保留每条结果的来源和排名贡献。

整个过程只查询 source-only 索引，不编译源码，也不调用模型。

## 当前通道

| 通道 | 当前 adapter | 权重 | 作用 |
| --- | --- | ---: | --- |
| `lexical_zoekt` | Zoekt（正式通道） | 1.0 | 大仓文本、标识符、路径和正则召回 |
| `lexical_fts5` | SQLite FTS5（回退） | 1.0 | 本地 bootstrap 与故障回退 |
| `lexical_postgres_fts` | PostgreSQL simple-text FTS（回退） | 1.0 | 团队主库故障回退 |
| `symbol_exact` | `symbol_occurrence` | 2.0 | 提升精确名称的定义和声明 |
| `relation_source` | `relation` | 0.75 | 补充直接调用、依赖和关系位置 |

三种 lexical provider 是互斥实现：一次查询只把实际使用的一个 lexical 通道写入 trace。Zoekt 不可用时才回退；协议错误、越界 repository 或版本不一致不会被静默降级。

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

每条结果包含 `fused_score` 和 `contributions`，可以看到它在每个通道的 rank、weight 和 reciprocal score。Context Pack v1.2 使用同一结果并把这些信息写入 retrieval trace。

## 真实源码结果

Linux 6.18.40 `kernel/sched` 活动 snapshot 上：

- 纯 FTS 中 `init_idle` 定义位于第 4；混合检索提升到第 1。
- `init_idle` 头文件声明位于第 2。
- `do_idle` 定义通过 symbol/relation 通道进入第 3。
- 第一名同时得到 `lexical_fts5`、`symbol_exact` 和 `relation_source` 支持。
- 增加 blob 行范围读取后，混合检索从约 8.3 秒降到约 0.38 秒；完整 Context Pack 约 0.97 秒。

这些数字是当前 Windows 本地 PoC 的单次观测，不作为生产 SLA。后续必须在更大范围、并发和 PostgreSQL/Zoekt 环境重新测量。

Linux 6.18.40 全树 snapshot 的 10 题自动评测进一步得到：

| 检索器 | File Recall@10 | Range Recall@10 | File MRR | Range MRR | 完整/部分/未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `lexical_fts5` | 0.5556 | 0.2593 | 0.7000 | 0.5167 | 0/6/4 |
| `lexical_fts5+symbol_exact+relation_source` | 0.7222 | 0.5926 | 0.8500 | 0.6833 | 5/3/2 |

本轮同时发现并修复两类规模问题：relation 的跨 JOIN OR 会扫描全部 5,098,771 条关系，现拆成三个索引候选通道，真实查询降到约 0.010～0.022 秒；多行 macro occurrence 与单行 macro chunk 现按范围重叠映射，避免 `likely`、`container_of`、`EXPORT_SYMBOL` 等真正定义被漏掉。完整报告见[全树验证](full-tree-validation.md)。

## 性能设计

FTS5 只用于 `MATCH`。symbol/relation 通道不再用 `chunk_id` 等值扫描 FTS5 虚表，而是从内容寻址 blob 解压对应文件并按 chunk 行范围读取正文。symbol 通道按查询标识符分别执行 definition-first 有界召回；relation 通道分别使用 source symbol、target symbol 和 target text 索引，再合并有界候选。catalog 增加 `chunk(file_id,start_line,end_line)`、relation source/target 索引，为后续存储 adapter 保留相同查询语义。

SQLite FTS 在 397 万 chunk 上完成 10 题 BM25 全局排序约需 605 秒，只承担离线 baseline。评测器支持严格校验的 lexical cache，后续只调整 symbol/relation/vector/reranker 时可把同一 snapshot 的下游消融缩短到秒级；正式线上 lexical 仍使用 Zoekt。

## 下一步

1. 已完成现有真实问题集的 FTS 与 symbol/relation RRF 全树自动对比。
2. 接入 pgvector 实验通道，但只有相对当前无向量基线的 Evidence Recall@10/MRR 有可复现增益时才保留 embedding/reranker。
3. 增加跨仓 solution snapshot 路由，使多个不可变 repository snapshot 进入同一检索 scope。
