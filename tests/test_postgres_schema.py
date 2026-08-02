from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateTable

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
        self.assertEqual(version, "1")
        self.assertTrue(extension)

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


if __name__ == "__main__":
    unittest.main()
