from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aikb.catalog import Catalog
from aikb.context_pack import ContextPack, build_context_pack, context_pack_json_schema
from aikb.ingestion import ingest_source


class ContextPackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "linux"
        self.source.mkdir()
        (self.source / "Makefile").write_text(
            "VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 40\nEXTRAVERSION =\n",
            encoding="utf-8",
        )
        (self.source / "Kconfig").write_text("mainmenu \"test\"\n", encoding="utf-8")
        for directory in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
            (self.source / directory).mkdir()
        budget_body = "\n".join("    value += 1;" for _ in range(100))
        (self.source / "kernel" / "demo.c").write_text(
            '#include "../include/demo.h"\n'
            "static int helper(void) { return 1; }\n"
            "int init_idle(void) { return helper(); }\n"
            "int budget_marker(void)\n"
            "{\n"
            "    int value = 0;\n"
            f"{budget_body}\n"
            "    return value;\n"
            "}\n",
            encoding="utf-8",
        )
        (self.source / "include" / "demo.h").write_text(
            "int init_idle(void);\n", encoding="utf-8"
        )
        self.scope = {
            "scope_id": "context-pack-test",
            "source": {
                "project": "linux-context-test",
                "version": "6.18.40",
                "kind": "release_archive",
                "archive_name": "linux-context-test.tar.xz",
                "archive_sha256": "b" * 64,
                "git_commit": None,
            },
            "include_roots": ["kernel"],
            "exclude_globs": [],
            "index_policy": {
                "mode": "source_only",
                "execute_build": False,
                "requires_build_artifacts": False,
            },
            "dependency_expansion": {
                "depth": 1,
                "max_files": 10,
                "max_candidates_per_reference": 4,
            },
        }
        self.database = self.root / "catalog.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_context_pack_is_deterministic_and_evidence_resolves(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            first = build_context_pack(catalog, "init_idle", max_evidence_items=4)
            second = build_context_pack(catalog, "  init_idle  ", max_evidence_items=4)
            validated = ContextPack.model_validate(first.model_dump(mode="json"))
            evidence_rows = []
            for item in first.evidence:
                evidence_rows.append(
                    catalog.connection.execute(
                        """
                        SELECT c.content_hash, f.blob_id, f.path,
                               c.start_line, c.end_line, c.snapshot_id
                        FROM chunk AS c
                        JOIN source_file AS f ON f.id = c.file_id
                        WHERE c.id = ?
                        """,
                        (item.chunk_id,),
                    ).fetchone()
                )

        self.assertEqual(first.retrieval_trace.id, second.retrieval_trace.id)
        self.assertEqual(first.id, second.id)
        self.assertEqual(
            first.model_dump(mode="json"),
            second.model_dump(mode="json"),
        )
        self.assertEqual(validated.schema_version, "1.2")
        self.assertGreaterEqual(len(first.evidence), 1)
        self.assertGreaterEqual(len(first.symbols), 1)
        self.assertEqual(first.symbols[0].name, "init_idle")
        self.assertEqual(first.coverage.evidence_status, "available_unassessed")
        for item, row in zip(first.evidence, evidence_rows, strict=True):
            self.assertIsNotNone(row)
            self.assertEqual(item.content_hash, row["content_hash"])
            self.assertEqual(item.blob_id, row["blob_id"])
            self.assertEqual(item.path, row["path"])
            self.assertEqual(item.lines, (row["start_line"], row["end_line"]))
            self.assertEqual(item.snapshot_id, row["snapshot_id"])

    def test_context_pack_enforces_evidence_budget(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            pack = build_context_pack(
                catalog,
                "budget_marker value",
                max_evidence_items=4,
                evidence_token_budget=64,
            )

        self.assertLessEqual(pack.budget.evidence_chars_used, 64 * 4)
        self.assertLessEqual(pack.budget.estimated_evidence_tokens, 64)
        self.assertTrue(pack.budget.truncated)
        self.assertTrue(any(item.content_truncated for item in pack.evidence))

    def test_context_pack_reports_missing_evidence_as_gap(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            pack = build_context_pack(catalog, "definitely_missing_symbol_xyz")

        self.assertEqual(pack.evidence, [])
        self.assertEqual(pack.coverage.evidence_status, "none")
        self.assertFalse(pack.coverage.complete)
        self.assertIn("no indexed evidence", pack.gaps[0])

    def test_context_pack_schema_is_versioned_and_strict(self) -> None:
        schema = context_pack_json_schema()

        self.assertEqual(schema["additionalProperties"], False)
        self.assertEqual(schema["$id"], "urn:aiknowledge:schema:context-pack:v1")
        self.assertIn("schema_uri", schema["required"])
        self.assertIn("schema_version", schema["required"])
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "1.2",
        )
        self.assertIn("CodeEvidence", schema["$defs"])


if __name__ == "__main__":
    unittest.main()
