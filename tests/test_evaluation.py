import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aikb.catalog import Catalog
from aikb.evaluation import (
    evaluate_results,
    evaluate_structured_results,
    load_questions,
    rank_question,
    render_structured_markdown,
    run_structured_evaluation,
)
from aikb.ingestion import ingest_source


class EvaluationTests(unittest.TestCase):
    def test_load_questions_rejects_missing_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "questions.jsonl"
            path.write_text('{"id":"q1"}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing fields"):
                load_questions(path)

    def test_rank_and_metrics_find_expected_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "kernel").mkdir()
            (source / "docs").mkdir()
            (source / "kernel" / "fork.c").write_text(
                "int copy_process(void) { return 0; }\ncopy_process();\n",
                encoding="utf-8",
            )
            (source / "docs" / "note.txt").write_text(
                "copy_process is documented here\n", encoding="utf-8"
            )
            question = {
                "id": "q1",
                "question": "copy_process 在哪里定义？",
                "category": "symbol_definition",
                "synthetic": True,
                "query_terms": ["copy_process"],
                "required_evidence": [{"path": "kernel/fork.c"}],
            }
            result = rank_question(
                source, question, ["kernel", "docs"], [], top_k=10
            )
            self.assertEqual("kernel/fork.c", result["results"][0]["path"])
            metrics = evaluate_results([question], [result], top_k=10)
            self.assertEqual(1.0, metrics["evidence_recall_at_10"])
            self.assertEqual(1.0, metrics["mrr"])

    def test_structured_metrics_require_line_range_overlap(self) -> None:
        question = {
            "id": "q1",
            "required_evidence": [
                {"path": "kernel/demo.c", "start_line": 10, "end_line": 12},
                {"path": "include/demo.h", "start_line": 3, "end_line": 3},
            ],
        }
        results = [
            {
                "id": "q1",
                "results": [
                    {
                        "path": "kernel/demo.c",
                        "start_line": 1,
                        "end_line": 4,
                        "citation": "repo@rev:kernel/demo.c:1-4",
                    },
                    {
                        "path": "include/demo.h",
                        "start_line": 1,
                        "end_line": 5,
                        "citation": "repo@rev:include/demo.h:1-5",
                    },
                ],
            }
        ]

        metrics = evaluate_structured_results([question], results, top_k=10)

        self.assertEqual(1.0, metrics["file_recall_at_10"])
        self.assertEqual(0.5, metrics["evidence_range_recall_at_10"])
        self.assertEqual("partial", metrics["per_question"][0]["status"])
        self.assertIsNone(
            metrics["per_question"][0]["range_matches"][0]["rank"]
        )
        self.assertEqual(
            2, metrics["per_question"][0]["range_matches"][1]["rank"]
        )

    def test_structured_evaluation_compares_lexical_and_rrf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "linux"
            source.mkdir()
            (source / "Makefile").write_text(
                "VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 40\nEXTRAVERSION =\n",
                encoding="utf-8",
            )
            (source / "Kconfig").write_text('mainmenu "test"\n', encoding="utf-8")
            for name in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
                (source / name).mkdir()
            (source / "kernel" / "demo.c").write_text(
                "static int exact_marker(void) { return 1; }\n"
                "int call_marker(void) { return exact_marker(); }\n",
                encoding="utf-8",
            )
            scope = {
                "scope_id": "evaluation-test",
                "source": {
                    "project": "linux-evaluation-test",
                    "version": "6.18.40",
                    "kind": "release_archive",
                    "archive_name": "linux-evaluation-test.tar.xz",
                    "archive_sha256": "f" * 64,
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
            questions = [
                {
                    "id": "q1",
                    "question": "exact_marker 在哪里定义？",
                    "category": "symbol",
                    "synthetic": True,
                    "query_terms": ["exact_marker"],
                    "required_evidence": [
                        {
                            "path": "kernel/demo.c",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                }
            ]
            with Catalog(root / "catalog.db") as catalog:
                catalog.initialize()
                snapshot = ingest_source(catalog, scope, source)
                report = run_structured_evaluation(
                    catalog,
                    questions,
                    top_k=10,
                    snapshot_id=snapshot["id"],
                )

        self.assertEqual(2, report["schema_version"])
        self.assertEqual(
            "lexical_fts5", report["retrievers"]["lexical"]["name"]
        )
        self.assertEqual(
            1.0,
            report["retrievers"]["hybrid_rrf"]["metrics"][
                "evidence_range_recall_at_10"
            ],
        )
        contribution_channels = {
            item["channel"]
            for item in report["retrievers"]["hybrid_rrf"]["questions"][0][
                "results"
            ][0]["contributions"]
        }
        self.assertIn("symbol_exact", contribution_channels)
        markdown = render_structured_markdown(report)
        self.assertIn("# 结构化检索自动评测报告", markdown)
        self.assertIn("Evidence Range Recall", markdown)
        self.assertIn("`q1` | complete | complete | 1/1 | 1/1", markdown)


if __name__ == "__main__":
    unittest.main()
