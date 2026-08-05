# GitHub repository ACL 只读差异计划

`kb-github-acl-plan` 读取 GitHub repository 的 effective collaborators，与 AIKnowledge 当前 principal direct grants 比较，只输出计划，不修改 GitHub 或 PostgreSQL。

## 为什么先只读

GitHub collaborator API 返回用户综合组织、团队、直接授权和 enterprise 后的最高生效权限，当前无法指出权限来自哪个来源。AIKnowledge schema v5 也还没有 grant provenance。此时自动撤销会把人工授权误当作 GitHub 托管授权，因此：

- GitHub 中存在、已绑定 principal 且本地缺失/失效/权限不同：进入 `activate_or_update`；
- GitHub 中存在但没有 numeric user ID binding：进入 `unmatched_collaborators`；
- binding 中存在但 GitHub 当前不存在：进入 `stale_bindings`；
- 本地 active direct grant 未出现在已绑定 collaborators 中：只进入带 `requires_review: true` 的 `revoke_candidates`；
- 命令没有 apply 选项，所有数据库查询都在只读连接路径完成。

GitHub numeric user ID 用于绑定，不用可改名的 login；login 只出现在未绑定差异中帮助管理员识别。

## 配置和运行

复制并修改 [`examples/github-acl-plan.example.json`](../examples/github-acl-plan.example.json)。token 只从环境变量读取，不接受命令行参数：

```powershell
$env:AIKB_GITHUB_TOKEN = "短期只读凭据"
$env:AIKB_SECURITY_ADMIN_POSTGRES_URL = "postgresql+psycopg://..."
python -m aikb kb-github-acl-plan `
  --config examples/github-acl-plan.example.json
```

GitHub 官方说明：list collaborators 需要调用者对 repository 具备 write/maintain/admin，组织仓还要求调用者是组织成员；fine-grained token 对该 endpoint 需要 repository `Metadata: read`。接口使用 `per_page=100` 并跟随 `Link: rel=next` 分页。[List repository collaborators](https://docs.github.com/en/rest/collaborators/collaborators#list-repository-collaborators)、[REST pagination](https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api)

客户端固定 `Accept: application/vnd.github+json` 和 API version `2026-03-10`，仅允许 HTTPS。分页 URL 必须保持相同 origin 和 endpoint；自动 HTTP redirect 被禁止，避免 Bearer token 被带到未审查地址。API 错误不输出响应正文或 token。

## 离线复现

CI 或审查可以传入不含 token 的固定快照：

```json
{
  "schema_version": 1,
  "repository": "example-organization/linux-fork",
  "captured_at": "2026-08-06T00:00:00Z",
  "collaborators": [
    {"user_id": 12345678, "login": "alice", "permission": "read"}
  ]
}
```

```powershell
python -m aikb kb-github-acl-plan `
  --config examples/github-acl-plan.example.json `
  --snapshot path/to/reviewed-snapshot.json
```

ACL snapshot 和计划包含团队成员身份元数据，应按内部权限文件管理，不能进入公共日志。下一版只有在 grant 增加明确 source/provenance、同步游标和回滚审计后，才会提供受控 apply。
