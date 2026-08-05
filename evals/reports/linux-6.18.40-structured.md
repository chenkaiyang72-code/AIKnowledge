# 结构化检索自动评测报告

- 问题数：10
- 标注证据范围数：27
- Top K：10
- 查询来源：`query_terms`
- Lexical 缓存：复用 `c4ebcd1a85403284127a253cdec45a7f9942867c06ea36253177ce399fdf222b`
- Snapshot：`linux@release:6.18.40` (`snap_a162172858bee6eee189963a`)
- 证据状态：沿用数据集现有标注；未经过人工复核的 draft 不能视为冻结黄金集。

## 汇总指标

| 检索器 | File Recall | Evidence Range Recall | File MRR | Range MRR | 完整/部分/未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `lexical_fts5` | 0.5556 | 0.2593 | 0.7000 | 0.5167 | 0/6/4 |
| `lexical_fts5+symbol_exact+relation_source` | 0.7222 | 0.5926 | 0.8500 | 0.6833 | 5/3/2 |

## 增量

- File Recall Δ：+0.1667
- Evidence Range Recall Δ：+0.3333
- File MRR Δ：+0.1500
- Evidence Range MRR Δ：+0.1667

## 逐题结果

| 问题 | Lexical | Hybrid RRF | Lexical 范围 | Hybrid 范围 |
| --- | --- | --- | ---: | ---: |
| `linux-web-001` | partial | complete | 1/2 | 2/2 |
| `linux-web-002` | missed | missed | 0/2 | 0/2 |
| `linux-web-003` | missed | complete | 0/1 | 1/1 |
| `linux-web-006` | missed | missed | 0/3 | 0/3 |
| `linux-web-008` | partial | partial | 1/3 | 1/3 |
| `linux-web-011` | partial | complete | 1/3 | 3/3 |
| `linux-web-013` | missed | partial | 0/3 | 1/3 |
| `linux-web-015` | partial | complete | 1/3 | 3/3 |
| `linux-web-020` | partial | partial | 2/4 | 2/4 |
| `linux-web-023` | partial | complete | 1/3 | 3/3 |

## 判定原则

- File Recall 用于和旧版按文件评测保持可比。
- Evidence Range Recall 要求返回 chunk 与标注行范围重叠，是更严格的 Context Pack 证据指标。
- 本报告只评价检索证据，不评价模型生成答案。
- vector/reranker 必须相对当前 hybrid 基线取得可重复增量才允许进入正式通道。
