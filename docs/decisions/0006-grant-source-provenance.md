# ADR-0006：仓库授权主体与授权来源分离

状态：Accepted（2026-08-06）

## 背景

同一个 principal/team 可能同时因人工 manifest、break-glass、GitHub team 或 GitLab group 获得同一 repository 的权限。只在 `repository_grant` 增加一个 `source` 字段会造成单一所有者：GitHub 撤权时可能误伤人工授权，人工 manifest 也可能覆盖 connector 权限。

## 决策

1. `repository_grant` 只保存 domain/repository/grantee 的稳定授权主体，一组唯一 grantee 仍只有一个父对象。
2. schema v6 新增 `repository_grant_source`；每个来源独立保存 `source_kind + source_key`、permission、source revision、observed time、expiry 和 revocation。
3. RLS 的可见性函数只在至少一个 source 当前有效时返回 true；不同来源形成并集，撤销一个来源不影响其他来源。
4. migration 把所有 schema v5 grant 保守回填成 `legacy` source，不猜测历史所有者。legacy source 必须由管理员审查后再迁移或撤销。
5. security manifest 只 upsert 自己的 `manifest + grant_source_key`；digest 写入 `source_revision`。GitHub planner显示 active source kinds，但在 connector 游标、apply 审计和恢复流程完成前仍保持只读。

## 结果

- 外部 ACL 同步可以最终做到 source-scoped revoke，不会误伤 manual/manifest grant。
- 父表的 permission/expiry/revocation 暂时保留兼容信息，但 RLS 从 v6 起以 source 表为权威；直接只插父表不会产生访问权。
- downgrade 会把所有当前有效 source 保守聚合回父 grant：权限取最高、有效期取覆盖范围最大的值；这是有损降级，生产执行前必须备份与审查。
