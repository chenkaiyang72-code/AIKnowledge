from __future__ import annotations

import os
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from aikb.catalog import Catalog
from aikb.context_pack import build_context_pack, build_solution_context_pack
from aikb.ingestion import ingest_source
from aikb.mcp_server import MCPReadConfig, MCPReadService
from aikb.postgres_catalog import PostgresCatalog, PostgresPrincipalContext
from aikb.oidc import PostgresPrincipalDirectory
from aikb.postgres_publish import PostgresSnapshotPublisher
from aikb.postgres_solution import (
    PostgresSolutionPublisher,
    resolve_postgres_solution_scope,
)
from aikb.postgres_schema_v1 import metadata
from aikb.postgres_auth_schema import auth_metadata
from aikb.postgres_security_schema import security_metadata
from aikb.postgres_solution_schema import solution_metadata
from aikb.solution import SolutionManifest


EXPECTED_TABLES = {
    "schema_metadata",
    "repository",
    "snapshot",
    "snapshot_event",
    "solution",
    "solution_snapshot",
    "solution_snapshot_event",
    "solution_snapshot_member",
    "blob",
    "analysis_artifact",
    "source_file",
    "chunk",
    "logical_symbol",
    "source_condition",
    "symbol_occurrence",
    "relation",
    "embedding_model",
    "chunk_embedding",
    "retrieval_trace",
    "security_domain",
    "principal",
    "security_team",
    "security_team_member",
    "repository_grant",
    "repository_grant_source",
    "mcp_audit_event",
}


