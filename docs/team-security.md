# 团队身份、仓库授权与 RLS

## 当前范围

PostgreSQL schema v4 增加五张安全表：

| 表 | 作用 |
| --- | --- |
| `security_domain` | 团队/组织级安全与数据策略边界 |
| `principal` | 由 OIDC issuer + subject 唯一映射的用户或服务身份 |
| `security_team` | domain 内的协作团队 |
| `security_team_member` | principal 与 team 的成员关系 |
| `repository_grant` | repository 对 principal 或 team 的带时效授权 |

`repository_grant` 的 principal/team 必须二选一；撤销或过期 grant、暂停 principal/team/domain 均不会通过可见性函数。授权变化只影响后续查询事务，不复制源码、不重建 snapshot。

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

`schema_metadata` 和 `embedding_model` 只包含全局 schema/model 元数据，可由 reader 读取。安全域、principal、team、membership 和 grant 表不授予 reader 直接查询权限；RLS 通过固定 search path 的 SECURITY DEFINER 函数做最小布尔判断。

## 运维约束

- 远程服务不得使用 migration/publisher URL。
- principal/domain 只来自已经验证的 token 与服务端目录映射，不能来自 MCP tool 参数或普通 header。
- 自定义 PostgreSQL GUC 是应用到数据库的传递载体，不是独立认证凭据；网络客户端没有 SQL 或 set_config 接口。
- 连接归还池前事务必须结束；使用 `SET LOCAL` 保证身份不会跨请求残留。
- RLS migration downgrade 不删除 cluster 级 role，但会撤销本项目表和函数权限。

## 验证门槛

集成测试使用真正的非 owner `aikb_reader`，建立 Alice/team 与 Bob/direct grant：Alice 只能看到 visible repository/chunk/solution member，Bob 只能看到 hidden 一侧，错误 domain 得到零行；同一测试还验证 secured `PostgresCatalog`、solution partial visibility 和只允许 principal 写自己的 retrieval trace。

远程 HTTP 仍未开放。下一步是固定 OIDC verifier、JWT audience/scope/expiry/not-before/revocation、MCP 401/Protected Resource Metadata，然后把认证后的 principal 接到本 RLS 事务。
