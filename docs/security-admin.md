# 团队身份与仓库授权管理

`kb-security-apply` 把一份严格 JSON manifest 原子应用到 PostgreSQL schema v6。它负责 security domain、OIDC principal、team、membership 和 repository grant/source，不接触源码内容。

## 安全语义

- manifest 是增量声明：出现的对象会创建或更新，未出现的对象保持不变，不做隐式删除；只有 membership 明确写 `active: false` 时才删除该关系，改回 `true` 可恢复；
- principal 的 domain、OIDC issuer 和 subject 一旦建立就不可通过 manifest 改写，避免身份接管；
- membership 和 grant 必须引用同一 security domain 中的对象；repository 必须已由发布流程建立；
- 根级 `grant_source_key` 标识这份 manifest 的稳定授权来源；digest 写入 `source_revision`。grant 的 `active: false` 只撤销该 manifest source，其他 manual/GitHub/GitLab source 仍有效；`active: true` 可显式恢复；
- 整份 manifest 在单事务内提交，任何交叉引用或唯一性错误都会整体回滚；
- `--dry-run` 执行相同数据库校验后回滚；命令不打印连接串、issuer subject 或其他身份明细。

## 使用

复制并修改 [`examples/security-manifest.example.json`](../examples/security-manifest.example.json)。repository ID 可由发布报告或管理员 SQL 获得，然后使用只供安全管理员持有的 owner/admin 连接：

```powershell
$env:AIKB_SECURITY_ADMIN_POSTGRES_URL = "postgresql+psycopg://..."
python -m aikb kb-security-apply `
  --manifest examples/security-manifest.example.json `
  --dry-run
python -m aikb kb-security-apply `
  --manifest examples/security-manifest.example.json
```

普通 MCP service login 只有 `SET ROLE aikb_reader/aikb_authenticator` 权限，不能运行该命令。生产环境应把 manifest 纳入代码审查，并让 CI/CD 从 secret manager 注入管理员连接串。

当用户设备丢失或 access token 需要立即失效时：

```powershell
python -m aikb kb-security-revoke-tokens --principal-id principal-alice
```

该命令把 `tokens_valid_after` 单调推进到数据库当前时间；之前签发的 token 随后都会被资源服务器拒绝。要暂停或永久撤销 principal，应另外把 manifest 中的 `status` 改为 `suspended` 或 `revoked` 并重新应用。
