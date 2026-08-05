# ADR-0002：保留语义候选重排，暂不启用全仓向量索引

- 状态：Accepted
- 日期：2026-08-06
- 范围：Phase 0B 单仓检索 PoC

## 背景

架构预留了 pgvector、embedding model 和 reranker，但是否启用必须由真实问题的增量收益决定。Linux 6.18.40 全树包含 3,970,532 个 chunk；直接建立全量向量索引会产生可观的首次推理、存储、ANN 索引、WAL 和增量维护成本。现有问题集只有 10 题，证据范围尚未经过领域工程师复核，不足以支持不可逆的生产选型。

## 决策

1. 接受通用 `EmbeddingProvider`、不可变 `EmbeddingModelSpec`、内容寻址本地 cache 和 `semantic-ablation` CLI，作为可复现实验边界。
2. 首个候选固定为 `Qwen/Qwen3-Embedding-0.6B@97b0c614…`、权重 SHA-256 `0437e45c…`、512 维、最大序列长度 2048。
3. 语义模型只重排经过 snapshot/citation/content-hash 校验的 Top-100 hybrid 候选；不允许模型绕过 repository/snapshot scope。
4. 保留 candidate weight `1.0`、semantic weight `0.5` 的保守 RRF 实验配置，但默认 Context Pack 仍不启用 semantic 通道。
5. 本阶段不生成 397 万个全量 embedding，不建立 HNSW/IVFFlat，也不接入额外的 Qwen3-Reranker 模型。
6. 在 30～50 个经过复核的问题、负样本和跨仓问题完成前，不把本轮 10 题结果称为黄金集结论，也不继续为固定题集调权重。

## 依据

- 深候选基线：File Recall `0.7222`、Range Recall `0.5926`、File MRR `0.9000`、Range MRR `0.7333`。
- 保守语义融合：`0.7778`、`0.6296`、`0.9000`、`0.8000`，四项无回退。
- 纯语义虽然 Recall 更高，但 File/Range MRR 降至 `0.5944`/`0.5033`，证明语义不能替代 symbol/relation 信号。
- Candidate@100 上限只有 20/27 个 evidence range，说明下一项主要矛盾是候选召回，而非重排。
- 512 维 halfvec 的原始负载约 4.07 GB；当前单机首次全量 embedding 粗估为数十小时。

## 后果

正向影响：

- 后续可以替换本地/HTTP provider，而不改评测和缓存契约。
- 模型、revision、权重、维度、instruction 和模板均进入 provenance，避免不同 embedding 静默混用。
- 只对有界候选推理，问题级更新成本低，且代码不出本机。

限制：

- 当前线上/Context Pack 不会获得 semantic 增益。
- 语义重排无法解决 Candidate@100 之外的文档和行范围缺口。
- 若未来启用全量向量召回，需要新增 PostgreSQL writer/query adapter、按安全域分区的 ANN、精确 recall 对照、重建与回滚流程。

## 重新评估条件

满足以下任一条件时重新评估全量向量召回：

- 经过复核的问题证明主要缺口是词面无法表达但语义相近的代码/文档；
- 跨仓 repository routing 后仍有系统性候选缺口；
- 选择性摘要/文件级向量不能达到召回门槛；
- 有可接受的首次构建窗口、存储预算和按 security domain 隔离方案。
