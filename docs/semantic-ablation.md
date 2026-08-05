# Qwen3 语义候选重排消融

## 结论

AIKnowledge 已在 Linux 6.18.40 的同一不可变 snapshot、同一批 10 个问题和 Top-100 深候选上完成本地语义消融。保守的 `hybrid + semantic RRF` 相对深候选基线同时提高 File Recall、Evidence Range Recall 和 Range MRR，并保持 File MRR 不变，因此保留 provider、embedding cache 和候选重排接口。

本轮不启用全仓向量召回，也不把 semantic 通道接入默认 Context Pack。原因是现有 10 题仍是 draft evidence，融合后的 Range Recall@10 只有 `0.6296`，而且 Candidate@100 之外仍有 7/27 个证据范围；这些缺口必须由新的召回通道或 source-only 规则解决，reranker 无法凭空找回。

决策记录见 [ADR-0002](decisions/0002-semantic-candidate-reranking.md)，完整逐题结果见[自动报告](../evals/reports/linux-6.18.40-semantic-qwen3-512.md)。

## 固定模型与输入

- Provider：`sentence-transformers==5.6.1`
- Model：`Qwen/Qwen3-Embedding-0.6B`
- Revision：`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`
- `model.safetensors` SHA-256：`0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`
- License：Apache-2.0
- Dimension：512；利用模型的 MRL 能力从最大 1024 维裁剪
- Max sequence length：2048
- Query：使用问题的自然语言正文，并固定英文 code-evidence instruction
- Document：`path + line range + kind + symbol + content` 的版本化模板
- Candidate：Top-100 `lexical + symbol + relation RRF`
- Fusion：RRF `k=60`，candidate weight `1.0`，semantic weight `0.5`

Qwen 官方模型卡说明 0.6B 模型支持代码与多语言检索、32K 上下文和 32～1024 自定义维度，并建议 query 使用任务 instruction；本实验选择 512 维和 2048 token 上限是本项目自己的资源折中，不是模型限制。[Qwen 模型卡](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)、[Qwen 官方实现](https://github.com/QwenLM/Qwen3-Embedding)

## 结果

| 检索器 | File Recall@10 | Range Recall@10 | File MRR | Range MRR | 完整/部分/未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Top-100 hybrid 候选原顺序 | 0.7222 | 0.5926 | 0.9000 | 0.7333 | 5/3/2 |
| 纯 semantic cosine | 0.8333 | 0.6667 | 0.5944 | 0.5033 | 5/3/2 |
| hybrid + semantic RRF | 0.7778 | 0.6296 | 0.9000 | 0.8000 | 5/3/2 |

融合相对深候选基线的增量：

- File Recall：`+0.0556`
- Evidence Range Recall：`+0.0370`
- File MRR：`+0.0000`
- Evidence Range MRR：`+0.0667`

纯 semantic 能找回更多证据，但把若干精确符号结果向后移动，MRR 明显下降，因此不能替换结构化 hybrid。保守融合保留 symbol/relation 的排序主导权，同时让 `__user` 问题多找回一处证据范围。

Candidate@100 的理论上限是 15/18 个证据文件和 20/27 个证据范围；融合目前把 14 个文件和 17 个范围推入 Top-10。剩余优先级应是扩大候选召回，而不是继续在 10 道 draft 问题上微调 semantic weight。

## 资源与缓存

实验机器为 RTX 4060 Ti 8 GB、32 GB 内存。模型文件预先下载后，830 个 query/document embedding 的冷运行约 31 秒；内容寻址缓存命中后的完整复跑约 1.3 秒，其中还包含对 1.19 GB 权重文件的 SHA-256 校验。缓存指纹包含 provider、模型名、不可变 revision、权重摘要、维度、最大序列长度、query instruction 和 document template version，任何影响向量的配置变化都会使用新缓存空间。

如果直接为 3,970,532 个 chunk 保存 512 维 `halfvec`，仅向量负载约为 4.07 GB，尚未包含 PostgreSQL 行、索引、WAL 和备份；按本次候选吞吐粗略外推，单机首次 embedding 是数十小时量级。pgvector 官方建议规模较大时使用 `halfvec`、量化和原向量重排，并监控近似索引相对精确搜索的 recall；这些优化应在全量向量召回已证明必要后采用。[pgvector 官方文档](https://github.com/pgvector/pgvector)

## 复现

基础依赖：

```powershell
python -m pip install -e ".[semantic]"
```

本机 GPU 使用了 PyTorch 官方 CUDA 13.0 预编译 wheel；这只安装二进制运行时，不编译项目或 Linux 源码：

```powershell
python -m pip install --upgrade torch==2.13.0+cu130 `
  --index-url https://download.pytorch.org/whl/cu130
```

先生成深候选：

```powershell
python -m aikb structured `
  --db .aikb/linux-full-eval.db `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --output evals/results/linux-6.18.40-structured-top100.json `
  --top-k 100
```

运行语义消融：

```powershell
python -m aikb semantic-ablation `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --input evals/results/linux-6.18.40-structured-top100.json `
  --output evals/results/linux-6.18.40-semantic-qwen3-512.json `
  --markdown-output evals/reports/linux-6.18.40-semantic-qwen3-512.md `
  --cache-db .aikb/embedding-cache.db `
  --top-k 10 `
  --candidate-k 100 `
  --model-name Qwen/Qwen3-Embedding-0.6B `
  --model-revision 97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3 `
  --model-weights-sha256 0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd `
  --dimension 512 `
  --device cuda `
  --batch-size 4 `
  --max-seq-length 2048
```

`--model-path` 可指向提前下载并按 SHA-256 校验的本地模型目录；不指定时，provider 按不可变 revision 从模型仓库加载。原始 JSON 结果和模型/embedding cache 位于 Git 忽略目录，不提交模型权重、源码片段或本机数据库。
