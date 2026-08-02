"""Create the PostgreSQL/pgvector knowledge catalog.

Revision ID: 0001_postgres_schema_v1
Revises: None
"""
from __future__ import annotations

from alembic import op

from aikb.postgres_schema_v1 import (
    POSTGRES_SCHEMA_VERSION,
    metadata as schema_v1_metadata,
    schema_metadata,
)


revision = "0001_postgres_schema_v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    bind = op.get_bind()
    schema_v1_metadata.create_all(bind=bind, checkfirst=False)
    bind.execute(
        schema_metadata.insert().values(
            key="postgres_schema_version",
            value=str(POSTGRES_SCHEMA_VERSION),
        )
    )


def downgrade() -> None:
    schema_v1_metadata.drop_all(bind=op.get_bind(), checkfirst=True)
