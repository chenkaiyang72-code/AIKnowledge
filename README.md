# AIKnowledge

面向研发团队的、可被 AI 操作并能持续进化的代码知识库。

项目希望把分散在代码、设计文档、历史问答和工程师经验中的知识，整理成带版本、来源和权限的共享上下文，并通过 MCP 提供给 Cursor、Claude Code、GitHub Copilot 等 AI 客户端。

## 当前阶段

项目已进入 Phase 0A 评测基线阶段：

- [项目总体设计](docs/architecture.md)
- [技术蓝图：架构、技术与开源组件映射](docs/technical-blueprint.md)
- [具体实施计划](docs/implementation-plan.md)
- [项目进展：已完成与未完成事项](docs/progress.md)
- [首个实验：Linux 6.18.40 lexical baseline](evals/datasets/linux-6.18.40/README.md)

首版技术路线：Python 模块化单体与独立 worker，使用 PostgreSQL/pgvector 保存元数据和向量，使用 Tree-sitter、SCIP/scip-clang 和 Zoekt 建立代码索引，并通过只读 MCP 网关向不同 AI 客户端提供带版本和引用的 Context Pack。

## 核心原则

- 回答必须能追溯到代码版本、稳定源码锚点和展示行号。
- 跨仓方案通过一致的 `solution snapshot` 固定各仓库版本，避免混用不兼容代码。
- 每次提问都产生学习信号，但未经验证的 AI 答案不能直接成为正式知识。
- 精确代码索引、全文检索、向量检索和关系检索共同工作，不能只依赖向量数据库。
- 通过标准 MCP 接口服务不同 AI 客户端，避免绑定单一模型或编辑器。
