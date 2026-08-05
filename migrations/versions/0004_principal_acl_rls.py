"""Add principals, team grants, query-before-filter RLS, and trace isolation.

Revision ID: 0004_principal_acl_rls
Revises: 0003_solution_snapshot
"""
from __future__ import annotations

from alembic import op

from aikb.postgres_security_schema import (
    principal,
    repository_grant,
    security_domain,
    security_team,
    security_team_member,
)


revision = "0004_principal_acl_rls"
down_revision = "0003_solution_snapshot"
branch_labels = None
depends_on = None


READ_POLICIES = {
    "repository": "aikb_can_read_repository(id)",
    "snapshot": "aikb_can_read_repository(repository_id)",
    "snapshot_event": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id = snapshot_event.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "blob": (
        "EXISTS (SELECT 1 FROM source_file f JOIN snapshot s ON s.id=f.snapshot_id "
        "WHERE f.blob_id=blob.id AND aikb_can_read_repository(s.repository_id))"
    ),
    "analysis_artifact": (
        "EXISTS (SELECT 1 FROM source_file f JOIN snapshot s ON s.id=f.snapshot_id "
        "WHERE f.blob_id=analysis_artifact.blob_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "source_file": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id=source_file.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "chunk": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id=chunk.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "logical_symbol": "aikb_can_read_repository(repository_id)",
    "source_condition": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id=source_condition.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "symbol_occurrence": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id=symbol_occurrence.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "relation": (
        "EXISTS (SELECT 1 FROM snapshot s WHERE s.id=relation.snapshot_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "chunk_embedding": (
        "EXISTS (SELECT 1 FROM chunk c JOIN snapshot s ON s.id=c.snapshot_id "
        "WHERE c.id=chunk_embedding.chunk_id "
        "AND aikb_can_read_repository(s.repository_id))"
    ),
    "solution": "aikb_can_read_solution(id)",
    "solution_snapshot": "aikb_can_read_solution_snapshot(id)",
    "solution_snapshot_event": (
        "aikb_can_read_solution_snapshot(solution_snapshot_id)"
    ),
    "solution_snapshot_member": "aikb_can_read_repository(repository_id)",
}

READER_TABLES = ", ".join(
    ["schema_metadata", "embedding_model", *READ_POLICIES]
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in (
        security_domain,
        principal,
        security_team,
        security_team_member,
        repository_grant,
    ):
        table.create(bind=bind, checkfirst=True)

    op.execute(
        """
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
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aikb_can_read_solution_snapshot(
          target_solution_snapshot_id text
        ) RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.solution_snapshot_member m
            WHERE m.solution_snapshot_id=target_solution_snapshot_id
              AND public.aikb_can_read_repository(m.repository_id)
          )
        $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION aikb_can_read_solution(target_solution_id text)
        RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT EXISTS (
            SELECT 1 FROM public.solution_snapshot ss
            WHERE ss.solution_id=target_solution_id
              AND public.aikb_can_read_solution_snapshot(ss.id)
          )
        $$
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='aikb_reader') THEN
            CREATE ROLE aikb_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
              NOINHERIT NOREPLICATION NOBYPASSRLS;
          ELSIF EXISTS (
            SELECT 1 FROM pg_roles WHERE rolname='aikb_reader'
              AND (rolsuper OR rolbypassrls OR rolcanlogin)
          ) THEN
            RAISE EXCEPTION 'existing aikb_reader role has unsafe attributes';
          END IF;
        END
        $$
        """
    )
    op.execute("REVOKE ALL ON FUNCTION aikb_can_read_repository(text) FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION aikb_can_read_solution_snapshot(text) FROM PUBLIC"
    )
    op.execute("REVOKE ALL ON FUNCTION aikb_can_read_solution(text) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION aikb_can_read_repository(text), "
        "aikb_can_read_solution_snapshot(text), aikb_can_read_solution(text) "
        "TO aikb_reader"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO aikb_reader")
    op.execute(f"GRANT SELECT ON {READER_TABLES} TO aikb_reader")
    op.execute("GRANT SELECT, INSERT ON retrieval_trace TO aikb_reader")

    for table, expression in READ_POLICIES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY aikb_read_visible ON {table} FOR SELECT "
            f"TO aikb_reader USING ({expression})"
        )

    trace_expression = (
        "principal_id=nullif(current_setting('aikb.principal_id', true), '') "
        "AND security_domain_id=nullif("
        "current_setting('aikb.security_domain_id', true), '')"
    )
    op.execute("ALTER TABLE retrieval_trace ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY aikb_trace_own ON retrieval_trace TO aikb_reader "
        f"USING ({trace_expression}) WITH CHECK ({trace_expression})"
    )
    op.execute(
        "UPDATE schema_metadata SET value = '4' "
        "WHERE key = 'postgres_schema_version'"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS aikb_trace_own ON retrieval_trace")
    op.execute("ALTER TABLE retrieval_trace DISABLE ROW LEVEL SECURITY")
    for table in reversed(READ_POLICIES):
        op.execute(f"DROP POLICY IF EXISTS aikb_read_visible ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(f"REVOKE ALL PRIVILEGES ON {READER_TABLES} FROM aikb_reader")
    op.execute("REVOKE ALL PRIVILEGES ON retrieval_trace FROM aikb_reader")
    op.execute("DROP FUNCTION IF EXISTS aikb_can_read_solution(text)")
    op.execute("DROP FUNCTION IF EXISTS aikb_can_read_solution_snapshot(text)")
    op.execute("DROP FUNCTION IF EXISTS aikb_can_read_repository(text)")
    bind = op.get_bind()
    for table in (
        repository_grant,
        security_team_member,
        security_team,
        principal,
        security_domain,
    ):
        table.drop(bind=bind, checkfirst=True)
    op.execute(
        "UPDATE schema_metadata SET value = '3' "
        "WHERE key = 'postgres_schema_version'"
    )
