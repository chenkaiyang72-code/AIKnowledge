"""Separate repository grant identity from independently revocable sources.

Revision ID: 0006_repository_grant_provenance
Revises: 0005_oidc_principal_directory
"""
from __future__ import annotations

from alembic import op

from aikb.postgres_security_schema import repository_grant_source


revision = "0006_repository_grant_provenance"
down_revision = "0005_oidc_principal_directory"
branch_labels = None
depends_on = None


READ_FUNCTION = """
CREATE OR REPLACE FUNCTION aikb_can_read_repository(target_repository_id text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.principal p
    JOIN public.security_domain d
      ON d.id=p.security_domain_id AND d.status='active'
    JOIN public.repository_grant g
      ON g.security_domain_id=p.security_domain_id
     AND g.repository_id=target_repository_id
    JOIN public.repository_grant_source gs
      ON gs.repository_grant_id=g.id
     AND gs.revoked_at IS NULL
     AND (gs.expires_at IS NULL OR gs.expires_at > statement_timestamp())
    WHERE p.id=nullif(current_setting('aikb.principal_id', true), '')
      AND p.security_domain_id=nullif(
            current_setting('aikb.security_domain_id', true), '')
      AND p.status='active'
      AND (
        g.principal_id=p.id
        OR EXISTS (
          SELECT 1
          FROM public.security_team_member tm
          JOIN public.security_team t ON t.id=tm.team_id
          WHERE tm.team_id=g.team_id
            AND tm.principal_id=p.id
            AND t.security_domain_id=p.security_domain_id
            AND t.status='active'
        )
      )
  )
$$
"""


LEGACY_READ_FUNCTION = """
CREATE OR REPLACE FUNCTION aikb_can_read_repository(target_repository_id text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
  SELECT EXISTS (
    SELECT 1
    FROM public.principal p
    JOIN public.security_domain d
      ON d.id=p.security_domain_id AND d.status='active'
    JOIN public.repository_grant g
      ON g.security_domain_id=p.security_domain_id
     AND g.repository_id=target_repository_id
    WHERE p.id=nullif(current_setting('aikb.principal_id', true), '')
      AND p.security_domain_id=nullif(
            current_setting('aikb.security_domain_id', true), '')
      AND p.status='active'
      AND g.revoked_at IS NULL
      AND (g.expires_at IS NULL OR g.expires_at > statement_timestamp())
      AND (
        g.principal_id=p.id
        OR EXISTS (
          SELECT 1
          FROM public.security_team_member tm
          JOIN public.security_team t ON t.id=tm.team_id
          WHERE tm.team_id=g.team_id
            AND tm.principal_id=p.id
            AND t.security_domain_id=p.security_domain_id
            AND t.status='active'
        )
      )
  )
$$
"""


def upgrade() -> None:
    repository_grant_source.create(bind=op.get_bind(), checkfirst=True)
    op.execute(
        """
        INSERT INTO repository_grant_source(
          id,repository_grant_id,source_kind,source_key,source_revision,
          permission,expires_at,revoked_at,last_observed_at,created_at
        )
        SELECT
          'legacy_' || substr(md5(g.id),1,24),g.id,'legacy',g.id,'schema-v5',
          g.permission,g.expires_at,g.revoked_at,g.created_at,g.created_at
        FROM repository_grant g
        ON CONFLICT (repository_grant_id,source_kind,source_key) DO NOTHING
        """
    )
    op.execute(READ_FUNCTION)
    op.execute(
        "UPDATE schema_metadata SET value='6' "
        "WHERE key='postgres_schema_version'"
    )


def downgrade() -> None:
    op.execute(
        """
        WITH effective AS (
          SELECT
            g.id,
            bool_or(
              gs.revoked_at IS NULL
              AND (gs.expires_at IS NULL OR gs.expires_at > statement_timestamp())
            ) AS active,
            bool_or(
              gs.revoked_at IS NULL
              AND (gs.expires_at IS NULL OR gs.expires_at > statement_timestamp())
              AND gs.expires_at IS NULL
            ) AS unbounded,
            max(gs.expires_at) FILTER (
              WHERE gs.revoked_at IS NULL
                AND (gs.expires_at IS NULL OR gs.expires_at > statement_timestamp())
            ) AS max_expiry,
            max(CASE gs.permission
              WHEN 'admin' THEN 3 WHEN 'write' THEN 2 ELSE 1 END) FILTER (
              WHERE gs.revoked_at IS NULL
                AND (gs.expires_at IS NULL OR gs.expires_at > statement_timestamp())
            ) AS permission_rank
          FROM repository_grant g
          JOIN repository_grant_source gs ON gs.repository_grant_id=g.id
          GROUP BY g.id
        )
        UPDATE repository_grant g SET
          permission=CASE effective.permission_rank
            WHEN 3 THEN 'admin' WHEN 2 THEN 'write' ELSE 'read' END,
          expires_at=CASE WHEN effective.unbounded THEN NULL ELSE effective.max_expiry END,
          revoked_at=CASE WHEN effective.active THEN NULL ELSE statement_timestamp() END
        FROM effective WHERE effective.id=g.id
        """
    )
    op.execute(LEGACY_READ_FUNCTION)
    repository_grant_source.drop(bind=op.get_bind(), checkfirst=True)
    op.execute(
        "UPDATE schema_metadata SET value='5' "
        "WHERE key='postgres_schema_version'"
    )
