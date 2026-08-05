# PostgreSQL/pgvector schema 与 snapshot adapter

## 目标

SQLite catalog 继续承担无基础设施的本地实验；PostgreSQL schema v1 是团队共享服务的权威存储起点。两者使用相同的 repository/snapshot/blob/file/chunk/symbol/relation 领域语义，但 PostgreSQL 额外提供 vector、JSONB、partial unique index 和后续 RLS 所需边界。

源码索引规则没有变化：数据库只接收静态扫描产物，不编译代码，不执行仓库脚本。

## 固定依赖

`postgres` optional dependency 当前固定：

- SQLAlchemy `2.0.51`
- Alembic `1.18.5`
- Psycopg binary `3.3.4`
- pgvector Python `0.5.0`
- CI service：`pgvector/pgvector:0.8.2-pg17-bookworm`

选择 Psycopg binary 和预装 pgvector 的服务镜像，是为了避免在开发机或 CI 现场编译数据库驱动/扩展。

官方依据：

- [pgvector repository and Docker tags](https://github.com/pgvector/pgvector)
- [SQLAlchemy 2.0 documentation](https://docs.sqlalchemy.org/en/20/)
- [Alembic documentation](https://alembic.sqlalchemy.org/en/latest/)
- [Psycopg 3 documentation](https://www.psycopg.org/)

## Provider 边界

`aikb.storage.ReadCatalog` 定义 Context Pack 和 RRF 实际需要的只读能力。SQLite `Catalog` 与 `PostgresCatalog` 都实现这个边界，Context Pack builder 不依赖 SQLite SQL 或连接对象。

写入边界由 `PostgresSnapshotPublisher` 单独承担，因为 snapshot 发布需要显式事务和原子 active 切换，不能把复杂写事务隐藏在通用 CRUD repository 中。本地 scanner 先生成已经验证的 source-only SQLite snapshot，publisher 只复制这些确定性产物，不重新解析源码、不编译代码。

## Schema

schema v1 包含 15 张基础业务表：

1. `schema_metadata`
2. `repository`
3. `snapshot`
4. `snapshot_event`
5. `blob`
6. `analysis_artifact`
7. `source_file`
8. `chunk`
9. `logical_symbol`
10. `source_condition`
11. `symbol_occurrence`
12. `relation`
13. `embedding_model`
14. `chunk_embedding`
15. `retrieval_trace`

schema v2 增加 `chunk.content` 和 `to_tsvector('simple', content)` GIN index。正文用于稳定 evidence 读取；GIN 只作为 PostgreSQL bootstrap/故障回退 lexical 通道，正式服务通过已实现的 `ZoektReadCatalog` 使用 Zoekt。

schema v3 通过独立 `postgres_solution_schema` 和 migration `0003_solution_snapshot` 增加四张跨仓版本表。schema v4 增加 security domain、principal、team/member 与 repository grant，并为源码、关系、embedding、solution 和 trace 建立 RLS。schema v5 增加 token 撤销时间、最小认证目录角色和 `mcp_audit_event`。schema v6 新增 `repository_grant_source`，把稳定 grantee 父对象与 manifest/manual/GitHub/GitLab 授权来源分开，总计 26 张业务表。独立 metadata 避免新表定义污染 `0001` 所引用的冻结 v1 metadata。

关键约束：

- 每仓只有一个 `state='active'` 的 snapshot，由 partial unique index 强制。
- snapshot、file、chunk、occurrence 和 relation 均带不可变版本归属。
- blob 和 analysis artifact 继续按内容哈希/分析 profile 去重。
- `chunk_embedding` 同时绑定 chunk 和 model/version，禁止不同 embedding 原地混用。
- vector 暂不建立 ANN index；必须先确定 model dimension 和真实评测收益。
- retrieval trace 只保存 query hash、scope、版本、预算和结果摘要，不默认保存完整源码或问题正文。
- 每个 solution 只有一个 active solution snapshot；manifest JSON、摘要和成员精确 repository snapshot 均不可变保存。
- repository 可见性要求至少一个 grant source 有效；source-scoped revoke 不影响其他来源，直接只插 `repository_grant` 父行不产生访问权。

## Migration

首个 revision 是 `0001_postgres_schema_v1`。它引用冻结的 `aikb.postgres_schema_v1` metadata；未来变更必须创建新 revision，不能让历史 migration 随当前模型漂移。

```powershell
python -m pip install -e ".[postgres]"
$env:AIKB_POSTGRES_URL = "postgresql+psycopg://aikb:aikb@127.0.0.1:5432/aikb"
python -m alembic upgrade head
```

离线检查：

```powershell
python -m alembic upgrade head --sql
```

## 原子发布

```powershell
$env:AIKB_POSTGRES_URL = "postgresql+psycopg://aikb:aikb@127.0.0.1:5432/aikb"
python -m aikb kb-publish-postgres `
  --db .aikb/catalog.db `
  --snapshot-id snap_0b0e8c0e71ad7f720c31b8e2
```

`--snapshot-id` 在本地 catalog 恰好只有一个 active snapshot 时可省略。`--batch-size` 默认是 1000，复制过程不会把大型仓库的全部 chunk、occurrence 或 relation 一次载入内存。

一次发布在同一个数据库事务内完成：

1. 注册 repository，并取得按 repository ID 计算的 PostgreSQL transaction advisory lock。
2. 按批次复制内容寻址 blob、analysis artifact、file、chunk、symbol、condition 和 relation。
3. 重新计算 file/blob/chunk、结构化/fallback、解析异常、occurrence、relation、condition 和字节数，必须与来源 snapshot 一致。
4. 写入 `validated` 事件，先 supersede 旧 active snapshot，再激活新 snapshot。

任一步骤失败时，repository、首次出现的 blob、snapshot 和所有派生记录一起回滚。再次发布 active snapshot 会校验已有计数后直接返回；再次发布 superseded snapshot 会在锁和同一事务内重新激活它。partial unique index 仍是“每仓只能有一个 active snapshot”的最终数据库约束。

全部成员 snapshot 发布完成后，可以原子发布跨仓版本组合：

```powershell
python -m aikb kb-solution-publish-postgres `
  --manifest configs/solutions/my-solution.json
```

solution publisher 使用独立 advisory lock、成员存在性检查、完整 manifest JSON、状态事件和每 solution 唯一 active 约束。它只组合已经发布的 source-only snapshots，不复制或重新分析源码。

## 验证

本地无需 PostgreSQL即可验证 metadata 和离线 DDL。设置 `AIKB_TEST_POSTGRES_URL` 后，集成测试会实际执行 Alembic upgrade，并检查：

- pgvector extension 可用；
- 26 张业务表存在；
- schema version 为 6；
- 同一 repository 插入第二个 active snapshot 会失败。
- PostgreSQL read adapter 能直接构造 Context Pack，且多词 lexical fallback 使用 OR 语义；
- publisher 支持小批次复制、幂等重试、新旧 snapshot 切换、历史 snapshot 重新激活和注入失败后的完整回滚。
- solution publisher 支持双仓固定版本、幂等发布、跨仓 Context Pack 和 partial visibility。

GitHub Actions 使用 PostgreSQL 17 pgvector service 执行这组测试。本地机器不需要安装 Docker 或 PostgreSQL。

2026-08-02 的首次完整 CI 已通过 23/23，见 [run 30750390588](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/30750390588)；read adapter 与 publisher 依次完成 24/24、25/25；schema v3 solution 完成 43/43，见 [run 31043795023](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31043795023)；MCP 完成 50/50，见 [run 31046518729](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31046518729)；schema v4 RLS 完成 51/51，见 [run 31047764753](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31047764753)；schema v5 OIDC/audit 完成 57/57，见 [run 31049508639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31049508639)；security admin 完成 61/61，见 [run 31050256639](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31050256639)；GitHub ACL planner 完成 65/65，见 [run 31050938115](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/31050938115)。schema v6 grant provenance 等待下一次共享 CI。

## 尚未完成

- scanner 当前仍先写本地 SQLite 再显式 publish；后台 index orchestrator、队列重试和自动发布尚未实现。
- PostgreSQL lexical 仍是 bootstrap/故障回退；Zoekt adapter 已实现，但团队环境的常驻 Zoekt 部署与运维尚未完成。
- organization/team/grant、RLS、OIDC/audit、security manifest 和 GitHub ACL 只读 planner 已通过共享 CI；schema v6 grant source 并集、legacy backfill 与 source-scoped revoke 等待下一次共享 CI。
- vector adapter、模型选择和 ANN index 尚未实现。
- 生产备份、连接池、分区和数据保留策略尚未验证。
