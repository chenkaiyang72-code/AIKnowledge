"""Add immutable multi-repository solution snapshots.

Revision ID: 0003_solution_snapshot
Revises: 0002_chunk_content
"""
from __future__ import annotations

from alembic import op

from aikb.postgres_solution_schema import (
    solution,
    solution_snapshot,
    solution_snapshot_event,
    solution_snapshot_member,
)


revision = "0003_solution_snapshot"
down_revision = "0002_chunk_content"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table in (
        solution,
        solution_snapshot,
        solution_snapshot_event,
        solution_snapshot_member,
    ):
        table.create(bind=bind, checkfirst=True)
    op.execute(
        "UPDATE schema_metadata SET value = '3' "
        "WHERE key = 'postgres_schema_version'"
    )


def downgrade() -> None:
    bind = op.get_bind()
    for table in (
        solution_snapshot_member,
        solution_snapshot_event,
        solution_snapshot,
        solution,
    ):
        table.drop(bind=bind, checkfirst=True)
    op.execute(
        "UPDATE schema_metadata SET value = '2' "
        "WHERE key = 'postgres_schema_version'"
    )
