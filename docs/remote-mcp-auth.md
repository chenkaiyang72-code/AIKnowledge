# 远程 MCP：OIDC、RLS 与审计

## 安全链路

```text
MCP client
  -> HTTPS reverse proxy
  -> MCP Bearer middleware (401 + Protected Resource Metadata)
  -> JWT signature / issuer / audience / scope / time validation
  -> issuer + subject -> active PostgreSQL principal/domain
  -> SET LOCAL ROLE aikb_reader + transaction-local identity
  -> repository/content/solution RLS
  -> Context Pack
  -> metadata-only mcp_audit_event
```

任何一层失败都不返回源码。未认证请求在 HTTP 层得到 401；已认证但无仓库权限的 tool call 得到统一的不可解析错误，不区分“不存在”和“无权限”。

## 安装与迁移

```powershell
python -m pip install -e ".[postgres,mcp,auth]"
$env:AIKB_POSTGRES_URL = "postgresql+psycopg://migration-user@db.example/aikb"
python -m alembic upgrade head
```

schema v5 增加 `tokens_valid_after`、NOLOGIN `aikb_authenticator` 和 `mcp_audit_event`。生产 service login 由 DBA 单独创建，它不能拥有表：

```sql
CREATE ROLE aikb_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
GRANT aikb_reader, aikb_authenticator TO aikb_service;
```

migration/publisher credential 与 `aikb_service` 必须分开。MCP 的 `--postgres-url` 只能使用 service login。

## principal 与授权

OIDC 身份以 issuer + subject 唯一映射，示意初始化由受控 admin transaction 完成：

```sql
INSERT INTO security_domain(id,name) VALUES ('domain_kernel','kernel-team');
INSERT INTO principal(
  id,security_domain_id,issuer,subject,display_name
) VALUES (
  'principal_alice','domain_kernel','https://idp.example','oidc-subject','Alice'
);
INSERT INTO repository_grant(
  id,security_domain_id,repository_id,principal_id,permission
) VALUES (
  'grant_alice_linux','domain_kernel','repo_linux','principal_alice','read'
);
```

生产环境使用版本化 security manifest 或后续 GitHub/GitLab ACL 同步，不把 SQL 权限交给普通用户。完整方式见[安全管理文档](security-admin.md)。要立即撤销该 principal 现有 token：

```powershell
python -m aikb kb-security-revoke-tokens --principal-id principal_alice
```

## 启动远程服务

resource server URL 是外部客户端看到的规范 HTTPS MCP 地址，也是 JWT 必须包含的 audience：

```powershell
python -m aikb mcp-serve `
  --transport streamable-http `
  --host 0.0.0.0 `
  --port 8000 `
  --path /mcp/read `
  --postgres-url "postgresql+psycopg://aikb_service@db.example/aikb" `
  --oidc-issuer "https://idp.example" `
  --oidc-jwks-url "https://idp.example/.well-known/jwks.json" `
  --resource-server-url "https://kb.example.com/mcp/read" `
  --required-scope "aiknowledge.read"
```

应用端口只暴露给 TLS reverse proxy。非 loopback 公共 issuer、JWKS 和 resource URL 强制 HTTPS；HTTP 只允许 loopback 测试。

Protected Resource Metadata 位于：

```text
https://kb.example.com/.well-known/oauth-protected-resource/mcp/read
```

返回的 `authorization_servers` 指向配置的 issuer，`scopes_supported` 至少包含 `aiknowledge.read`。错误 audience、issuer、scope、exp/iat/nbf、token type、算法、未知/暂停 principal 和撤销时间都会得到相同的 401 invalid token。

## 审计

每次实际 tool call 记录：principal/domain、MCP request ID、tool、success/error、query hash、requested scope hash、Context Pack trace ID 和结果计数。不会记录 Bearer token、问题文本、文件路径、仓库名称或源码正文。reader 只能读取自己的 audit rows，不能更新或删除。

## 当前边界

- IdP 配置、用户登录、MFA、consent、client registration 和 token issuance 不在 AIKnowledge 内实现。
- 当前支持 JWT access token；opaque token introspection 尚未实现。
- 当前提供 principal 级 `tokens_valid_after`，尚无单 jti denylist。
- GitHub/GitLab ACL 自动同步、service credential 轮换、反向代理限流和生产部署仍在 Phase 1B 后续任务中。
