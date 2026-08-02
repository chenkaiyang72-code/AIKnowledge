import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aikb.evaluation import evaluate_results, load_questions, rank_question


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


if __name__ == "__main__":
    unittest.main()
