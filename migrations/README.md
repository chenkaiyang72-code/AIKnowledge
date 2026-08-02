# PostgreSQL migrations

`0001_postgres_schema_v1` imports the frozen `aikb.postgres_schema_v1` metadata. Future schema changes must add a new revision and must not modify the v1 table contract in a way that changes the historical migration.

Run with an explicit database URL:

```powershell
$env:AIKB_POSTGRES_URL = "postgresql+psycopg://aikb:aikb@127.0.0.1:5432/aikb"
python -m alembic upgrade head
```

The migration enables the `vector` extension but does not compile it. Local/CI environments must use a PostgreSQL distribution or container image where pgvector is already installed.
