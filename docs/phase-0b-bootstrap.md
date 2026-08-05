# Phase 0B 本地知识库启动骨架

## 当前产出

本步骤实现了一个不依赖外部服务的本地知识库最小切片，用于先验证最重要的数据语义：

```text
Linux source
  -> deterministic file scan
  -> bounded source dependency expansion
  -> content-addressed blob
  -> immutable repository snapshot
  -> Tree-sitter C structure or deterministic fallback chunk
  -> SQLite FTS search
  -> repository@revision:path:start-end citation
```

当前实体包括：

- `repository`：一个逻辑代码仓库。
- `snapshot`：由 revision、源码摘要、实际文件清单摘要和索引配置共同确定的不可变版本。
- `blob`：以 SHA-256 标识并使用 zlib 压缩保存的文件内容，相同内容只保存一次。
- `source_file`：snapshot 中的路径、语言、行数和 blob 映射。
- `chunk`：带类型、符号、起止行、内容哈希和生成器版本的检索单元。
- `snapshot_event`：记录 `building -> validated -> active -> superseded` 状态变化。
- `chunk_fts`：本地 FTS5 全文索引，只承担当前启动阶段的可查询验证。

## 为什么当前使用 SQLite

最终架构仍然是 PostgreSQL/pgvector。当前机器没有 Docker 和 PostgreSQL，本步骤使用 Python 自带的 SQLite，原因是：

1. 先验证 repository/snapshot/blob/file/chunk 的约束、幂等性和结构化解析链路。
2. 让本地源码实验不被基础设施安装阻塞。
3. 给后续 PostgreSQL schema、Tree-sitter 和 Zoekt 提供可执行的输入输出契约。

SQLite 是本地 bootstrap adapter，不是最终团队共享数据库。进入多人共享和向量索引前，仍要把相同领域模型落到 PostgreSQL migration 中。

## 当前切块策略

当前 C/C header 生成器为 `tree-sitter-c-v3`：提取函数定义、声明、typedef、宏和顶层宏调用；函数前连续的注释会并入同一个 chunk，超过 240 行的语法节点会被分段并保留同一符号。

非 C 文件、没有可提取结构的文件或缺少解析依赖时使用 `line-window-v1` fallback：每块默认 120 行、相邻块重叠 20 行。Linux 的宏和条件编译会让 Tree-sitter 报告语法异常，但解析器仍会从 `ERROR` 恢复节点中提取结构；`parse_error_count` 当前表示“存在解析异常的文件数”，不代表这些文件完全解析失败。

当前固定 `tree-sitter==0.25.2` 和 `tree-sitter-c==0.24.2`。实验中 0.26.0 Python binding 在 Windows 解析宏密集的内核文件时发生原生进程异常，因此暂不升级；依赖升级必须重新跑真实 `kernel/sched` 回归。

项目不接入依赖编译数据库的 scip-clang。符号和关系能力通过直接扫描标识符、include、Kconfig/Kbuild、预处理条件和调用表达式实现；无法唯一确定的关系保存为带置信度的候选。扫描器会从种子路径沿显式源码依赖做有界扩展，scope 默认限制深度 1、最多增加 500 个文件、每条引用最多保留 8 个候选。

## 亲自运行第一个实验

在项目根目录执行：

```powershell
python -m pip install -e .

$source = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40"
$db = ".aikb/linux-sched.db"

python -m aikb kb-ingest `
  --db $db `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --source $source `
  --include kernel/sched

python -m aikb kb-stats --db $db

python -m aikb kb-search `
  --db $db `
  --query "init_idle do_idle" `
  --top-k 5

python -m aikb kb-symbol `
  --db $db `
  --name init_idle `
  --top-k 20

python -m aikb kb-retrieve `
  --db $db `
  --query "init_idle do_idle" `
  --top-k 10

python -m aikb kb-context `
  --db $db `
  --query "init_idle do_idle" `
  --max-evidence-items 6 `
  --evidence-token-budget 1200
```

