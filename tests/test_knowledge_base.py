from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aikb.catalog import Catalog
from aikb.ingestion import ingest_source


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "linux"
        self.source.mkdir()
        (self.source / "Makefile").write_text(
            "VERSION = 6\n"
            "PATCHLEVEL = 18\n"
            "SUBLEVEL = 40\n"
            "EXTRAVERSION =\n",
            encoding="utf-8",
        )
        (self.source / "Kconfig").write_text("mainmenu \"test\"\n", encoding="utf-8")
        for directory in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
            (self.source / directory).mkdir()
        (self.source / "kernel" / "demo.c").write_text(
            '#include "../include/demo.h"\n'
            "#ifdef CONFIG_DEMO\n"
            "static int helper(void) { return 1; }\n"
            "static int demo_init(void)\n"
            "{\n"
            "    return helper() + init_idle();\n"
            "}\n"
            "#endif\n",
            encoding="utf-8",
        )
        (self.source / "include" / "demo.h").write_text(
            "int init_idle(void);\n", encoding="utf-8"
        )
        self.scope = {
            "scope_id": "test-linux",
            "source": {
                "project": "linux-test",
                "version": "6.18.40",
                "kind": "release_archive",
                "archive_name": "linux-test.tar.xz",
                "archive_sha256": "a" * 64,
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ingest_is_idempotent_and_search_returns_citation(self) -> None:
        database = self.root / "catalog.db"
        with Catalog(database) as catalog:
            catalog.initialize()
            first = ingest_source(
                catalog,
                self.scope,
                self.source,
                chunk_lines=3,
                chunk_overlap=1,
            )
            second = ingest_source(
                catalog,
                self.scope,
                self.source,
                chunk_lines=3,
                chunk_overlap=1,
            )
            hits = catalog.search("demo_init", top_k=5)
            summary = catalog.summary()
            symbol_report = catalog.find_symbol("helper")
            relation_stage = catalog.connection.execute(
                """
                SELECT name FROM sqlite_temp_master
                WHERE type = 'table' AND name = 'pending_relation_stage'
                """
            ).fetchone()

        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["file_count"], 2)
        self.assertIsNone(relation_stage)
        self.assertEqual(summary["snapshot_count"], 1)
        self.assertEqual(len(summary["active_snapshots"]), 1)
        active = summary["active_snapshots"][0]
        self.assertGreaterEqual(active["symbol_occurrence_count"], 3)
        self.assertGreaterEqual(active["relation_count"], 3)
        self.assertGreaterEqual(active["condition_count"], 1)
        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].path, "kernel/demo.c")
        self.assertEqual(hits[0].kind, "function")
        self.assertEqual(hits[0].symbol, "demo_init")
        self.assertEqual(hits[0].generator, "tree-sitter-c-v3")
        self.assertIn("linux-test@release:6.18.40:kernel/demo.c", hits[0].as_dict()["citation"])
        self.assertEqual(symbol_report["occurrence_count"], 1)
        helper_calls = [
            item
            for item in symbol_report["relations"]
            if item["kind"] == "calls" and item["target_text"] == "helper"
        ]
        self.assertEqual(len(helper_calls), 1)
        self.assertEqual(helper_calls[0]["confidence"], "source_inferred")
        self.assertIn("CONFIG_DEMO", helper_calls[0]["source_condition"])

    def test_changed_manifest_creates_new_snapshot_and_supersedes_old(self) -> None:
        database = self.root / "catalog.db"
        with Catalog(database) as catalog:
            catalog.initialize()
            original_source = (self.source / "kernel" / "demo.c").read_text(
                encoding="utf-8"
            )
            first = ingest_source(catalog, self.scope, self.source)
            (self.source / "kernel" / "demo.c").write_text(
                "static int demo_init(void) { return 42; }\n",
                encoding="utf-8",
            )
            second = ingest_source(catalog, self.scope, self.source)
            (self.source / "kernel" / "demo.c").write_text(
                original_source,
                encoding="utf-8",
            )
            third = ingest_source(catalog, self.scope, self.source)
            states = catalog.connection.execute(
                "SELECT id, state FROM snapshot ORDER BY created_at, id"
            ).fetchall()

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["analysis_cache_hit_count"], 0)
        self.assertEqual(first["analysis_cache_miss_count"], 2)
        self.assertEqual(second["analysis_cache_hit_count"], 1)
        self.assertEqual(second["analysis_cache_miss_count"], 1)
        self.assertEqual(third["id"], first["id"])
        self.assertTrue(third["idempotent"])
        self.assertTrue(third["reactivated"])
        self.assertEqual({row["state"] for row in states}, {"active", "superseded"})
        self.assertEqual(sum(row["state"] == "active" for row in states), 1)
        active_id = next(row["id"] for row in states if row["state"] == "active")
        self.assertEqual(active_id, first["id"])

    def test_ingest_rejects_build_dependent_scope(self) -> None:
        database = self.root / "catalog.db"
        scope = dict(self.scope)
        scope["index_policy"] = {
            "mode": "compiled",
            "execute_build": True,
            "requires_build_artifacts": True,
        }

        with Catalog(database) as catalog:
            catalog.initialize()
            with self.assertRaisesRegex(ValueError, "source-only indexing"):
                ingest_source(catalog, scope, self.source)

    def test_dependency_expansion_adds_direct_include_without_compiling(self) -> None:
        database = self.root / "catalog.db"
        scope = dict(self.scope)
        scope["include_roots"] = ["kernel"]
        scope["dependency_expansion"] = {
            "depth": 1,
            "max_files": 10,
            "max_candidates_per_reference": 4,
        }

        with Catalog(database) as catalog:
            catalog.initialize()
            result = ingest_source(catalog, scope, self.source)
            include_relation = catalog.connection.execute(
                """
                SELECT target.path AS target_path
                FROM relation AS relation
                LEFT JOIN source_file AS target ON target.id = relation.target_file_id
                WHERE relation.snapshot_id = ? AND relation.kind = 'includes'
                """,
                (result["id"],),
            ).fetchone()

        self.assertEqual(result["seed_file_count"], 1)
        self.assertEqual(result["dependency_file_count"], 1)
        self.assertEqual(result["file_count"], 2)
        self.assertFalse(result["dependency_expansion_truncated"])
        self.assertEqual(include_relation["target_path"], "include/demo.h")

    def test_dependency_budget_change_reuses_source_analysis_cache(self) -> None:
        database = self.root / "catalog.db"
        scope = dict(self.scope)
        scope["include_roots"] = ["kernel"]
        scope["dependency_expansion"] = {
            "depth": 1,
            "max_files": 10,
            "max_candidates_per_reference": 4,
        }

        with Catalog(database) as catalog:
            catalog.initialize()
            first = ingest_source(catalog, scope, self.source)
            second = ingest_source(
                catalog,
                scope,
                self.source,
                dependency_max_files=9,
            )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["analysis_cache_miss_count"], 2)
        self.assertEqual(second["analysis_cache_hit_count"], 2)
        self.assertEqual(second["analysis_cache_miss_count"], 0)


if __name__ == "__main__":
    unittest.main()
