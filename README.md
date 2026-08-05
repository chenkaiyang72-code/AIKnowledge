# AIKnowledge

[![test](https://github.com/chenkaiyang72-code/AIKnowledge/actions/workflows/test.yml/badge.svg)](https://github.com/chenkaiyang72-code/AIKnowledge/actions/workflows/test.yml)

面向研发团队的、可被 AI 操作并能持续进化的代码知识库。

项目希望把分散在代码、设计文档、历史问答和工程师经验中的知识，整理成带版本、来源和权限的共享上下文，并通过 MCP 提供给 Cursor、Claude Code、GitHub Copilot 等 AI 客户端。

## 当前阶段

项目已进入 Phase 0B 结构化检索阶段；问题集已保留，按当前决定暂缓人工复核：

- [项目总体设计](docs/architecture.md)
- [技术蓝图：架构、技术与开源组件映射](docs/technical-blueprint.md)
- [具体实施计划](docs/implementation-plan.md)
- [项目进展：已完成与未完成事项](docs/progress.md)
- [首个实验：Linux 6.18.40 lexical baseline](evals/datasets/linux-6.18.40/README.md)
- [Phase 0B：本地知识库启动骨架](docs/phase-0b-bootstrap.md)
- [Context Pack v1：AI 客户端证据契约](docs/context-pack-v1.md)
- [本地混合检索 v1：lexical/symbol/relation RRF](docs/hybrid-retrieval.md)
- [PostgreSQL/pgvector schema v1](docs/postgres-schema-v1.md)
- [Zoekt source-only 索引与检索适配器](docs/zoekt-adapter.md)
- [Linux 6.18.40 全树 source-only 验证](docs/full-tree-validation.md)
- [结构化检索自动评测报告](evals/reports/linux-6.18.40-structured.md)

首版技术路线：Python 模块化单体与独立 worker，使用 PostgreSQL/pgvector 保存元数据和向量，使用 Tree-sitter、源码标识符/关系提取器和 Zoekt 建立无需编译的代码索引，并通过只读 MCP 网关向不同 AI 客户端提供带版本和引用的 Context Pack。

## 当前可运行能力

Phase 0B 已有一个本地启动后端，可以把固定源码范围扫描为不可变 snapshot，使用 Tree-sitter 生成 C 结构化 chunk，并沿源码中的 include/Kconfig/Kbuild 关系做有深度和文件预算的依赖扩展；检索结果带版本、符号和行号：

```powershell
python -m pip install -e .

$source = "C:\Users\yangc\Desktop\开开个人\AIstart\linux\linux-6.18.40"

python -m aikb kb-ingest `
  --scope evals/datasets/linux-6.18.40/scope.json `
  --source $source `
  --include kernel/sched

python -m aikb kb-search --query "init_idle do_idle" --top-k 5

python -m aikb kb-retrieve --query "init_idle do_idle" --top-k 10

python -m aikb kb-symbol --name init_idle --top-k 20

python -m aikb kb-context `
  --query "init_idle do_idle" `
  --max-evidence-items 6 `
  --evidence-token-budget 1200
```

默认数据库位于 `.aikb/catalog.db`。`kb-symbol` 返回定义/声明、调用和 include 等直接扫描关系，并明确标注 `source_exact`、`source_inferred` 或 `ambiguous_candidate`。`kb-retrieve` 用确定性 RRF 融合 lexical、精确 symbol 和直接 relation 三个通道；`kb-context` 把融合结果输出为带不可变 snapshot、blob/chunk 哈希、稳定 citation、预算和 retrieval trace 的 Context Pack v1.2，没有证据时明确返回 gap。依赖扩展默认读取 scope 中的深度、文件数和每条引用候选数预算；只改变扫描预算不会使未变化源码的分析缓存失效。冷扫描先按有界批次持久化内容寻址分析缓存，完整 snapshot 仍只在计数校验后原子激活。SQLite 只用于本地 bootstrap；团队共享主库仍按设计使用 PostgreSQL/pgvector。

PostgreSQL schema 和 migration 可选安装：

```powershell
python -m pip install -e ".[postgres]"
$env:AIKB_POSTGRES_URL = "postgresql+psycopg://aikb:aikb@127.0.0.1:5432/aikb"
python -m alembic upgrade head

# 将本地已经验证的 active snapshot 原子发布到团队数据库
python -m aikb kb-publish-postgres --db .aikb/catalog.db

# 本地有多个仓库时，明确指定要发布的不可变 snapshot
python -m aikb kb-publish-postgres `
  --db .aikb/catalog.db `
  --snapshot-id snap_0b0e8c0e71ad7f720c31b8e2

# 仅查看 migration SQL，不连接数据库
python -m alembic upgrade head --sql
```

发布器按有界批次复制静态扫描产物，在单个 PostgreSQL 事务内校验计数并执行 `building -> validated -> active`。每个仓库使用事务级 advisory lock；重复发布是幂等的，发布失败整体回滚，切换 active snapshot 不使用 force。数据库连接串优先放在 `AIKB_POSTGRES_URL`，不要提交到仓库。

正式 lexical 通道可连接预构建 Zoekt 服务。先从已经验证的 catalog snapshot 导出只包含源码的不可变目录，再由禁用 ctags 的 Zoekt indexer 建索引；整个过程不运行源码仓库脚本，也不编译 Linux：

```powershell
python -m aikb kb-zoekt-export `
  --db .aikb/catalog.db `
  --snapshot-id snap_0b0e8c0e71ad7f720c31b8e2 `
  --output .aikb/zoekt/linux-sched

$env:AIKB_ZOEKT_URL = "http://127.0.0.1:6070"
python -m aikb kb-retrieve `
  --query "init_idle do_idle" `
  --zoekt-required
```

Zoekt 容器的固定版本、索引和启动命令见[运行文档](docs/zoekt-adapter.md)。未设置 `AIKB_ZOEKT_URL` 时使用 catalog FTS；设置后若服务暂时不可用，默认回退到 SQLite/PostgreSQL FTS，生产验收可用 `--zoekt-required` 禁止回退。

## 全树评测

Linux 6.18.40 固定 scope 已完成 70,925 文件的 source-only 全树扫描，生成 3,970,532 个 chunk、5,125,759 个 symbol occurrence 和 5,098,771 条关系。10 个真实问题上的无向量 hybrid 基线为 File Recall@10 `0.7222`、Evidence Range Recall@10 `0.5926`、File MRR `0.8500`、Range MRR `0.6833`；完整细节和资源数据见[全树验证文档](docs/full-tree-validation.md)。

```powershell
python -m aikb structured `
  --db .aikb/linux-full-eval.db `
  --questions evals/datasets/linux-6.18.40/questions.first-batch.jsonl `
  --output evals/results/linux-6.18.40-structured.json `
  --markdown-output evals/reports/linux-6.18.40-structured.md `
  --top-k 10
```

SQLite FTS 全树运行约需 10 分钟；正式 lexical 通道使用 Zoekt。后续只调整 symbol/relation/vector/reranker 时，可以使用 `--reuse-lexical-from` 复用经过 schema、问题、查询、Top K 和 snapshot 校验的 lexical 结果。

## 核心原则

- 回答必须能追溯到代码版本、稳定源码锚点和展示行号。
- 索引只能直接扫描仓库内容，不执行构建脚本，不依赖 `.config`、`compile_commands.json` 或可成功编译的环境。
- 跨仓方案通过一致的 `solution snapshot` 固定各仓库版本，避免混用不兼容代码。
- 每次提问都产生学习信号，但未经验证的 AI 答案不能直接成为正式知识。
- 精确代码索引、全文检索、向量检索和关系检索共同工作，不能只依赖向量数据库。
- 通过标准 MCP 接口服务不同 AI 客户端，避免绑定单一模型或编辑器。
