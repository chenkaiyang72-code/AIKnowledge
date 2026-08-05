# Linux 6.18.40 全树 source-only 验证

## 结论

AIKnowledge 已对本地 Linux 6.18.40 固定源码范围完成一次冷启动全量扫描，并生成经过计数复核的 active snapshot。整个过程只读取源码和文档，不执行 `make`、编译器、生成器或仓库脚本，也不依赖 `.config`、`compile_commands.json` 或构建产物。

这次结果证明当前 scanner 能在单机上直接处理完整 Linux 源码规模；SQLite 仍只是本地 bootstrap catalog，约 10 分钟的全树 FTS 评测也再次证明正式团队查询必须使用已接入的 Zoekt，而不是把 SQLite FTS 当作生产搜索引擎。

## 固定输入

- Source：`linux-6.18.40.tar.xz` 的解压目录
- Archive SHA-256：`3712fc1ec839e4daac981176c8518912e8f452650aaedfe4381da4419613a431`
- Revision：`release:6.18.40`
- Scope：`evals/datasets/linux-6.18.40/scope.json`
- Dependency depth：`0`，本轮直接扫描 scope 中的完整根目录，不额外扩展范围
- Snapshot：`snap_a162172858bee6eee189963a`
- Manifest digest：`c99433976cb1d89a668a8d648923eaea5f95c91cd0a567e22697bb30a4e91e95`
- Index profile digest：`d638a8216deffbf0b19165ba4196ed51025d691a1d0e3ea92e1e05849da45adc`

复现命令：

```powershell
$source = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40"

python -m aikb kb-ingest `
  --db .aikb/linux-full-eval.db `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --source $source `
  --dependency-depth 0

