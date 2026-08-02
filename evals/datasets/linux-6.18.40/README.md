# Linux 6.18.40 Phase 0A 数据集

这个目录固定第一个实验语料和问题格式。`questions.smoke.jsonl` 只验证工具链，不计入正式黄金集；正式数据必须来自团队过去真实提出的问题。

`questions.candidates.jsonl` 收录了 56 条来自公开网络原始提问或真实缺陷报告的候选题。它们已经翻译并收敛到 Linux 6.18.40 源码实验，但尚未完成人工证据标注，不能直接视为黄金集。

## 当前语料身份

- 发行版本：Linux 6.18.40
- 本地归档：`linux-6.18.40.tar.xz`
- SHA-256：`3712fc1ec839e4daac981176c8518912e8f452650aaedfe4381da4419613a431`
- Git commit：无，当前目录是发行归档的解压结果
- build profile：无 `.config`、`compile_commands.json` 和 `Module.symvers`

因此 Phase 0A 只评估文本、路径和精确标识符召回。涉及宏展开、条件编译、跨翻译单元类型解析的问题，要等生成固定 build profile 后才进入 SCIP/scip-clang 评测。

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
```

正式进入下一阶段前，需要收集 30～50 道真实问题，其中至少 20% 是“当前源码中证据不足或无法回答”的负样本。
