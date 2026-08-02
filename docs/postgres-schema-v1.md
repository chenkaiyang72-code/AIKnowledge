# PostgreSQL/pgvector schema v1

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

`aikb.storage.ReadCatalog` 定义 Context Pack 和 RRF 实际需要的只读能力。当前 `Catalog` 以结构化类型方式实现该 Protocol；未来 PostgreSQL adapter 只需要实现相同方法，不允许 Context Pack builder 依赖 SQLite SQL 或连接对象。

ingest 写入接口会在 PostgreSQL adapter 实现时单独定义，因为 snapshot 发布需要显式事务和原子 active 切换，不能把复杂写事务隐藏在通用 CRUD repository 中。

## Schema

schema v1 包含 15 张业务表：

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

关键约束：

- 每仓只有一个 `state='active'` 的 snapshot，由 partial unique index 强制。
- snapshot、file、chunk、occurrence 和 relation 均带不可变版本归属。
- blob 和 analysis artifact 继续按内容哈希/分析 profile 去重。
- `chunk_embedding` 同时绑定 chunk 和 model/version，禁止不同 embedding 原地混用。
- vector 暂不建立 ANN index；必须先确定 model dimension 和真实评测收益。
- retrieval trace 只保存 query hash、scope、版本、预算和结果摘要，不默认保存完整源码或问题正文。

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

## 验证

本地无需 PostgreSQL即可验证 metadata 和离线 DDL。设置 `AIKB_TEST_POSTGRES_URL` 后，集成测试会实际执行 Alembic upgrade，并检查：

- pgvector extension 可用；
- 15 张业务表存在；
- schema version 为 1；
- 同一 repository 插入第二个 active snapshot 会失败。

GitHub Actions 使用 PostgreSQL 17 pgvector service 执行这组测试。本地机器不需要安装 Docker 或 PostgreSQL。

## 尚未完成

- PostgreSQL read/write adapter 尚未实现，运行时仍使用 SQLite bootstrap。
- organization/team/repository ACL 和 RLS policy 尚未加入；`retrieval_trace` 已预留 principal/security domain 字段。
- vector adapter、模型选择和 ANN index 尚未实现。
- 生产备份、连接池、分区和数据保留策略尚未验证。
