from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from aikb.postgres_schema_v1 import NAMING_CONVENTION


solution_metadata = MetaData(naming_convention=NAMING_CONVENTION)

# Lightweight references let this migration-owned metadata compile foreign
# keys without mutating the frozen v1 metadata imported by revision 0001.
Table(
    "repository",
    solution_metadata,
    Column("id", String(64), primary_key=True),
)
Table(
    "snapshot",
    solution_metadata,
    Column("id", String(64), primary_key=True),
)


solution = Table(
    "solution",
    solution_metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(512), nullable=False, unique=True),
    Column("description", Text, nullable=False, server_default=""),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

solution_snapshot = Table(
    "solution_snapshot",
    solution_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "solution_id",
        String(64),
        ForeignKey("solution.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("revision", Text, nullable=False),
    Column("manifest_digest", String(64), nullable=False),
    Column("manifest_json", JSONB, nullable=False),
    Column("state", String(16), nullable=False),
    Column("member_count", Integer, nullable=False, server_default="0"),
    Column(
        "created_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
    Column("activated_at", DateTime(timezone=True)),
    CheckConstraint(
        "state IN ('building', 'validated', 'active', 'superseded')",
        name="state",
    ),
    CheckConstraint("member_count >= 0", name="nonnegative_member_count"),
    UniqueConstraint(
        "solution_id",
        "revision",
        "manifest_digest",
        name="uq_solution_snapshot_identity",
    ),
)
Index(
    "uq_solution_snapshot_one_active_per_solution",
    solution_snapshot.c.solution_id,
    unique=True,
    postgresql_where=solution_snapshot.c.state == "active",
)

solution_snapshot_event = Table(
    "solution_snapshot_event",
    solution_metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "solution_snapshot_id",
        String(64),
        ForeignKey("solution_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("state", String(16), nullable=False),
    Column(
        "recorded_at",
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    ),
)

solution_snapshot_member = Table(
    "solution_snapshot_member",
    solution_metadata,
    Column(
        "solution_snapshot_id",
        String(64),
        ForeignKey("solution_snapshot.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "repository_id",
        String(64),
        ForeignKey("repository.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "snapshot_id",
        String(64),
        ForeignKey("snapshot.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("role", String(128), nullable=False),
    Column("ordinal", Integer, nullable=False),
    Column("required", Boolean, nullable=False, server_default=text("true")),
    CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
    PrimaryKeyConstraint("solution_snapshot_id", "repository_id"),
    UniqueConstraint(
        "solution_snapshot_id", "snapshot_id", name="uq_solution_member_snapshot"
    ),
    UniqueConstraint(
        "solution_snapshot_id", "role", name="uq_solution_member_role"
    ),
    UniqueConstraint(
        "solution_snapshot_id", "ordinal", name="uq_solution_member_ordinal"
    ),
)
Index(
    "ix_solution_snapshot_member_snapshot",
    solution_snapshot_member.c.snapshot_id,
)
