# 团队身份、仓库授权与 RLS

## 当前范围

PostgreSQL schema v4 增加身份/授权基础，schema v5 增加 token 撤销、认证目录角色和 MCP 审计，schema v6 将授权主体与授权来源分离：

| 表 | 作用 |
| --- | --- |
| `security_domain` | 团队/组织级安全与数据策略边界 |
| `principal` | 由 OIDC issuer + subject 唯一映射的用户或服务身份 |
| `security_team` | domain 内的协作团队 |
| `security_team_member` | principal 与 team 的成员关系 |
| `repository_grant` | repository 对 principal 或 team 的带时效授权 |
| `repository_grant_source` | manifest/manual/GitHub/GitLab 等可独立撤销的授权来源 |
| `mcp_audit_event` | principal 隔离的 metadata-only tool 调用审计 |

`repository_grant` 的 principal/team 必须二选一；schema v6 的 RLS 只有在至少一个 `repository_grant_source` 未撤销且未过期时才放行。同一 grantee 的 manifest、manual、GitHub 或 GitLab 来源形成并集，撤销一个来源不影响其他来源；暂停 principal/team/domain 仍会阻断全部来源。授权变化只影响后续查询事务，不复制源码、不重建 snapshot。

## 查询前强制

`PostgresCatalog` 收到 `PostgresPrincipalContext` 后，每个读取事务先执行：

```sql
SET LOCAL ROLE aikb_reader;
SELECT set_config('aikb.principal_id', :principal_id, true);
SELECT set_config('aikb.security_domain_id', :security_domain_id, true);
```

随后普通 repository/snapshot/chunk 查询由 RLS 自动过滤。`resolve_postgres_solution_scope` 使用同一事务边界；solution 有部分成员可见时只返回可见 member，并把 `partial_visibility` 设为 true。

`aikb_reader` 是 cluster 级 NOLOGIN group role，迁移会拒绝复用带 LOGIN、SUPERUSER 或 BYPASSRLS 属性的同名角色。生产 service login 只能被授予该 role，并且不能拥有业务表。publisher 使用独立 owner/write credential，不向 MCP 暴露。

## 已覆盖的数据

- repository、snapshot 与状态事件；
- blob、analysis artifact、source file、chunk；
- logical symbol、occurrence、condition、relation；
- chunk embedding；
- solution、solution snapshot、事件和 member；
- retrieval trace 的当前 principal/domain 读写隔离。

`schema_metadata` 和 `embedding_model` 只包含全局 schema/model 元数据，可由 reader 读取。安全域、principal、team、membership、grant 和 grant source 表不授予 reader 直接查询权限；RLS 通过固定 search path 的 SECURITY DEFINER 函数做最小布尔判断。

## 运维约束

- 远程服务不得使用 migration/publisher URL。
- principal/domain 只来自已经验证的 token 与服务端目录映射，不能来自 MCP tool 参数或普通 header。
- 自定义 PostgreSQL GUC 是应用到数据库的传递载体，不是独立认证凭据；网络客户端没有 SQL 或 set_config 接口。
- 连接归还池前事务必须结束；使用 `SET LOCAL` 保证身份不会跨请求残留。
- RLS migration downgrade 不删除 cluster 级 role，但会撤销本项目表和函数权限。

## 验证门槛

集成测试使用真正的非 owner `aikb_reader`，建立 Alice/team 与 Bob/direct grant：Alice 只能看到 visible repository/chunk/solution member，Bob 只能看到 hidden 一侧，错误 domain 得到零行；同一测试还验证 secured `PostgresCatalog`、solution partial visibility 和只允许 principal 写自己的 retrieval trace。

OIDC verifier、JWT audience/scope/expiry/not-before/revocation、MCP 401/Protected Resource Metadata 与 metadata-only audit 已在共享 PostgreSQL CI 完成 57/57 终验，见 [run 31049508639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31049508639)。domain/principal/team/grant 由原子、增量、无隐式删除的 security manifest 管理，见[安全管理文档](security-admin.md)；远程部署方式见[远程 MCP 文档](remote-mcp-auth.md)。
