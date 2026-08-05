from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from aikb.postgres_schema_v1 import NAMING_CONVENTION


auth_metadata = MetaData(naming_convention=NAMING_CONVENTION)
Table("security_domain", auth_metadata, Column("id", String(64), primary_key=True))
Table("principal", auth_metadata, Column("id", String(64), primary_key=True))


mcp_audit_event = Table(
    "mcp_audit_event",
    auth_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "principal_id",
        String(64),
        ForeignKey("principal.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column(
        "security_domain_id",
        String(64),
        ForeignKey("security_domain.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    Column("request_id", String(128), nullable=False),
    Column("tool_name", String(64), nullable=False),
    Column("outcome", String(16), nullable=False),
    Column("query_hash", String(64)),
    Column("trace_id", String(64)),
    Column("scope_summary", JSONB, nullable=False),
    Column("result_summary", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("outcome IN ('success', 'error')", name="outcome"),
)
Index(
    "ix_mcp_audit_principal_created",
    mcp_audit_event.c.principal_id,
    mcp_audit_event.c.created_at,
)
Index(
    "ix_mcp_audit_domain_created",
    mcp_audit_event.c.security_domain_id,
    mcp_audit_event.c.created_at,
)
