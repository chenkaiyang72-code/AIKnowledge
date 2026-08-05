# ADR-0003：MCP v2 只读接入

- 状态：Accepted
- 日期：2026-08-06

## 背景

早期设计记录时 MCP Python SDK v2 仍是 release candidate，因此计划锁定 v1.28。到实现阶段，官方 `v2.0.0` 已于 2026-07-28 GA，PyPI 默认稳定线也已切换到 2.0.0。继续新写 v1 服务会立即产生维护债务，并错过 v2 的无会话协议与统一客户端测试能力。

AIKnowledge 的首个 MCP 目标只是把已经验证的 repository/solution resolver 和 Context Pack 暴露给 Cursor、Claude Code 等客户端。它不需要 sampling、elicitation、客户端 roots 或写工具。

## 决策

1. 可选依赖固定为 `mcp==2.0.0`，不使用浮动范围或 pre-release。
2. 只公开 `aikb_scope_resolve`、`aikb_context_search`、`aikb_context_get` 三个 read-only tools。
3. 同时提供 stdio 与 stateless Streamable HTTP；SSE 不进入新实现。
4. Phase 1A 未认证 HTTP 只允许 loopback binding，不开放远程团队访问。
5. 工具返回现有严格、版本化 Pydantic models；不为不同客户端产生不同证据格式。
6. repository source content 始终是不可信数据，不能触发 server-side 命令、采样、写入或外部请求。
7. 远程 OAuth、token audience、ACL、RLS 与审计统一在 Phase 1B 实现，不用静态 header 或 token passthrough 假装完成认证。

## 结果

- 新客户端可以使用 v2 协议，SDK 仍为旧 MCP 客户端保留兼容路径。
- stdio 能立即用于单机实验；HTTP 入口已经过真实传输测试，但在认证完成前不会暴露到网络。
- MCP adapter 保持很薄，检索质量、版本固定和 partial visibility 仍由同一领域代码决定。
- Cursor/Claude Code 人工 UI 兼容矩阵、远程认证和正式多用户部署仍是明确的后续工作，不因协议测试通过而被标成完成。
