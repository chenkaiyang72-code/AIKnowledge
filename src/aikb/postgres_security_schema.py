from __future__ import annotations

from sqlalchemy import (
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
)
from sqlalchemy.dialects.postgresql import JSONB

from aikb.postgres_schema_v1 import NAMING_CONVENTION


POSTGRES_SECURITY_SCHEMA_VERSION = 4
security_metadata = MetaData(naming_convention=NAMING_CONVENTION)

# This lightweight declaration lets the migration-owned metadata compile the
# repository foreign key without mutating the frozen v1 metadata.
Table("repository", security_metadata, Column("id", String(64), primary_key=True))


security_domain = Table(
    "security_domain",
    security_metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(256), nullable=False, unique=True),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("data_policy", JSONB, nullable=False, server_default="{}"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active', 'suspended')", name="status"),
)

principal = Table(
    "principal",
    security_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "security_domain_id",
        String(64),
        ForeignKey("security_domain.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("issuer", Text, nullable=False),
    Column("subject", Text, nullable=False),
    Column("display_name", Text, nullable=False, server_default=""),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("token_epoch", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("last_seen_at", DateTime(timezone=True)),
    CheckConstraint("status IN ('active', 'suspended', 'revoked')", name="status"),
    CheckConstraint("token_epoch >= 0", name="nonnegative_token_epoch"),
    UniqueConstraint("issuer", "subject", name="uq_principal_oidc_identity"),
)
Index("ix_principal_domain_status", principal.c.security_domain_id, principal.c.status)

security_team = Table(
    "security_team",
    security_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "security_domain_id",
        String(64),
        ForeignKey("security_domain.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("name", String(256), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('active', 'suspended')", name="status"),
    UniqueConstraint("security_domain_id", "name", name="uq_security_team_domain_name"),
)

security_team_member = Table(
    "security_team_member",
    security_metadata,
    Column(
        "team_id",
        String(64),
        ForeignKey("security_team.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "principal_id",
        String(64),
        ForeignKey("principal.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("role", String(16), nullable=False, server_default="member"),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("role IN ('member', 'maintainer')", name="role"),
    PrimaryKeyConstraint("team_id", "principal_id"),
)

repository_grant = Table(
    "repository_grant",
    security_metadata,
    Column("id", String(64), primary_key=True),
    Column(
        "security_domain_id",
        String(64),
        ForeignKey("security_domain.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "repository_id",
        String(64),
        ForeignKey("repository.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("principal_id", String(64), ForeignKey("principal.id", ondelete="CASCADE")),
    Column("team_id", String(64), ForeignKey("security_team.id", ondelete="CASCADE")),
    Column("permission", String(16), nullable=False, server_default="read"),
    Column("granted_by_principal_id", String(64), ForeignKey("principal.id", ondelete="SET NULL")),
    Column("expires_at", DateTime(timezone=True)),
    Column("revoked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint("(principal_id IS NULL) <> (team_id IS NULL)", name="one_grantee"),
    CheckConstraint("permission IN ('read', 'write', 'admin')", name="permission"),
)
Index(
    "uq_repository_grant_principal",
    repository_grant.c.security_domain_id,
    repository_grant.c.repository_id,
    repository_grant.c.principal_id,
    unique=True,
    postgresql_where=repository_grant.c.principal_id.is_not(None),
)
Index(
    "uq_repository_grant_team",
    repository_grant.c.security_domain_id,
    repository_grant.c.repository_id,
    repository_grant.c.team_id,
    unique=True,
    postgresql_where=repository_grant.c.team_id.is_not(None),
)
Index(
    "ix_repository_grant_repository_domain",
    repository_grant.c.repository_id,
    repository_grant.c.security_domain_id,
)
