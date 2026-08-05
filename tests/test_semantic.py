from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from aikb.catalog import SearchHit
from aikb.semantic import (
    EmbeddingCache,
    EmbeddingModelSpec,
    rerank_candidates,
)
from aikb.semantic_evaluation import (
    load_candidate_report,
    run_semantic_ablation,
)


class FakeEmbeddingProvider:
    def __init__(self) -> None:
        self._spec = EmbeddingModelSpec(
            provider="fake",
            model_name="semantic-fixture",
            model_revision="revision-1",
            dimension=2,
        )
        self.query_calls = 0
        self.document_calls = 0

    @property
    def spec(self) -> EmbeddingModelSpec:
        return self._spec

    def embed_queries(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.query_calls += 1
        return [(1.0, 0.0) for _ in texts]

    def embed_documents(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        self.document_calls += 1
        return [
            (1.0, 0.0) if "RELEVANT" in text else (0.0, 1.0)
            for text in texts
        ]


def make_hit(
    chunk_id: str,
    path: str,
    content: str,
    start_line: int = 1,
    end_line: int = 1,
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        blob_id=f"blob-{chunk_id}",
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        repository="linux",
        snapshot_id="snapshot-1",
        revision="release:fixture",
        path=path,
        start_line=start_line,
        end_line=end_line,
        kind="function",
        symbol=None,
        generator="fixture",
        rank=1.0,
        content=content,
        content_truncated=False,
    )


class SemanticTests(unittest.TestCase):
    def test_semantic_rerank_is_cached_by_model_and_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeEmbeddingProvider()
            candidates = [
                make_hit("one", "kernel/one.c", "irrelevant"),
                make_hit("two", "kernel/two.c", "RELEVANT evidence"),
            ]
            with EmbeddingCache(Path(directory) / "embeddings.db") as cache:
                first = rerank_candidates("find evidence", candidates, provider, cache)
                second = rerank_candidates("find evidence", candidates, provider, cache)

            self.assertEqual(first.semantic_hits[0].hit.chunk_id, "two")
            self.assertEqual(first.query_cache_misses, 1)
            self.assertEqual(first.document_cache_misses, 2)
            self.assertEqual(second.query_cache_hits, 1)
            self.assertEqual(second.document_cache_hits, 2)
            self.assertEqual(provider.query_calls, 1)
            self.assertEqual(provider.document_calls, 1)

    def test_candidate_report_validation_and_ablation_metrics(self) -> None:
        question = {
            "id": "q1",
            "question": "Which source is relevant?",
            "category": "fixture",
            "synthetic": False,
            "query_terms": ["source", "relevant"],
            "required_evidence": [
                {"path": "kernel/two.c", "start_line": 1, "end_line": 1}
            ],
        }
        first = make_hit("one", "kernel/one.c", "irrelevant")
        second = make_hit("two", "kernel/two.c", "RELEVANT evidence")
        report = {
            "schema_version": 2,
            "scope": {
                "resolved_snapshots": [
                    {
                        "repository": "linux",
                        "snapshot_id": "snapshot-1",
                        "revision": "release:fixture",
                    }
                ]
            },
            "top_k": 2,
            "retrievers": {
                "hybrid_rrf": {
                    "name": "fixture-hybrid",
                    "questions": [
                        {
                            "id": "q1",
                            "query": "source relevant",
                            "results": [first.as_dict(), second.as_dict()],
                        }
                    ],
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report_path = root / "candidate.json"
            report_path.write_text(json.dumps(report), encoding="utf-8")
            loaded, digest = load_candidate_report(report_path, [question], 2)
            with EmbeddingCache(root / "embedding.db") as cache:
                ablation = run_semantic_ablation(
                    loaded,
                    digest,
                    [question],
                    FakeEmbeddingProvider(),
                    cache,
                    top_k=1,
                    candidate_k=2,
                )

            baseline = ablation["retrievers"]["candidate_hybrid"]["metrics"]
            semantic = ablation["retrievers"]["semantic_rerank"]["metrics"]
            ceiling = ablation["retrievers"]["candidate_hybrid"][
                "candidate_ceiling_metrics"
            ]
            self.assertEqual(baseline["evidence_range_recall_at_1"], 0.0)
            self.assertEqual(semantic["evidence_range_recall_at_1"], 1.0)
            self.assertEqual(ceiling["evidence_range_recall_at_2"], 1.0)

            tampered = json.loads(json.dumps(report))
            tampered["retrievers"]["hybrid_rrf"]["questions"][0]["results"][0][
                "citation"
            ] = "invalid"
            report_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "citation is invalid"):
                load_candidate_report(report_path, [question], 2)


if __name__ == "__main__":
    unittest.main()
