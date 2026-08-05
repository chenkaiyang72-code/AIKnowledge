# 语义候选重排消融报告

- 问题数：10
- Top K：10
- Candidate K：100
- 候选报告：`3e83741a74fcfcde0b51c845c0a883311e6da6f7b09b1adbb94da37f241e63b9`
- 模型：`Qwen/Qwen3-Embedding-0.6B`
- 模型 revision：`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`
- 权重 SHA-256：`0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`
- 向量维度：512
- 最大序列长度：2048
- 模型指纹：`05cee76a87fc545e2daa9523a3bee2d1bb339aa87630ee62fee0bd75f5db2ec8`
- 融合：RRF k=60，candidate weight=1，semantic weight=0.5
- 查询输入：自然语言问题；instruction 固定为代码证据检索任务。
- 证据状态：沿用 draft 标注，只用于工程消融，不视为冻结黄金集。

## Top-K 指标

| 检索器 | File Recall | Range Recall | File MRR | Range MRR | 完整/部分/未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `candidate_hybrid` | 0.7222 | 0.5926 | 0.9000 | 0.7333 | 5/3/2 |
| `semantic_rerank` | 0.8333 | 0.6667 | 0.5944 | 0.5033 | 5/3/2 |
| `hybrid_semantic_rrf` | 0.7778 | 0.6296 | 0.9000 | 0.8000 | 5/3/2 |

## 融合增量

- File Recall Δ：+0.0556
- Range Recall Δ：+0.0370
- File MRR Δ：+0.0000
- Range MRR Δ：+0.0667

## 候选池上限

Candidate@100 已包含 15/18 个证据文件、20/27 个证据范围。重排只能移动已进入候选池的证据，不能找回池外证据。

## 逐题结果

| 问题 | Candidate | Semantic | Fused | Candidate 范围 | Fused 范围 |
| --- | --- | --- | --- | ---: | ---: |
| `linux-web-001` | complete | complete | complete | 2/2 | 2/2 |
| `linux-web-002` | missed | missed | missed | 0/2 | 0/2 |
| `linux-web-003` | complete | complete | complete | 1/1 | 1/1 |
| `linux-web-006` | missed | missed | missed | 0/3 | 0/3 |
| `linux-web-008` | partial | partial | partial | 1/3 | 2/3 |
| `linux-web-011` | complete | complete | complete | 3/3 | 3/3 |
| `linux-web-013` | partial | partial | partial | 1/3 | 1/3 |
| `linux-web-015` | complete | complete | complete | 3/3 | 3/3 |
| `linux-web-020` | partial | partial | partial | 2/4 | 2/4 |
| `linux-web-023` | complete | complete | complete | 3/3 | 3/3 |

## 缓存

- Query hit/miss：0/10
- Document hit/miss：1/829

## 判定

只有相对同一深候选基线的 Recall/MRR 有可解释净增益，并且本地延迟、显存和索引成本可接受，语义通道才进入正式检索。
