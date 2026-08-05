# ADR-0005：远程 MCP 作为 OIDC Resource Server

状态：已接受（共享 CI 待终验）
日期：2026-08-06

## 背景

远程 MCP 会处理团队私有源码，不能使用固定 API header、客户端自报 principal 或 token passthrough。MCP 授权规范要求 HTTP server 作为 OAuth resource server，在未认证时返回 HTTP 401 和 Protected Resource Metadata，并验证 token 确实签发给自身。

## 决策

1. 使用 MCP Python SDK v2 自带的 Bearer auth middleware、401 challenge 和 RFC 9728 Protected Resource Metadata，不在 tool 内返回“未登录”错误。
2. AIKnowledge 只验证外部 OIDC/OAuth authorization server 签发的 JWT，不实现自己的密码登录或 token 签发服务。
3. 首版固定 `PyJWT[crypto]==2.13.0`，只允许显式配置的非对称算法；拒绝 HMAC、`none`、未知 `typ` 和从 token header 动态选择算法。
4. 必须验证 signature、issuer、精确 audience/resource、required scope、subject、client ID、exp、iat 和 nbf；JWKS URL 是 operator 配置的 HTTPS 地址，不从请求 token 或普通 header 推导。
5. 通过签名验证后，issuer + subject 再由 `aikb_authenticator` 最小权限角色映射到 active principal/domain。token 不能直接声明 repository、team 或 grant。
6. `principal.tokens_valid_after` 提供全量 token 撤销：iat 早于该时间的 token 一律拒绝。
7. 验证器只把服务端映射后的 principal/domain 放入 MCP request context；随后 PostgreSQL `aikb_reader` 和 RLS 强制实际仓库可见性。
8. 成功和失败的 tool call 写 `mcp_audit_event`，只保存 query/scope SHA-256、tool/outcome、trace ID 和结果计数，不保存 token、问题原文或源码正文。

## 后果

- Cursor、Claude Code 等支持 MCP OAuth 的客户端可通过标准发现流程连接。
- authorization server 的登录、MFA、consent 和 client registration 仍由企业 IdP 负责。
- service login 必须是 `aikb_reader` 与 `aikb_authenticator` 的成员，但不得拥有业务表或 BYPASSRLS。
- 远程绑定必须配合反向代理 TLS；公开 resource URL 同时是 JWT audience，字符串必须精确一致。
- 当前 token 撤销粒度是 principal 的“某时间前全部失效”；单个 jti denylist 可在确有需求后增加。

## 依据

- [MCP Authorization specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [MCP Authorization tutorial](https://modelcontextprotocol.io/docs/tutorials/security/authorization)
- [MCP Security Best Practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices)
- [PyJWT 2.13.0](https://pypi.org/project/PyJWT/)
- [PyJWT JWKS usage](https://pyjwt.readthedocs.io/en/stable/usage.html#retrieve-rsa-signing-keys-from-a-jwks-endpoint)