第一次运行会创建数据库和 active snapshot；同样的命令再次运行应返回 `"idempotent": true`，而不是重复创建数据。

`.aikb/` 已加入 `.gitignore`。这是本机派生产物，不提交到 GitHub。

## 已完成的真实源码验证

对 Linux 6.18.40 `kernel/sched` 的验证结果：

| 项目 | 结果 |
| --- | --- |
| 默认配置 active snapshot | `snap_0b0e8c0e71ad7f720c31b8e2` |
| revision | `release:6.18.40` |
| 文件 | 218（46 个种子 + 172 个一层依赖） |
| 唯一 blob | 212 |
| chunk | 13,486 |
| Tree-sitter 结构化 chunk | 13,445 |
| fallback chunk | 41 |
| 含解析异常但仍可恢复结构的文件 | 113 |
| symbol occurrence | 15,960 |
| source-only relation | 16,532 |
| 条件范围 | 2,206 |
| 依赖引用诊断 | 未解析 5；歧义 9；单引用候选预算触发截断 |
| 调用目标解析率 | `40.11% -> 50.94%` |
| 调用歧义比例 | `60.62% -> 49.78%` |
| blob 分析缓存 | 改变依赖文件预算后 218 hit，0 miss |
| 输入字节 | 3,473,293 |
| 重复运行 | 返回相同 snapshot，`idempotent=true` |
| 示例定义引用 | `linux@release:6.18.40:kernel/sched/core.c:7969-8027`，符号为 `init_idle` |

`kb-symbol --name init_idle` 能同时返回 `include/linux/sched/task.h:64` 的声明、`kernel/sched/core.c:7969-8027` 的定义、`sched_init -> init_idle` 入边和 `init_idle` 内部调用候选；只在当前扫描范围中找到唯一目标的调用标记为 `source_inferred`，范围外或多候选目标标记为 `ambiguous_candidate`。C header 与 C implementation 目前仍保留不同 logical symbol ID，后续只能在签名、定义数量和条件兼容性足够时归并，不能仅凭同名产生虚假唯一关系。

schema v4 增加 `analysis_artifact`：以 `blob SHA-256 + language + analysis_profile_digest` 唯一标识压缩后的 chunk、occurrence、condition 和 relation 原始分析产物；schema v5 增加种子/依赖文件数和依赖诊断统计。默认扩展首次验证为 52 hit/166 miss；只改变 snapshot 的依赖文件预算后为 218 hit/0 miss。冷扫描现在每 250 个文件提交一批内容寻址 blob 与 analysis artifact；已完成批次可在 snapshot 失败后复用，但 snapshot 级路径、引用、关系和 FTS 仍会在完整校验后原子激活。扫描范围和预算不进入 blob analysis profile。

如果同一个不可变 snapshot 已存在但当前为 `superseded`，再次运行对应 ingest 会原子重新激活它并记录 snapshot event，不会重新解析或复制数据。

## 当前还不能声称什么

- 已完成固定 scope 的 Linux 6.18.40 全树扫描：70,925 个文件、3,970,532 个 chunk、5,125,759 个 occurrence、5,098,771 条关系；详见[全树验证](full-tree-validation.md)。这证明了单机 source-only 路径可运行，不代表已经具备后台任务编排、增量调度和生产 SLA。
- 已实现第一版 source-only occurrence、预处理条件、include、Kconfig/Kbuild、调用候选、有界依赖扩展和按 blob 复用的分析缓存；复杂变量展开与声明/定义安全归并仍需扩展。
- 项目明确不生成 build profile、不依赖 `.config` 或 `compile_commands.json`，也不执行 Linux 构建。
- 已有 PostgreSQL/pgvector schema、原子 publisher、Zoekt lexical adapter 和 lexical/symbol/relation RRF；vector 通道仍待效果评测。
- 已有 Context Pack v1.3 和跨仓 partial-visibility PoC；还没有 MCP、正式身份权限与多人共享服务。

因此目前是“第一版可运行的本地知识目录与检索骨架”，不是已经完成的团队知识库。
