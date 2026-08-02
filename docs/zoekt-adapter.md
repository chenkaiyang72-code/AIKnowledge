# Zoekt source-only 索引与检索适配器

## 边界与选择

Zoekt 是 AIKnowledge 的正式 lexical provider，负责在大型源码仓库中召回文本、标识符、路径和正则候选；PostgreSQL/SQLite 仍是 snapshot、chunk、关系与权限的权威目录。Zoekt 的命中必须通过 repository/snapshot scope 校验并按文件与行号映射回权威 chunk，才能进入 RRF 和 Context Pack。

实现遵守以下边界：

- 只导出 catalog 中已经验证的 `active` 或 `superseded` 不可变 snapshot。
- 导出前复核 zlib 内容、blob SHA-256、文件大小和 manifest 计数；相同输入重复导出幂等，已存在目录被篡改时拒绝覆盖。
- 每个 snapshot 使用独立内部仓库名 `aikb-snapshot-<snapshot_id>`，避免不同版本混在同一 shard scope。
- 使用官方预构建 Zoekt 镜像，并显式传入 `-disable_ctags`；不执行源码仓库脚本、不运行生成器、不编译 Linux。
- AIKnowledge 在发出查询前限定 snapshot，收到结果后再次精确校验 repository；越界结果和版本冲突是协议错误，不能静默回退。
- 只有网络、超时或服务端不可用时允许回退到 catalog FTS，trace 会记录真实执行的 provider。

实现依据为 Zoekt 官方的 [README](https://github.com/sourcegraph/zoekt/blob/main/README.md)、[JSON API](https://github.com/sourcegraph/zoekt/blob/main/doc/json-api.md) 和[查询语法](https://github.com/sourcegraph/zoekt/blob/main/doc/query_syntax.md)。

## 1. 导出不可变 snapshot

```powershell
python -m aikb kb-zoekt-export `
  --db .aikb/catalog.db `
  --snapshot-id snap_0b0e8c0e71ad7f720c31b8e2 `
  --output .aikb/zoekt/linux-sched
```

输出目录包含：

- `source/`：从内容寻址 blob 还原的静态源码。
- `manifest.json`：snapshot、revision、manifest/index profile digest 和逐文件哈希。
- `zoekt.meta.json`：传给 `zoekt-index -meta` 的内部仓库名、固定 revision 与 AIKnowledge 元数据。

## 2. 用固定的预构建镜像建立索引

当前 CI 固定以下镜像 digest：

```text
ghcr.io/sourcegraph/zoekt@sha256:0bf4af966897c2fd493e2b0826440e17d5640e8c4d8579c7e5cac28f084da75a
```

下面是与 CI 一致的容器内命令；将导出目录挂载到 `/fixture/export`，将可写索引目录挂载到 `/fixture/index`：

```text
zoekt-index \
  -index /fixture/index \
  -meta /fixture/export/zoekt.meta.json \
  -disable_ctags \
  /fixture/export/source
```

启动只读查询服务：

```text
zoekt-webserver -index /data/index -rpc -listen :6070
```

`-rpc` 开放 JSON `POST /api/search`；服务应只放在内部网络，由 AIKnowledge adapter 访问，不直接暴露给 AI 客户端或最终用户。

## 3. 查询与回退

```powershell
$env:AIKB_ZOEKT_URL = "http://127.0.0.1:6070"

python -m aikb kb-search `
  --query "init_idle do_idle" `
  --snapshot-id snap_0b0e8c0e71ad7f720c31b8e2 `
  --zoekt-required

python -m aikb kb-context `
  --query "init_idle do_idle" `
  --max-evidence-items 6 `
  --evidence-token-budget 1200
```

也可用 `--zoekt-url` 覆盖环境变量。默认行为是在 Zoekt 暂时不可达时回退；验收、CI 和生产健康检查应使用 `--zoekt-required`，避免把 FTS 回退误认为 Zoekt 成功。

## 4. 验证证据

GitHub Actions 从一个 source-only fixture 构建不可变 snapshot，使用固定镜像建立索引并启动 webserver，然后运行完整测试。live test 要求 Zoekt 命中 `kernel/live.c:1`，并确认它映射回同一 snapshot 的权威 chunk。PostgreSQL 17 + pgvector、SQLite、Context Pack 和 Zoekt 合计 29 个测试全部通过，见 [run 30753488893](https://github.com/chenkaiyang72-code/AIKnowledge/actions/runs/30753488893)。
