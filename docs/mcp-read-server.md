# 只读 MCP 服务

## 当前结论

Phase 1A 使用官方 MCP Python SDK `mcp==2.0.0`。它已于 2026-07-28 正式 GA，而不是此前设计时看到的 release candidate；PyPI 的默认稳定版本和 GitHub latest release 均为 2.0.0。SDK v2 同时兼容 2026-07-28 无会话协议与旧客户端初始化流程，因此不再为新实现锁定维护线 v1。

依据：

- [MCP Python SDK v2.0.0 release](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0)
- [MCP Python SDK installation](https://py.sdk.modelcontextprotocol.io/installation/)
- [MCP Python SDK v2 changes](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md)

MCP 只是现有证据服务的协议适配器，不重新实现检索，也不运行目标仓库代码。安装：

```powershell
python -m pip install -e ".[mcp]"
```

MCP 服务以 SQLite `mode=ro&immutable=1` URI 和 `PRAGMA query_only` 打开本地 catalog，只校验现有 schema，不建库、不迁移、不创建 WAL。该本地 catalog 必须先完成发布并停止写入，不能与 scanner/publisher 并发；团队常驻服务将在 Phase 1B 使用 PostgreSQL reader 和事务发布。

## 工具契约

服务只公开三个工具：

| 工具 | 作用 | 返回 |
| --- | --- | --- |
| `aikb_scope_resolve` | 把一个 repository 或 solution 解析为可见、不可变 snapshot scope | Context Pack v1.3 的 `ContextScope` |
| `aikb_context_search` | 在一个固定 repository/solution scope 内构造确定性证据包 | 完整 `ContextPack` |
| `aikb_context_get` | 按 repository、snapshot、path、line 读取包含该位置的权威 chunk | 稳定 citation、hash 与源码正文 |

三者都声明 MCP `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true`、`openWorldHint=false`。服务没有 publish、feedback、shell、文件写入或任意 SQL 工具。

`aikb_context_search` 在协议 schema 上限制：问题最多 2,000 字符、evidence 最多 20 条、证据预算 64～12,000 近似 token、symbol 最多 10 个、每 symbol relation 最多 20 条。底层 Context Pack 继续执行更严格的确定性条数/正文预算。`aikb_context_get` 拒绝绝对路径和 `..` 路径，只能读取 catalog 中已索引的 repository-relative source location。

源码正文被服务端 instructions 明确标记为不可信数据，不得被 AI 当成工具指令。这个边界不能彻底消除 prompt injection，但可以防止 MCP server 自身把仓库内容解释为命令。

## 本地 stdio

stdio 只把 MCP 协议写到标准输出，适合单机 Cursor/Claude Code，且不会开放监听端口：

```powershell
python -m aikb mcp-serve `
  --db .aikb/catalog.db `
  --transport stdio
```

Cursor 在项目的 `.cursor/mcp.json` 中配置：

```json
{
  "mcpServers": {
    "aiknowledge": {
      "command": "python",
      "args": [
        "-m",
        "aikb",
        "mcp-serve",
        "--db",
        "C:/absolute/path/to/AIKnowledge/.aikb/catalog.db"
      ]
    }
  }
}
```

Cursor 官方支持 stdio 与 Streamable HTTP，并从 `.cursor/mcp.json` 或全局 `~/.cursor/mcp.json` 读取配置：[Cursor MCP documentation](https://docs.cursor.com/context/model-context-protocol)。

Claude Code 本地配置：

```powershell
claude mcp add --transport stdio aiknowledge -- `
  python -m aikb mcp-serve `
  --db "C:/absolute/path/to/AIKnowledge/.aikb/catalog.db"

claude mcp get aiknowledge
```

Claude Code 要求用 `--` 分隔自身参数与 server command；HTTP 是推荐的远程传输，SSE 已弃用：[Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)。

2026-08-06 已使用 Claude Code `2.1.222` 在隔离配置中执行真实 stdio 健康检查，`aiknowledge` 状态为 `Connected`；验证结束后未改动用户现有 Claude 配置。Cursor 已发现本机安装，但其 CLI 不提供等价的 MCP 健康检查，仍需在 UI 中完成最终调用验收。

## 本机 Streamable HTTP

```powershell
python -m aikb mcp-serve `
  --db .aikb/catalog.db `
  --transport streamable-http `
  --host 127.0.0.1 `
  --port 8000 `
  --path /mcp/read
```

当前服务使用 `stateless_http=true`、JSON response 和 1 MiB 请求体上限。因为 Phase 1A 还没有认证，CLI 强制 HTTP 只能绑定 `127.0.0.1`、`localhost` 或 `::1`；传入 `0.0.0.0` 或远程地址会直接失败。团队远程地址必须等 Phase 1B 接入 OIDC/OAuth、token audience 校验、repository ACL 与 RLS 后开放。

不要用固定 header 把其他服务的 token 原样透传。MCP 官方安全指南明确禁止 token passthrough，并要求远程 server 验证 token 是签发给自身 audience 的：[MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)。

## PoC 可见性

`--allow-repository` 可重复传入，为本地客户端模拟 repository allow-set：

```powershell
python -m aikb mcp-serve `
  --db .aikb/catalog.db `
  --allow-repository linux-kernel `
  --transport stdio
```

直接请求未允许 repository 会得到通用工具错误；solution 只检索可见成员，并按 Context Pack v1.3 返回 `partial_visibility=true`。部分可见响应不输出完整 solution snapshot ID、manifest digest、隐藏成员名称、snapshot、正文、symbol、跨仓 link 或候选统计。

这只是应用层 test double，不能作为远程多用户安全边界。正式服务必须由已验证 principal 生成 allow-set，并在数据库查询前通过 RLS 再次强制。

## 自动验证

`tests/test_mcp_server.py` 当前覆盖：

- v2 SDK 内存客户端发现且只能发现三个只读工具；
- repository 与 solution Context Pack 的 structured output 可由严格 Pydantic schema 重新验证；
- `context.get` citation 可回到精确 repository/revision/path/lines；
- 隐藏 repository、路径穿越和未授权 scope 返回工具错误且不泄露隐藏内容；
- 连续 100 次协议调用结果稳定、数据库及目录不发生写入；
- 不存在的数据库不会被自动创建，内部路径和底层错误不会透传给客户端；
- 真实 stdio 子进程完成工具发现；
- 真实 stateless Streamable HTTP 完成发现与调用；
- 未认证 HTTP 拒绝非 loopback binding。

这些测试验证协议和本机传输；Claude Code 已通过真实健康连接，但还不等于 Cursor/Claude agent 最终回答验收，也不包含远程 OAuth。下一步会完成客户端调用矩阵，并把远程认证留在 Phase 1B 安全边界内。
