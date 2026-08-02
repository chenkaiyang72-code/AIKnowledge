# AIKnowledge

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

python -m aikb kb-symbol --name init_idle --top-k 20
```

默认数据库位于 `.aikb/catalog.db`。`kb-symbol` 返回定义/声明、调用和 include 等直接扫描关系，并明确标注 `source_exact`、`source_inferred` 或 `ambiguous_candidate`。依赖扩展默认读取 scope 中的深度、文件数和每条引用候选数预算；只改变扫描预算不会使未变化源码的分析缓存失效。SQLite 只用于本地 bootstrap；团队共享主库仍按设计使用 PostgreSQL/pgvector。

## 核心原则

- 回答必须能追溯到代码版本、稳定源码锚点和展示行号。
- 索引只能直接扫描仓库内容，不执行构建脚本，不依赖 `.config`、`compile_commands.json` 或可成功编译的环境。
- 跨仓方案通过一致的 `solution snapshot` 固定各仓库版本，避免混用不兼容代码。
- 每次提问都产生学习信号，但未经验证的 AI 答案不能直接成为正式知识。
- 精确代码索引、全文检索、向量检索和关系检索共同工作，不能只依赖向量数据库。
- 通过标准 MCP 接口服务不同 AI 客户端，避免绑定单一模型或编辑器。