python -m aikb kb-stats --db .aikb/linux-full-eval.db
```

## 快照结果

| 项目 | 结果 |
| --- | ---: |
| 扫描文件 | 70,925 |
| 跳过文件 | 8,363 |
| 输入字节 | 1,002,793,762 |
| 唯一 blob | 70,848 |
| analysis artifact | 70,850 |
| 分析缓存 hit / miss | 75 / 70,850 |
| chunk | 3,970,532 |
| Tree-sitter structured chunk | 3,940,252 |
| fallback chunk | 30,280 |
| symbol occurrence | 5,125,759 |
| relation | 5,098,771 |
| source condition | 126,783 |
| parse status | structured 54,426；fallback 266；not applicable 16,233 |

关系分布：

| 关系 | 数量 |
| --- | ---: |
| `calls` | 4,674,868 |
| `includes` | 348,224 |
| `depends_on_config` | 27,076 |
| `kbuild_contains` | 26,740 |
| `selects_config` | 20,010 |
| `includes_config` | 1,771 |
| `implys_config` | 82 |

置信度分布为 `source_exact` 130,734、`source_inferred` 1,810,085、`ambiguous_candidate` 3,157,952。大量 C 调用仍然只能是候选关系；系统保留歧义，不把静态扫描无法唯一绑定的调用伪装成编译器级事实。

独立校验确认 `source_file`、`chunk`、`chunk_fts`、`symbol_occurrence`、`relation`、`source_condition` 和 distinct file blob 计数全部与 snapshot 声明相同，事件顺序为 `building -> validated -> active`。

## 规模与资源观察

- 最终成功运行从 2026-08-06 00:54:31 到 02:15:35，墙钟时间约 81 分钟；snapshot 在 02:10:00 激活，后续时间用于 SQLite checkpoint 和关闭连接。
- 本地数据库最终约 12.64 GB。
- 未提交 snapshot 事务的 WAL 峰值约 11.92 GB；commit 后 WAL 被回收。
- 峰值工作集约 2.59 GB，private memory 约 2.55 GB。
- 内容寻址 blob 和 analysis artifact 每 250 个文件提交一批；它们可以在 snapshot 失败后安全复用，但 `source_file/chunk/symbol/relation/FTS` 仍只在完整校验后原子可见。

SQLite 单事务产生大 WAL 是本地 bootstrap 的已知成本。生产 worker 仍应使用独立任务编排、资源配额和 PostgreSQL 原子 publisher；本结果不把 12 GB SQLite 文件定义为团队部署方案。

## 全树运行暴露并修复的问题

1. 深层预处理语法树触发 Python 递归上限。结构化 chunk 与 source relation walker 已改为显式栈，并用 1,100 层嵌套预处理块回归测试。
2. 数百万待绑定关系曾保存在 Python 列表。现改为磁盘临时表，关系阶段内存保持稳定。
3. include 解析曾对每条关系遍历全部约 7 万路径。现按 basename 预建索引，7 万路径上的 10,000 次候选解析微基准约 0.07 秒；真实关系阶段写入吞吐提高约一个数量级。
4. 首次失败会回滚所有解析缓存。现将内容寻址缓存按有界批次预热，故障注入测试确认缓存保留而半成品 snapshot 不可见。
5. 关系召回曾使用跨 LEFT JOIN 的 OR，导致每题扫描 5,098,771 条关系。现拆成 source symbol、target symbol、target text 三个索引候选通道；真实查询约 0.010～0.022 秒。
6. 多行 macro occurrence 必须由 chunk 完全包围，导致真正宏定义被漏掉。现改为行范围重叠，并在 SQLite/PostgreSQL adapter 和多行宏测试中保持一致。

## 真实问题自动评测

数据集仍是 10 个用户确认保留的问题、27 个 draft evidence range、18 个唯一证据文件。证据没有经过领域工程师复核，因此报告是工程决策输入，不是冻结黄金集。

| 检索器 | File Recall@10 | Range Recall@10 | File MRR | Range MRR | 完整/部分/未命中 |
| --- | ---: | ---: | ---: | ---: | ---: |
| ripgrep file baseline | 0.5000 | 不适用 | 0.3583 | 不适用 | 3/3/4（按文件） |
| SQLite FTS chunk baseline | 0.5556 | 0.2593 | 0.7000 | 0.5167 | 0/6/4 |
| FTS + symbol + relation RRF | 0.7222 | 0.5926 | 0.8500 | 0.6833 | 5/3/2 |

相对 FTS，RRF 的增量为 File Recall `+0.1667`、Range Recall `+0.3333`、File MRR `+0.1500`、Range MRR `+0.1667`。完整命中题包括 `likely/unlikely`、`container_of`、platform driver、idle task 和中断上下文；仍未命中的主要是 vmalloc/kmalloc 权威文档与 oldconfig 的具体行为行范围。

第一次全树 FTS + hybrid 运行耗时 605.3 秒。评测器现支持严格校验的 lexical cache，只有 schema、Top K、问题 ID、查询文本和 snapshot 全部一致时才可复用；只重算 symbol/relation/RRF 的同一轮评测耗时 0.367 秒。这样后续 vector/reranker 消融不需要反复支付 SQLite FTS 成本。

```powershell
python -m aikb structured `
  --db .aikb/linux-full-eval.db `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --output evals/results/linux-6.18.40-structured.json `
  --markdown-output evals/reports/linux-6.18.40-structured.md `
  --top-k 10

# 只在相同问题、Top K 和 snapshot 上做下游消融时复用 lexical 结果
python -m aikb structured `
  --db .aikb/linux-full-eval.db `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --reuse-lexical-from evals/results/linux-6.18.40-structured.json `
  --output evals/results/linux-6.18.40-structured-ablation.json `
  --markdown-output evals/reports/linux-6.18.40-structured.md `
  --top-k 10
```

完整逐题结果见 [结构化检索自动评测报告](../evals/reports/linux-6.18.40-structured.md)。后续 Qwen3 消融已经证明有界候选语义融合存在增益，但当前数据不足以启用全量向量召回；结论见[语义消融](semantic-ablation.md)与 [ADR-0002](decisions/0002-semantic-candidate-reranking.md)。
