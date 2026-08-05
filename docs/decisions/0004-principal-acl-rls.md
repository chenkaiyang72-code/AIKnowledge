# ADR-0004：principal、团队授权与 PostgreSQL RLS

状态：已接受（数据库集成 CI 待终验）
日期：2026-08-06

## 背景

Phase 0C 的 `allowed_repositories` 只能证明 Context Pack 在得到正确允许集合后不会主动输出隐藏成员，不能作为多人服务的安全边界。客户端参数、应用过滤和检索后裁剪都可能被遗漏；源码正文、symbol、relation、solution member、embedding 与审计也必须使用同一权限语义。

## 决策

1. OIDC 身份映射为稳定 `principal`，每个 principal 必须属于一个 `security_domain`；团队通过 `security_team` 和 `security_team_member` 表达。
2. `repository_grant` 只允许一个 principal 或一个 team 作为 grantee，支持 read/write/admin、到期和撤销；跨 domain grant 不会生效。
3. 网络请求不得接受客户端传入的仓库 allow-set。认证层只产生 principal/domain，随后每个 PostgreSQL 读取事务执行 `SET LOCAL ROLE aikb_reader` 和事务级 `set_config`。
4. `aikb_reader` 必须是 `NOLOGIN`、`NOSUPERUSER`、`NOBYPASSRLS`。迁移 owner/publisher 不复用该读取角色；远程服务账号不得拥有表、不得是 superuser 或 BYPASSRLS。
5. repository、snapshot、blob/file/chunk、symbol/relation、embedding 和 solution 表启用 RLS。solution snapshot 在至少一个成员可见时可解析，但 member 行仍逐仓过滤，并根据不可见总成员产生 `partial_visibility`。
6. `retrieval_trace` 只允许当前 principal/domain 插入和读取自己的记录，不允许 reader 更新或删除。
7. 策略不使用 `FORCE ROW LEVEL SECURITY`，让受控 migration/publisher owner 保持原子写入；安全验收必须以非 owner 的 `aikb_reader` 角色执行，不能用 owner 查询冒充 RLS 测试。

## 后果

- 即使某条应用 SQL 忘记 repository 条件，数据库也会在召回前过滤。
- grant/membership 变更对下一事务生效，不需要重建索引。
- 内容寻址 blob 被多个仓库复用时，只要 principal 至少能读取一个引用它的 source file 才可见。
- 当前只完成数据库身份与授权基础；远程 HTTP 仍保持关闭，必须再完成 JWT signature/issuer/audience/expiry/revocation 校验、Protected Resource Metadata 和真实越权 CI。

## 不采用

- 不使用客户端声明 allow-set：它不是认证结果。
- 不只在 Context Pack 输出阶段删除隐藏结果：检索、排序和 trace 已经可能泄露。
- 不把每个团队复制成独立索引：版本与内容去重成本过高，权限变更也会触发不必要重建。
- 不让网络读取服务使用表 owner：PostgreSQL owner 默认绕过 RLS。