class PostgresSchemaUnitTests(unittest.TestCase):
    def test_schema_contains_versioned_catalog_and_vector_tables(self) -> None:
        current_tables = (
            set(metadata.tables)
            | set(solution_metadata.tables)
            | set(security_metadata.tables)
            | set(auth_metadata.tables)
        )
        self.assertEqual(current_tables, EXPECTED_TABLES)
        self.assertNotIn("chunk_fts", metadata.tables)
        snapshot_indexes = {index.name for index in metadata.tables["snapshot"].indexes}
        self.assertIn("uq_snapshot_one_active_per_repository", snapshot_indexes)

        embedding_ddl = str(
            CreateTable(metadata.tables["chunk_embedding"]).compile(
                dialect=postgresql.dialect()
            )
        )
        trace_ddl = str(
            CreateTable(metadata.tables["retrieval_trace"]).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("VECTOR", embedding_ddl)
        self.assertIn("JSONB", trace_ddl)


POSTGRES_URL = os.environ.get("AIKB_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "AIKB_TEST_POSTGRES_URL is not configured")
class PostgresMigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from alembic import command
        from alembic.config import Config

        cls.config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        cls.config.set_main_option("sqlalchemy.url", POSTGRES_URL)
        command.upgrade(cls.config, "head")
        cls.engine = create_engine(POSTGRES_URL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_legacy_v5_grant_is_backfilled_without_losing_visibility(self) -> None:
        from alembic import command

        suffix = uuid.uuid4().hex
        domain_id = f"domain_legacy_{suffix}"
        principal_id = f"principal_legacy_{suffix}"
        repository_id = f"repo_legacy_{suffix}"
        repository_name = f"legacy-{suffix}"
        grant_id = f"grant_legacy_{suffix}"
        command.downgrade(self.config, "0005_oidc_principal_directory")
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO security_domain(id,name) VALUES (:id,:name)"),
                    {"id": domain_id, "name": f"Legacy {suffix}"},
                )
                connection.execute(
                    text(
                        "INSERT INTO principal(id,security_domain_id,issuer,subject) "
                        "VALUES (:id,:domain,'https://issuer.example',:subject)"
                    ),
                    {
                        "id": principal_id,
                        "domain": domain_id,
                        "subject": f"legacy-{suffix}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO repository(id,name,source_kind,source_uri) "
                        "VALUES (:id,:name,'test','test://legacy')"
                    ),
                    {"id": repository_id, "name": repository_name},
                )
                connection.execute(
                    text(
                        "INSERT INTO repository_grant(id,security_domain_id,"
                        "repository_id,principal_id,permission) VALUES "
                        "(:id,:domain,:repository,:principal,'read')"
                    ),
                    {
                        "id": grant_id,
                        "domain": domain_id,
                        "repository": repository_id,
                        "principal": principal_id,
                    },
                )
            command.upgrade(self.config, "head")
            with self.engine.connect() as connection:
                transaction = connection.begin()
                source = connection.execute(
                    text(
                        "SELECT source_kind,source_key,permission,revoked_at "
                        "FROM repository_grant_source "
                        "WHERE repository_grant_id=:grant"
                    ),
                    {"grant": grant_id},
                ).one()
                connection.execute(text("SET LOCAL ROLE aikb_reader"))
                connection.execute(
                    text("SELECT set_config('aikb.principal_id',:value,true)"),
                    {"value": principal_id},
                )
                connection.execute(
                    text(
                        "SELECT set_config('aikb.security_domain_id',:value,true)"
                    ),
                    {"value": domain_id},
                )
                visible = connection.execute(
                    text("SELECT name FROM repository")
                ).scalars().all()
                transaction.rollback()
            self.assertEqual(tuple(source), ("legacy", grant_id, "read", None))
            self.assertEqual(visible, [repository_name])
        finally:
            command.upgrade(self.config, "head")
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM security_domain WHERE id=:id"),
                    {"id": domain_id},
                )
                connection.execute(
                    text("DELETE FROM repository WHERE id=:id"),
                    {"id": repository_id},
                )

    def test_migration_is_current_and_pgvector_is_available(self) -> None:
        inspector = inspect(self.engine)
        self.assertTrue(EXPECTED_TABLES.issubset(set(inspector.get_table_names())))
        with self.engine.connect() as connection:
            version = connection.execute(
                text(
                    "SELECT value FROM schema_metadata "
                    "WHERE key = 'postgres_schema_version'"
                )
            ).scalar_one()
            extension = connection.execute(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            ).scalar_one()
        self.assertEqual(version, "6")
        self.assertTrue(extension)
        self.assertIn("content", {column["name"] for column in inspector.get_columns("chunk")})
        self.assertIn(
            "tokens_valid_after",
            {column["name"] for column in inspector.get_columns("principal")},
        )

    def test_reader_rls_filters_repository_content_solution_members_and_traces(self) -> None:
        suffix = uuid.uuid4().hex
        domain_id = f"domain_{suffix}"
        alice_id = f"principal_alice_{suffix}"
        bob_id = f"principal_bob_{suffix}"
        team_id = f"team_{suffix}"
        visible_repository_id = f"repo_visible_{suffix}"
        hidden_repository_id = f"repo_hidden_{suffix}"
        visible_repository = f"visible-{suffix}"
        hidden_repository = f"hidden-{suffix}"
        visible_snapshot = f"snap_visible_{suffix}"
        hidden_snapshot = f"snap_hidden_{suffix}"
        visible_blob = ("a" + suffix)[:64].ljust(64, "a")
        hidden_blob = ("b" + suffix)[:64].ljust(64, "b")
        visible_file = f"file_visible_{suffix}"
        hidden_file = f"file_hidden_{suffix}"
        visible_chunk = f"chunk_visible_{suffix}"
        hidden_chunk = f"chunk_hidden_{suffix}"
        solution_id = f"solution_{suffix}"
        solution_snapshot_id = f"solution_snapshot_{suffix}"
        alice_trace = f"trace_alice_{suffix}"

        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO security_domain(id,name) VALUES (:id,:name)"
                ),
                {"id": domain_id, "name": f"domain-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO principal(id,security_domain_id,issuer,subject,display_name) "
                    "VALUES (:alice,:domain,'https://issuer.example',:alice_subject,'Alice'),"
                    "(:bob,:domain,'https://issuer.example',:bob_subject,'Bob')"
                ),
                {
                    "alice": alice_id,
                    "bob": bob_id,
                    "domain": domain_id,
                    "alice_subject": f"alice-{suffix}",
                    "bob_subject": f"bob-{suffix}",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO security_team(id,security_domain_id,name) "
                    "VALUES (:id,:domain,:name)"
                ),
                {"id": team_id, "domain": domain_id, "name": f"kernel-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO security_team_member(team_id,principal_id) "
                    "VALUES (:team,:principal)"
                ),
                {"team": team_id, "principal": alice_id},
            )
            connection.execute(
                text(
                    "INSERT INTO repository(id,name,source_kind,source_uri) VALUES "
                    "(:visible_id,:visible_name,'test','test://visible'),"
                    "(:hidden_id,:hidden_name,'test','test://hidden')"
                ),
                {
                    "visible_id": visible_repository_id,
                    "visible_name": visible_repository,
                    "hidden_id": hidden_repository_id,
                    "hidden_name": hidden_repository,
                },
            )
            for snapshot_id, repository_id, digest in (
                (visible_snapshot, visible_repository_id, "1"),
                (hidden_snapshot, hidden_repository_id, "2"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO snapshot(id,repository_id,revision,source_digest,"
                        "manifest_digest,index_profile_digest,state,file_count,blob_count,"
                        "chunk_count) VALUES (:id,:repository,'rev',:source,:manifest,"
                        ":profile,'active',1,1,1)"
                    ),
                    {
                        "id": snapshot_id,
                        "repository": repository_id,
                        "source": digest * 64,
                        "manifest": (str(int(digest) + 2)) * 64,
                        "profile": (str(int(digest) + 4)) * 64,
                    },
                )
            for blob_id in (visible_blob, hidden_blob):
                connection.execute(
                    text(
                        "INSERT INTO blob(id,size_bytes,compressed_content) "
                        "VALUES (:id,1,:content)"
                    ),
                    {"id": blob_id, "content": b"x"},
                )
            for file_id, snapshot_id, blob_id, path in (
                (visible_file, visible_snapshot, visible_blob, "visible.c"),
                (hidden_file, hidden_snapshot, hidden_blob, "hidden.c"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO source_file(id,snapshot_id,blob_id,path,language,"
                        "line_count,size_bytes,decode_status,parse_status) VALUES "
                        "(:id,:snapshot,:blob,:path,'c',1,1,'utf8','structured')"
                    ),
                    {"id": file_id, "snapshot": snapshot_id, "blob": blob_id, "path": path},
                )
            for chunk_id, snapshot_id, file_id, content in (
                (visible_chunk, visible_snapshot, visible_file, "visible evidence"),
                (hidden_chunk, hidden_snapshot, hidden_file, "hidden evidence"),
            ):
                connection.execute(
                    text(
                        "INSERT INTO chunk(id,snapshot_id,file_id,ordinal,kind,start_line,"
                        "end_line,content_hash,generator,content) VALUES "
                        "(:id,:snapshot,:file,0,'window',1,1,:hash,'test',:content)"
                    ),
                    {
                        "id": chunk_id,
                        "snapshot": snapshot_id,
                        "file": file_id,
                        "hash": ("c" if "visible" in content else "d") * 64,
                        "content": content,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO solution(id,name) VALUES (:id,:name)"
                ),
                {"id": solution_id, "name": f"solution-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO solution_snapshot(id,solution_id,revision,manifest_digest,"
                    "manifest_json,state,member_count) VALUES "
                    "(:id,:solution,'r1',:digest,CAST(:manifest AS jsonb),'active',2)"
                ),
                {
                    "id": solution_snapshot_id,
                    "solution": solution_id,
                    "digest": "e" * 64,
                    "manifest": '{"schema_version":"1"}',
                },
            )
            connection.execute(
                text(
                    "INSERT INTO solution_snapshot_member(solution_snapshot_id,"
                    "repository_id,snapshot_id,role,ordinal) VALUES "
                    "(:solution,:visible_repo,:visible_snapshot,'visible',0),"
                    "(:solution,:hidden_repo,:hidden_snapshot,'hidden',1)"
                ),
                {
                    "solution": solution_snapshot_id,
                    "visible_repo": visible_repository_id,
                    "visible_snapshot": visible_snapshot,
                    "hidden_repo": hidden_repository_id,
                    "hidden_snapshot": hidden_snapshot,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO repository_grant(id,security_domain_id,repository_id,"
                    "team_id,permission) VALUES (:id,:domain,:repository,:team,'read')"
                ),
                {
                    "id": f"grant_team_{suffix}",
                    "domain": domain_id,
                    "repository": visible_repository_id,
                    "team": team_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO repository_grant(id,security_domain_id,repository_id,"
                    "principal_id,permission) VALUES (:id,:domain,:repository,:principal,'read')"
                ),
                {
                    "id": f"grant_bob_{suffix}",
                    "domain": domain_id,
                    "repository": hidden_repository_id,
                    "principal": bob_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO repository_grant_source(id,repository_grant_id,"
                    "source_kind,source_key,permission) VALUES "
                    "(:team_source,:team_grant,'manual','integration-test','read'),"
                    "(:bob_source,:bob_grant,'manual','integration-test','read')"
                ),
                {
                    "team_source": f"grant_source_team_{suffix}",
                    "team_grant": f"grant_team_{suffix}",
                    "bob_source": f"grant_source_bob_{suffix}",
                    "bob_grant": f"grant_bob_{suffix}",
                },
            )

        def visible_rows(principal_id: str, security_domain_id: str) -> tuple[list[str], list[str], list[str]]:
            with self.engine.connect() as connection:
                transaction = connection.begin()
                connection.execute(text("SET LOCAL ROLE aikb_reader"))
                connection.execute(
                    text("SELECT set_config('aikb.principal_id',:value,true)"),
                    {"value": principal_id},
                )
                connection.execute(
                    text("SELECT set_config('aikb.security_domain_id',:value,true)"),
                    {"value": security_domain_id},
                )
                repositories = connection.execute(
                    text("SELECT name FROM repository ORDER BY name")
                ).scalars().all()
                contents = connection.execute(
                    text("SELECT content FROM chunk ORDER BY content")
                ).scalars().all()
                members = connection.execute(
                    text(
                        "SELECT repository_id FROM solution_snapshot_member "
                        "ORDER BY repository_id"
                    )
                ).scalars().all()
                transaction.rollback()
            return repositories, contents, members

        try:
            with self.engine.connect() as connection:
                role = connection.execute(
                    text(
                        "SELECT rolsuper,rolbypassrls,rolcanlogin FROM pg_roles "
                        "WHERE rolname='aikb_reader'"
                    )
                ).one()
            self.assertEqual(tuple(role), (False, False, False))
            with self.engine.connect() as connection:
                authenticator_role = connection.execute(
                    text(
                        "SELECT rolsuper,rolbypassrls,rolcanlogin FROM pg_roles "
                        "WHERE rolname='aikb_authenticator'"
                    )
                ).one()
            self.assertEqual(tuple(authenticator_role), (False, False, False))

            directory = PostgresPrincipalDirectory(POSTGRES_URL, engine=self.engine)
            mapped = directory.resolve("https://issuer.example", f"alice-{suffix}")
            self.assertIsNotNone(mapped)
            assert mapped is not None
            self.assertEqual(mapped.principal_id, alice_id)
            self.assertEqual(mapped.security_domain_id, domain_id)
            self.assertEqual(mapped.tokens_valid_after, 0)
            self.assertIsNone(
                directory.resolve("https://issuer.example", f"unknown-{suffix}")
            )

            alice_rows = visible_rows(alice_id, domain_id)
            self.assertEqual(alice_rows[0], [visible_repository])
            self.assertEqual(alice_rows[1], ["visible evidence"])
            self.assertEqual(alice_rows[2], [visible_repository_id])

            secured_adapter = PostgresCatalog(
                POSTGRES_URL,
                engine=self.engine,
                principal_context=PostgresPrincipalContext(
                    principal_id=alice_id,
                    security_domain_id=domain_id,
                ),
            )
            resolved = secured_adapter.resolve_snapshots()
            self.assertEqual(
                [(row["repository"], row["snapshot_id"]) for row in resolved],
                [(visible_repository, visible_snapshot)],
            )
            secured_hits = secured_adapter.search("evidence")
            self.assertEqual([hit.content for hit in secured_hits], ["visible evidence"])
            with self.assertRaises(ValueError):
                secured_adapter.resolve_snapshots(repository=hidden_repository)
            secured_solution = resolve_postgres_solution_scope(
                self.engine,
                solution=f"solution-{suffix}",
                principal_context=PostgresPrincipalContext(
                    principal_id=alice_id,
                    security_domain_id=domain_id,
                ),
            )
            self.assertTrue(secured_solution.partial_visibility)
            self.assertEqual(
                [member["repository"] for member in secured_solution.snapshots],
                [visible_repository],
            )

            service = MCPReadService(
                MCPReadConfig(
                    postgres_engine=self.engine,
                    token_verifier=object(),
                    auth_settings=AuthSettings(
                        issuer_url="https://issuer.example",
                        resource_server_url="https://kb.example/mcp/read",
                        required_scopes=["aiknowledge.read"],
                    ),
                )
            )
            access_token = AccessToken(
                token="verified-test-token",
                client_id="integration-test",
                scopes=["aiknowledge.read"],
                expires_at=None,
                resource="https://kb.example/mcp/read",
                subject=f"alice-{suffix}",
                claims={
                    "iss": "https://issuer.example",
                    "aikb_principal_id": alice_id,
                    "aikb_security_domain_id": domain_id,
                },
            )
            auth_token = auth_context_var.set(AuthenticatedUser(access_token))
            try:
                pack = service.execute_read(
                    tool_name="aikb_context_search",
                    request_id=f"request-success-{suffix}",
                    scope_kind="repository",
                    scope_identifier=visible_repository,
                    query_text="evidence",
                    operation=lambda: service.search_context(
                        query="evidence",
                        repository=visible_repository,
                        max_evidence_items=2,
                    ),
                    summarize=lambda result: {"evidence_count": len(result.evidence)},
                )
                self.assertEqual(
                    [item.repository for item in pack.evidence],
                    [visible_repository],
                )
                with self.assertRaises(ValueError):
                    service.execute_read(
                        tool_name="aikb_context_search",
                        request_id=f"request-error-{suffix}",
                        scope_kind="repository",
                        scope_identifier=hidden_repository,
                        query_text="hidden evidence",
                        operation=lambda: service.search_context(
                            query="hidden evidence",
                            repository=hidden_repository,
                        ),
                        summarize=lambda result: {
                            "evidence_count": len(result.evidence)
                        },
                    )
            finally:
                auth_context_var.reset(auth_token)

            with self.engine.connect() as connection:
                audit_rows = connection.execute(
                    text(
                        "SELECT outcome,query_hash,trace_id,scope_summary,result_summary "
                        "FROM mcp_audit_event WHERE principal_id=:principal "
                        "ORDER BY request_id"
                    ),
                    {"principal": alice_id},
                ).mappings().all()
            self.assertEqual([row["outcome"] for row in audit_rows], ["error", "success"])
            self.assertTrue(all(len(row["query_hash"]) == 64 for row in audit_rows))
            serialized_audit = str([dict(row) for row in audit_rows])
            self.assertNotIn("hidden evidence", serialized_audit)
            self.assertNotIn(hidden_repository, serialized_audit)

            bob_rows = visible_rows(bob_id, domain_id)
            self.assertEqual(bob_rows[0], [hidden_repository])
            self.assertEqual(bob_rows[1], ["hidden evidence"])
            self.assertEqual(bob_rows[2], [hidden_repository_id])

            wrong_domain_rows = visible_rows(alice_id, f"wrong-{domain_id}")
            self.assertEqual(wrong_domain_rows, ([], [], []))

            with self.engine.begin() as connection:
                connection.execute(text("SET LOCAL ROLE aikb_reader"))
                connection.execute(
                    text("SELECT set_config('aikb.principal_id',:value,true)"),
                    {"value": alice_id},
                )
                connection.execute(
                    text("SELECT set_config('aikb.security_domain_id',:value,true)"),
                    {"value": domain_id},
                )
                connection.execute(
                    text(
                        "INSERT INTO retrieval_trace(id,principal_id,security_domain_id,"
                        "query_hash,scope,retriever_versions,budget,result_summary) VALUES "
                        "(:id,:principal,:domain,:hash,CAST('{}' AS jsonb),"
                        "CAST('{}' AS jsonb),CAST('{}' AS jsonb),CAST('{}' AS jsonb))"
                    ),
                    {
                        "id": alice_trace,
                        "principal": alice_id,
                        "domain": domain_id,
                        "hash": "f" * 64,
                    },
                )
                traces = connection.execute(
                    text("SELECT id FROM retrieval_trace")
                ).scalars().all()
                self.assertEqual(traces, [alice_trace])
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM retrieval_trace WHERE id=:id"), {"id": alice_trace}
                )
                connection.execute(
                    text("DELETE FROM mcp_audit_event WHERE principal_id=:id"),
                    {"id": alice_id},
                )
                connection.execute(
                    text("DELETE FROM solution WHERE id=:id"), {"id": solution_id}
                )
                connection.execute(
                    text("DELETE FROM repository WHERE id IN (:visible,:hidden)"),
                    {"visible": visible_repository_id, "hidden": hidden_repository_id},
                )
                connection.execute(
                    text("DELETE FROM blob WHERE id IN (:visible,:hidden)"),
                    {"visible": visible_blob, "hidden": hidden_blob},
                )
                connection.execute(
                    text("DELETE FROM security_domain WHERE id=:id"), {"id": domain_id}
                )

    def test_only_one_active_snapshot_is_allowed_per_repository(self) -> None:
        suffix = uuid.uuid4().hex
        repository_id = f"repo_{suffix}"
        first_snapshot = f"snap_first_{suffix}"
        second_snapshot = f"snap_second_{suffix}"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository(id, name, source_kind, source_uri) "
                    "VALUES (:id, :name, 'test', 'test://source')"
                ),
                {"id": repository_id, "name": f"integration-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO snapshot("
                    "id, repository_id, revision, source_digest, manifest_digest, "
                    "index_profile_digest, state) VALUES ("
                    ":id, :repository_id, 'rev-1', :digest, :manifest, :profile, 'active')"
                ),
                {
                    "id": first_snapshot,
                    "repository_id": repository_id,
                    "digest": "a" * 64,
                    "manifest": "b" * 64,
                    "profile": "c" * 64,
                },
            )
        try:
            with self.assertRaises(IntegrityError):
                with self.engine.begin() as connection:
                    connection.execute(
                        text(
                            "INSERT INTO snapshot("
                            "id, repository_id, revision, source_digest, "
                            "manifest_digest, index_profile_digest, state) VALUES ("
                            ":id, :repository_id, 'rev-2', :digest, :manifest, "
                            ":profile, 'active')"
                        ),
                        {
                            "id": second_snapshot,
                            "repository_id": repository_id,
                            "digest": "d" * 64,
                            "manifest": "e" * 64,
                            "profile": "f" * 64,
                        },
                    )
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM snapshot WHERE repository_id = :repository_id"),
                    {"repository_id": repository_id},
                )
                connection.execute(
                    text("DELETE FROM repository WHERE id = :repository_id"),
                    {"repository_id": repository_id},
                )

    def test_postgres_read_adapter_builds_context_pack(self) -> None:
        suffix = uuid.uuid4().hex
        repository_id = f"repo_{suffix}"
        snapshot_id = f"snap_{suffix}"
        blob_id = "1" * 64
        file_id = f"file_{suffix}"
        chunk_id = f"chunk_{suffix}"
        symbol_id = f"symbol_{suffix}"
        occurrence_id = f"occurrence_{suffix}"
        content = "int init_idle(void) { return 0; }\n"
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository(id,name,source_kind,source_uri) "
                    "VALUES (:id,:name,'test','test://source')"
                ),
                {"id": repository_id, "name": f"adapter-{suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO snapshot(id,repository_id,revision,source_digest,"
                    "manifest_digest,index_profile_digest,state,file_count,blob_count,"
                    "chunk_count,symbol_occurrence_count) VALUES "
                    "(:id,:repository_id,'rev-1',:source,:manifest,:profile,'active',1,1,1,1)"
                ),
                {
                    "id": snapshot_id,
                    "repository_id": repository_id,
                    "source": "2" * 64,
                    "manifest": "3" * 64,
                    "profile": "4" * 64,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO blob(id,size_bytes,compressed_content) "
                    "VALUES (:id,:size,:content)"
                ),
                {"id": blob_id, "size": len(content), "content": b"placeholder"},
            )
            connection.execute(
                text(
                    "INSERT INTO source_file(id,snapshot_id,blob_id,path,language,"
                    "line_count,size_bytes,decode_status,parse_status) VALUES "
                    "(:id,:snapshot,:blob,'kernel/demo.c','c',1,:size,'utf8','structured')"
                ),
                {
                    "id": file_id,
                    "snapshot": snapshot_id,
                    "blob": blob_id,
                    "size": len(content),
                },
            )
            connection.execute(
                text(
                    "INSERT INTO chunk(id,snapshot_id,file_id,ordinal,kind,start_line,"
                    "end_line,symbol,content_hash,generator,content) VALUES "
                    "(:id,:snapshot,:file,0,'function',1,1,'init_idle',:hash,"
                    "'tree-sitter-c-v3',:content)"
                ),
                {
                    "id": chunk_id,
                    "snapshot": snapshot_id,
                    "file": file_id,
                    "hash": "5" * 64,
                    "content": content,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO logical_symbol(id,repository_id,language,kind,namespace,"
                    "name,signature) VALUES "
                    "(:id,:repository,'c','function','repository','init_idle',:signature)"
                ),
                {
                    "id": symbol_id,
                    "repository": repository_id,
                    "signature": "int init_idle(void)",
                },
            )
            connection.execute(
                text(
                    "INSERT INTO symbol_occurrence(id,snapshot_id,file_id,logical_symbol_id,"
                    "role,start_line,end_line,confidence,generator) VALUES "
                    "(:id,:snapshot,:file,:symbol,'definition',1,1,'source_exact',"
                    "'source-relations-v2')"
                ),
                {
                    "id": occurrence_id,
                    "snapshot": snapshot_id,
                    "file": file_id,
                    "symbol": symbol_id,
                },
            )
        try:
            adapter = PostgresCatalog(POSTGRES_URL, engine=self.engine)
            pack = build_context_pack(
                adapter,
                "init_idle",
                repository=f"adapter-{suffix}",
                max_evidence_items=2,
            )
            self.assertEqual(pack.evidence[0].symbol, "init_idle")
            self.assertEqual(pack.evidence[0].snapshot_id, snapshot_id)
            self.assertIn("symbol_exact", {
                item.channel for item in pack.evidence[0].retrieval.contributions
            })
            lexical_hits = adapter.search(
                "init_idle definitely_absent",
                repository=f"adapter-{suffix}",
            )
            self.assertEqual(lexical_hits[0].symbol, "init_idle")
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM repository WHERE id=:id"), {"id": repository_id}
                )
                connection.execute(text("DELETE FROM blob WHERE id=:id"), {"id": blob_id})

    def test_snapshot_publisher_is_atomic_idempotent_and_reversible(self) -> None:
        suffix = uuid.uuid4().hex
        project = f"publisher-{suffix}"
        repository_id: str | None = None
        blob_ids: set[str] = set()

        class FailingPublisher(PostgresSnapshotPublisher):
            def _validate_counts(
                self,
                target: Connection,
                snapshot: dict[str, Any],
            ) -> None:
                super()._validate_counts(target, snapshot)
                raise RuntimeError("injected validation failure")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "linux"
            source.mkdir()
            (source / "Makefile").write_text(
                "VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 40\nEXTRAVERSION =\n",
                encoding="utf-8",
            )
            (source / "Kconfig").write_text('mainmenu "test"\n', encoding="utf-8")
            for directory in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
                (source / directory).mkdir()
            symbol = f"publish_{suffix}"
            header = source / "include" / "publisher.h"
            implementation = source / "kernel" / "publisher.c"
            header.write_text(f"int {symbol}(void);\n", encoding="utf-8")
            implementation.write_text(
                '#include "../include/publisher.h"\n'
                f"int {symbol}(void) {{ return 1; }}\n",
                encoding="utf-8",
            )
            scope = {
                "scope_id": f"publisher-scope-{suffix}",
                "source": {
                    "project": project,
                    "version": "6.18.40",
                    "kind": "release_archive",
                    "archive_name": f"linux-{suffix}.tar.xz",
                    "archive_sha256": suffix * 2,
                    "git_commit": None,
                },
                "include_roots": ["kernel", "include"],
                "exclude_globs": [],
                "index_policy": {
                    "mode": "source_only",
                    "execute_build": False,
                    "requires_build_artifacts": False,
                },
            }
            try:
                with Catalog(root / "catalog.db") as catalog:
                    catalog.initialize()
                    first = ingest_source(catalog, scope, source)
                    blob_ids.update(
                        row["id"]
                        for row in catalog.connection.execute("SELECT id FROM blob")
                    )
                    repository_id = catalog.connection.execute(
                        "SELECT repository_id FROM snapshot WHERE id=?", (first["id"],)
                    ).fetchone()["repository_id"]
                    publisher = PostgresSnapshotPublisher(self.engine, batch_size=1)
                    first_publish = publisher.publish(catalog, first["id"])
                    self.assertFalse(first_publish.idempotent)
                    self.assertEqual(first_publish.state, "active")

                    with self.engine.connect() as connection:
                        states = connection.execute(
                            text(
                                "SELECT state FROM snapshot_event WHERE snapshot_id=:id "
                                "ORDER BY id"
                            ),
                            {"id": first["id"]},
                        ).scalars().all()
                    self.assertEqual(states, ["building", "validated", "active"])

                    adapter = PostgresCatalog(POSTGRES_URL, engine=self.engine)
                    pack = build_context_pack(
                        adapter,
                        symbol,
                        repository=project,
                        max_evidence_items=2,
                    )
                    self.assertEqual(pack.evidence[0].symbol, symbol)
                    self.assertEqual(pack.evidence[0].snapshot_id, first["id"])

                    repeated = publisher.publish(catalog, first["id"])
                    self.assertTrue(repeated.idempotent)
                    self.assertFalse(repeated.reactivated)
                    with self.engine.connect() as connection:
                        event_count = connection.execute(
                            text(
                                "SELECT count(*) FROM snapshot_event "
                                "WHERE snapshot_id=:id"
                            ),
                            {"id": first["id"]},
                        ).scalar_one()
                    self.assertEqual(event_count, 3)

                    implementation.write_text(
                        '#include "../include/publisher.h"\n'
                        f"int {symbol}(void) {{ return 2; }}\n",
                        encoding="utf-8",
                    )
                    second = ingest_source(catalog, scope, source)
                    blob_ids.update(
                        row["id"]
                        for row in catalog.connection.execute("SELECT id FROM blob")
                    )
                    second_publish = publisher.publish(catalog, second["id"])
                    self.assertEqual(
                        second_publish.superseded_snapshot_ids, (first["id"],)
                    )
                    with self.engine.connect() as connection:
                        snapshot_states = dict(
                            connection.execute(
                                text(
                                    "SELECT id,state FROM snapshot "
                                    "WHERE repository_id=:repository_id"
                                ),
                                {"repository_id": repository_id},
                            ).all()
                        )
                    self.assertEqual(
                        snapshot_states,
                        {first["id"]: "superseded", second["id"]: "active"},
                    )

                    implementation.write_text(
                        '#include "../include/publisher.h"\n'
                        f"int {symbol}(void) {{ return 3; }}\n",
                        encoding="utf-8",
                    )
                    third = ingest_source(catalog, scope, source)
                    blob_ids.update(
                        row["id"]
                        for row in catalog.connection.execute("SELECT id FROM blob")
                    )
                    with self.assertRaisesRegex(
                        RuntimeError, "injected validation failure"
                    ):
                        FailingPublisher(self.engine, batch_size=1).publish(
                            catalog, third["id"]
                        )
                    with self.engine.connect() as connection:
                        failed_snapshot_count = connection.execute(
                            text("SELECT count(*) FROM snapshot WHERE id=:id"),
                            {"id": third["id"]},
                        ).scalar_one()
                        active_snapshot = connection.execute(
                            text(
                                "SELECT id FROM snapshot WHERE repository_id=:repository_id "
                                "AND state='active'"
                            ),
                            {"repository_id": repository_id},
                        ).scalar_one()
                    self.assertEqual(failed_snapshot_count, 0)
                    self.assertEqual(active_snapshot, second["id"])

                    reactivated = publisher.publish(catalog, first["id"])
                    self.assertTrue(reactivated.idempotent)
                    self.assertTrue(reactivated.reactivated)
                    self.assertEqual(
                        reactivated.superseded_snapshot_ids, (second["id"],)
                    )
                    with self.engine.connect() as connection:
                        active_snapshot = connection.execute(
                            text(
                                "SELECT id FROM snapshot WHERE repository_id=:repository_id "
                                "AND state='active'"
                            ),
                            {"repository_id": repository_id},
                        ).scalar_one()
                    self.assertEqual(active_snapshot, first["id"])
            finally:
                if repository_id is not None:
                    with self.engine.begin() as connection:
                        connection.execute(
                            text("DELETE FROM repository WHERE id=:id"),
                            {"id": repository_id},
                        )
                        for blob_id in blob_ids:
                            connection.execute(
                                text("DELETE FROM blob WHERE id=:id"), {"id": blob_id}
                            )

    def test_solution_publisher_pins_multiple_repositories_and_filters_visibility(
        self,
    ) -> None:
        suffix = uuid.uuid4().hex
        projects = [f"solution-core-{suffix}", f"solution-driver-{suffix}"]
        repository_ids: list[str] = []
        blob_ids: set[str] = set()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local_catalog_path = root / "catalog.db"
            with Catalog(local_catalog_path) as catalog:
                catalog.initialize()
                snapshot_ids: list[str] = []
                for index, project in enumerate(projects):
                    source = root / project
                    source.mkdir()
                    (source / "Makefile").write_text(
                        "VERSION = 1\nPATCHLEVEL = 0\nSUBLEVEL = 0\nEXTRAVERSION =\n",
                        encoding="utf-8",
                    )
                    (source / "Kconfig").write_text(
                        'mainmenu "solution"\n', encoding="utf-8"
                    )
                    for directory in [
                        "arch", "drivers", "fs", "kernel", "mm", "include"
                    ]:
                        (source / directory).mkdir()
                    symbol = "solution_core" if index == 0 else "solution_driver"
                    (source / "kernel" / "fixture.c").write_text(
                        f"int {symbol}(void) {{ return {index}; }}\n",
                        encoding="utf-8",
                    )
                    scope = {
                        "scope_id": f"scope-{project}",
                        "source": {
                            "project": project,
                            "version": "1.0.0",
                            "kind": "release_archive",
                            "archive_name": f"{project}.tar.xz",
                            "archive_sha256": ("a" if index == 0 else "b") * 64,
                            "git_commit": None,
                        },
                        "include_roots": ["kernel"],
                        "exclude_globs": [],
                        "index_policy": {
                            "mode": "source_only",
                            "execute_build": False,
                            "requires_build_artifacts": False,
                        },
                    }
                    snapshot = ingest_source(catalog, scope, source)
                    snapshot_ids.append(snapshot["id"])
                    repository_ids.append(
                        catalog.connection.execute(
                            "SELECT repository_id FROM snapshot WHERE id=?",
                            (snapshot["id"],),
                        ).fetchone()["repository_id"]
                    )
                blob_ids.update(
                    row["id"] for row in catalog.connection.execute("SELECT id FROM blob")
                )
                snapshot_publisher = PostgresSnapshotPublisher(self.engine)
                for snapshot_id in snapshot_ids:
                    snapshot_publisher.publish(catalog, snapshot_id)

                manifest = SolutionManifest.model_validate(
                    {
                        "name": f"solution-{suffix}",
                        "revision": "r1",
                        "members": [
                            {
                                "repository": projects[0],
                                "snapshot_id": snapshot_ids[0],
                                "role": "core",
                            },
                            {
                                "repository": projects[1],
                                "snapshot_id": snapshot_ids[1],
                                "role": "driver",
                            },
                        ],
                    }
                )
                publisher = PostgresSolutionPublisher(self.engine)
                first = publisher.publish(manifest)
                repeated = publisher.publish(manifest)
                self.assertFalse(first.idempotent)
                self.assertTrue(repeated.idempotent)

                scope = resolve_postgres_solution_scope(
                    self.engine, manifest.name
                )
                adapter = PostgresCatalog(POSTGRES_URL, engine=self.engine)
                pack = build_solution_context_pack(
                    adapter,
                    "solution_core solution_driver",
                    scope,
                    max_evidence_items=4,
                )
                self.assertEqual(
                    {item.repository for item in pack.evidence}, set(projects)
                )
                restricted = resolve_postgres_solution_scope(
                    self.engine,
                    manifest.name,
                    allowed_repositories={projects[0]},
                )
                restricted_pack = build_solution_context_pack(
                    adapter, "solution_core", restricted, max_evidence_items=4
                )
                serialized = restricted_pack.model_dump_json()
                self.assertTrue(restricted_pack.scope.partial_visibility)
                self.assertNotIn(projects[1], serialized)
                self.assertNotIn(snapshot_ids[1], serialized)

        with self.engine.begin() as connection:
            connection.execute(
                text("DELETE FROM solution WHERE name=:name"),
                {"name": f"solution-{suffix}"},
            )
            for repository_id in repository_ids:
                connection.execute(
                    text("DELETE FROM repository WHERE id=:id"),
                    {"id": repository_id},
                )
            for blob_id in blob_ids:
                connection.execute(
                    text("DELETE FROM blob WHERE id=:id"), {"id": blob_id}
                )


if __name__ == "__main__":
    unittest.main()
