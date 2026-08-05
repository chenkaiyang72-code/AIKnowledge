"""Add token revocation time and a least-privilege principal lookup role.

Revision ID: 0005_oidc_principal_directory
Revises: 0004_principal_acl_rls
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from aikb.postgres_auth_schema import mcp_audit_event


revision = "0005_oidc_principal_directory"
down_revision = "0004_principal_acl_rls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "principal",
        sa.Column(
            "tokens_valid_after",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("to_timestamp(0)"),
        ),
    )
    mcp_audit_event.create(bind=op.get_bind(), checkfirst=True)
    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='aikb_authenticator') THEN
            CREATE ROLE aikb_authenticator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          ELSIF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname='aikb_authenticator'
              AND (rolsuper OR rolbypassrls OR rolcanlogin)
          ) THEN
            RAISE EXCEPTION 'existing aikb_authenticator role has unsafe attributes';
          END IF;
        END
        $$
        """
    )
    op.execute("GRANT USAGE ON SCHEMA public TO aikb_authenticator")
    op.execute(
        "GRANT SELECT (id,status) ON security_domain TO aikb_authenticator"
    )
    op.execute(
        "GRANT SELECT (id,security_domain_id,issuer,subject,status,tokens_valid_after) "
        "ON principal TO aikb_authenticator"
    )
    op.execute("GRANT SELECT, INSERT ON mcp_audit_event TO aikb_reader")
    op.execute("ALTER TABLE mcp_audit_event ENABLE ROW LEVEL SECURITY")
    audit_expression = (
        "principal_id=nullif(current_setting('aikb.principal_id', true), '') "
        "AND security_domain_id=nullif("
        "current_setting('aikb.security_domain_id', true), '')"
    )
    op.execute(
        "CREATE POLICY aikb_audit_own ON mcp_audit_event TO aikb_reader "
        f"USING ({audit_expression}) WITH CHECK ({audit_expression})"
    )
    op.execute(
        "UPDATE schema_metadata SET value = '5' "
        "WHERE key = 'postgres_schema_version'"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS aikb_audit_own ON mcp_audit_event")
    op.execute("ALTER TABLE mcp_audit_event DISABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL PRIVILEGES ON mcp_audit_event FROM aikb_reader")
    mcp_audit_event.drop(bind=op.get_bind(), checkfirst=True)
    op.execute("REVOKE ALL PRIVILEGES ON principal FROM aikb_authenticator")
    op.execute("REVOKE ALL PRIVILEGES ON security_domain FROM aikb_authenticator")
    op.drop_column("principal", "tokens_valid_after")
    op.execute(
        "UPDATE schema_metadata SET value = '4' "
        "WHERE key = 'postgres_schema_version'"
    )
