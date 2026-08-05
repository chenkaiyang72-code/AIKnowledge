from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aikb.catalog import Catalog
from aikb.ingestion import ingest_source
from aikb.retrieval import extract_identifier_terms, retrieve_hybrid


class HybridRetrievalTests(unittest.TestCase):
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
        (self.source / "kernel" / "demo.c").write_text(
            "static int exact_target(void) { return 7; }\n"
            "int first_caller(void) { return exact_target(); }\n"
            "int second_caller(void) { return exact_target(); }\n",
            encoding="utf-8",
        )
        (self.source / "include" / "demo.h").write_text(
            "#define TWO_LINE(value) \\\n"
            "\t((value) + 1)\n",
            encoding="utf-8",
        )
        self.scope = {
            "scope_id": "retrieval-test",
            "source": {
                "project": "linux-retrieval-test",
                "version": "6.18.40",
                "kind": "release_archive",
                "archive_name": "linux-retrieval-test.tar.xz",
                "archive_sha256": "c" * 64,
                "git_commit": None,
            },
            "include_roots": ["include", "kernel"],
            "exclude_globs": [],
            "index_policy": {
                "mode": "source_only",
                "execute_build": False,
                "requires_build_artifacts": False,
            },
        }
        self.database = self.root / "catalog.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_exact_symbol_definition_is_promoted_and_traceable(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            first = retrieve_hybrid(catalog, "exact_target", top_k=10)
            second = retrieve_hybrid(catalog, " exact_target ", top_k=10)

        self.assertGreaterEqual(len(first.hits), 3)
        self.assertEqual(first.hits[0].hit.symbol, "exact_target")
        self.assertIn(
            "symbol_exact",
            {item.channel for item in first.hits[0].contributions},
        )
        self.assertTrue(
            any(
                item.hit.symbol in {"first_caller", "second_caller"}
                and "relation_source"
                in {contribution.channel for contribution in item.contributions}
                for item in first.hits
            )
        )
        self.assertEqual(
            [
                (item.hit.chunk_id, item.fused_score, item.contributions)
                for item in first.hits
            ],
            [
                (item.hit.chunk_id, item.fused_score, item.contributions)
                for item in second.hits
            ],
        )

    def test_identifier_extraction_is_bounded_and_deduplicated(self) -> None:
        query = " ".join(["same", "same", *[f"symbol_{index}" for index in range(30)]])
        terms = extract_identifier_terms(query)

        self.assertEqual(terms[0], "same")
        self.assertEqual(len(terms), 16)
        self.assertEqual(len(set(terms)), len(terms))

    def test_multiline_macro_occurrence_maps_to_overlapping_chunk(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            hits = catalog.search_symbol_chunks(["TWO_LINE"], top_k=10)

        self.assertGreaterEqual(len(hits), 1)
        self.assertEqual(hits[0].path, "include/demo.h")
        self.assertLessEqual(hits[0].start_line, 1)
        self.assertGreaterEqual(hits[0].end_line, 1)


if __name__ == "__main__":
    unittest.main()
