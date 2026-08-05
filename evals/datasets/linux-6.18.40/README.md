# Linux 6.18.40 Phase 0A 数据集

这个目录固定第一个实验语料和问题格式。`questions.smoke.jsonl` 只验证工具链，不计入正式黄金集；正式数据必须来自团队过去真实提出的问题。

`questions.candidates.jsonl` 收录了 56 条来自公开网络原始提问或真实缺陷报告的候选题。它们已经翻译并收敛到 Linux 6.18.40 源码实验，但尚未完成人工证据标注，不能直接视为黄金集。

`questions.first-batch.jsonl` 是从候选题中初选的 10 个问题，已由用户确认全部保留，因此当前 `review_status` 为 `accepted`。Codex 已为它们标注 27 处源码范围、共 18 个唯一证据文件，并完成路径和行号校验；`evidence_status` 仍为 `draft`，必须经过证据复核后才能改为 `reviewed`。

## 当前语料身份

- 发行版本：Linux 6.18.40
- 本地归档：`linux-6.18.40.tar.xz`
- SHA-256：`3712fc1ec839e4daac981176c8518912e8f452650aaedfe4381da4419613a431`
- Git commit：无，当前目录是发行归档的解压结果
- 索引策略：`source_only`，不编译、不执行仓库脚本、不依赖任何构建产物

Phase 0A 只评估文本、路径和精确标识符召回。后续对宏、条件编译和跨文件关系的准确性，不通过编译某个配置解决，而是直接解析条件表达式、Kconfig/Kbuild、include、标识符和调用候选，并在结果中保留歧义与置信度。

## 问题格式

每行一个 JSON 对象：

```json
{
  "id": "linux-real-001",
  "question": "团队成员当时实际提出的问题",
  "category": "symbol_definition",
  "synthetic": false,
  "query_terms": ["exact_identifier"],
  "required_evidence": [
    {
      "path": "kernel/example.c",
      "start_line": 100,
      "end_line": 120,
      "symbol": "exact_identifier"
    }
  ]
}
```

`query_terms` 是 Phase 0A baseline 使用的字面检索词，不等同于未来系统的查询理解结果。标注人必须先独立确认 `required_evidence`，不能把 baseline 搜到的第一条结果直接当答案。

候选题还包含：

- `review_status`：初始为 `candidate`，审阅后改为 `accepted` 或 `rejected`；建议保留拒绝记录而不是直接删除。
- `answerability_hint`：对源码可回答性和版本/配置依赖的初步提示，不是最终标签。
- `provenance`：原始网站、标题、直达链接和采集日期。

在 PowerShell 中查看候选题：

```powershell
$candidates = Get-Content -Encoding UTF8 questions.candidates.jsonl |
  ForEach-Object { $_ | ConvertFrom-Json }

$candidates.Count
$candidates | Select-Object id, question, answerability_hint, review_status
```

## 运行

在项目根目录执行：

```powershell
$env:PYTHONPATH = "src"
$source = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40"
$archive = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40.tar.xz"

python -m aikb inspect `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --source $source `
  --archive $archive

python -m aikb baseline `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --questions evals/datasets/linux-6.18.40/questions.smoke.jsonl `
  --source $source `
  --output evals/results/linux-6.18.40-smoke.json

python -m aikb baseline `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --source $source `
  --output evals/results/linux-6.18.40-first-batch-scope-v2.json
```

第一批结果见 `evals/reports/linux-6.18.40-first-batch.md`：固定范围包含 `scripts/` 后，Evidence Recall@10 为 `0.5000`，MRR 为 `0.3583`。该结果只用于暴露检索缺陷，不能替代人工证据复核。

全树结构化评测：

```powershell
python -m aikb structured `
  --db .aikb/linux-full-eval.db `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --output evals/results/linux-6.18.40-structured.json `
  --markdown-output evals/reports/linux-6.18.40-structured.md `
  --top-k 10
```

当前无向量 hybrid 基线的 File Recall@10 为 `0.7222`、Evidence Range Recall@10 为 `0.5926`、File MRR 为 `0.8500`、Range MRR 为 `0.6833`。逐题结果见 `evals/reports/linux-6.18.40-structured.md`。

按照用户决定，人工证据复核、扩充到 30～50 题和负样本目前继续暂停；现有 10 题和 draft evidence 只作为自动回归与技术决策输入，不删除也不冒充冻结黄金集。
