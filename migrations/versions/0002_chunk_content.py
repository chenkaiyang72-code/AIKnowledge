"""Store deterministic chunk content for evidence reads and FTS fallback.

Revision ID: 0002_chunk_content
Revises: 0001_postgres_schema_v1
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_chunk_content"
down_revision = "0001_postgres_schema_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunk",
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("chunk", "content", server_default=None)
    op.execute(
        "CREATE INDEX ix_chunk_content_fts ON chunk "
        "USING gin (to_tsvector('simple', content))"
    )
    op.execute(
        "UPDATE schema_metadata SET value = '2' "
        "WHERE key = 'postgres_schema_version'"
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_content_fts", table_name="chunk")
    op.drop_column("chunk", "content")
    op.execute(
        "UPDATE schema_metadata SET value = '1' "
        "WHERE key = 'postgres_schema_version'"
    )
