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

from aikb.catalog import Catalog
from aikb.context_pack import build_context_pack
from aikb.ingestion import ingest_source
from aikb.postgres_catalog import PostgresCatalog
from aikb.postgres_publish import PostgresSnapshotPublisher
from aikb.postgres_schema_v1 import metadata


EXPECTED_TABLES = {
    "schema_metadata",
    "repository",
    "snapshot",
    "snapshot_event",
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
}


class PostgresSchemaUnitTests(unittest.TestCase):
    def test_schema_contains_versioned_catalog_and_vector_tables(self) -> None:
        self.assertEqual(set(metadata.tables), EXPECTED_TABLES)
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
        self.assertEqual(version, "2")
        self.assertTrue(extension)
        self.assertIn("content", {column["name"] for column in inspector.get_columns("chunk")})

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


if __name__ == "__main__":
    unittest.main()
