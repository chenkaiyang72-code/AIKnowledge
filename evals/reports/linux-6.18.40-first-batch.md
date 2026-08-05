# Linux 6.18.40 第一批真实问题基线报告

运行日期：2026-08-06
数据集：`questions.first-batch.jsonl`
检索器：ripgrep fixed-string，按“命中检索词数量，再按命中次数”排序
Top K：10

## 结论

第一批包含 10 个来自公开真实提问的问题、27 处源码证据标注，对应 18 个唯一证据文件。用户已确认 10 个问题全部保留；所有证据路径和行号都已通过自动校验，但证据内容仍是 Codex 初查结果，尚未经过领域工程师确认，因此当前不是冻结黄金集。

本轮检索结果：

- Evidence Recall@10：`0.5000`（18 个证据文件找回 9 个）
- MRR：`0.3583`
- 证据文件全部找回：3 题
- 证据文件部分找回：3 题
- 证据文件未找回：4 题

这个结果验证了普通文本检索的主要缺陷：当标识符在内核中大量出现时，调用点和重复文档会淹没定义、核心实现和约束检查。后续结构化检索需要显式提高“定义、类型、实现、权威文档”的权重。

## 逐题结果

| 问题 | 结果 | 关键现象 |
| --- | --- | --- |
| `linux-web-001` likely/unlikely | 未找回 | `likely` 和 `unlikely` 命中过多并被截断，调用点排在 `include/linux/compiler.h` 前面 |
| `linux-web-002` vmalloc/kmalloc | 未找回 | 同时出现两个词的实现和测试文件得分更高，内存分配文档未进入前 10 |
| `linux-web-003` container_of | 未找回 | 高频宏调用和相关头文件命中压过 `include/linux/container_of.h` |
| `linux-web-006` oldconfig | 全部找回 | 将 `scripts/` 纳入固定范围后，`scripts/kconfig/conf.c` 排名第 1 |
| `linux-web-008` __user | 部分找回 | 找回 `include/linux/uaccess.h`，未找回定义地址空间约束的 `compiler_types.h` |
| `linux-web-011` platform driver | 全部找回 | 三个精确符号共同命中，`drivers/base/platform.c` 排名第 1 |
| `linux-web-013` EXPORT_SYMBOL | 未找回 | 大量导出调用点占满前 10，宏定义和模块装载限制均未进入前 10 |
| `linux-web-015` idle task | 全部找回 | `idle.c` 排名第 2，`core.c` 排名第 7 |
| `linux-web-020` 调度策略 | 部分找回 | 找回 `core.c` 和 `rt.c`，策略常量头文件未进入前 10 |
| `linux-web-023` 中断上下文睡眠 | 部分找回 | 找回 `kernel.h` 和 `core.c`，`preempt.h` 未进入前 10 |

## 对 Phase 0B 的直接要求

下一版检索不能只统计字符串出现次数，至少需要：

1. 标识符定义优先于普通引用。
2. 调用者可以沿定义、实现、类型和调用关系扩展上下文。
3. 对 `include/` 中的定义和 `Documentation/` 中的权威说明设置独立召回通道，最后用 RRF 合并，而不是简单提高固定目录权重。
4. 高频词必须有稳定的召回上限和去重策略，避免按文件遍历顺序截断后漏掉定义。
5. 每次返回证据时保留 snapshot、路径、行号、符号和检索轨迹。

## 复现命令

在项目根目录执行：

```powershell
$env:PYTHONPATH = "src"
$source = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40"

python -m aikb baseline `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --source $source `
  --output evals/results/linux-6.18.40-first-batch-scope-v2.json
```

`evals/results/` 保存本机生成的逐条原始结果并由 Git 忽略；本报告保存可审阅和可追踪的结论。
