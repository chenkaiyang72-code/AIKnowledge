from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from aikb.catalog import SearchHit
from aikb.evaluation import evaluate_structured_results, load_questions, sha256_file
from aikb.semantic import (
    DEFAULT_QUERY_INSTRUCTION,
    EmbeddingCache,
    EmbeddingProvider,
    SEMANTIC_RRF_K,
    SEMANTIC_RRF_WEIGHT,
    SemanticRerankHit,
    SentenceTransformerEmbeddingProvider,
    rerank_candidates,
)


DEFAULT_MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
DEFAULT_MODEL_WEIGHTS_SHA256 = (
    "0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd"
)


def _deserialize_hit(item: dict[str, Any], question_id: str) -> SearchHit:
    expected_citation = (
        f"{item['repository']}@{item['revision']}:"
        f"{item['path']}:{item['start_line']}-{item['end_line']}"
    )
    if item.get("citation") != expected_citation:
        raise ValueError(
            f"candidate citation is invalid for question {question_id}"
        )
    if not item["content_truncated"] and hashlib.sha256(
        item["content"].encode("utf-8")
    ).hexdigest() != item["content_hash"]:
        raise ValueError(
            f"candidate content hash is invalid for question {question_id}"
        )
    return SearchHit(
        chunk_id=item["chunk_id"],
        blob_id=item["blob_id"],
        content_hash=item["content_hash"],
        repository=item["repository"],
        snapshot_id=item["snapshot_id"],
        revision=item["revision"],
        path=item["path"],
        start_line=item["start_line"],
        end_line=item["end_line"],
        kind=item["kind"],
        symbol=item.get("symbol"),
        generator=item["generator"],
        rank=item["rank"],
        content=item["content"],
        content_truncated=item["content_truncated"],
    )


def load_candidate_report(
    path: Path,
    questions: list[dict[str, Any]],
    candidate_k: int,
) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != 2:
        raise ValueError("candidate input must be a structured schema_version 2 report")
    if report.get("top_k", 0) < candidate_k:
        raise ValueError("candidate input top_k is smaller than --candidate-k")
    source = report.get("retrievers", {}).get("hybrid_rrf", {})
    if not isinstance(source.get("questions"), list):
        raise ValueError("candidate input has no hybrid_rrf question results")
    cached_questions = {item["id"]: item for item in source["questions"]}
    if set(cached_questions) != {question["id"] for question in questions}:
        raise ValueError("candidate input question IDs do not match the dataset")
    allowed_snapshots = {
        item["snapshot_id"]
        for item in report.get("scope", {}).get("resolved_snapshots", [])
    }
    if not allowed_snapshots:
        raise ValueError("candidate input has no resolved snapshot")
    for question in questions:
        cached = cached_questions[question["id"]]
        expected_query = " ".join(question["query_terms"])
        if cached.get("query") != expected_query:
            raise ValueError(
                f"candidate query does not match question {question['id']}"
            )
        for item in cached.get("results", [])[:candidate_k]:
            if item.get("snapshot_id") not in allowed_snapshots:
                raise ValueError(
                    f"candidate escapes snapshot scope for question {question['id']}"
                )
            _deserialize_hit(item, question["id"])
    return report, hashlib.sha256(raw).hexdigest()


def _serialize_plain(hit: SearchHit) -> dict[str, Any]:
    return hit.as_dict()


def _serialize_semantic(item: SemanticRerankHit) -> dict[str, Any]:
    return {
        **item.hit.as_dict(),
        "original_hybrid_rank": item.original_rank,
        "semantic_rank": item.semantic_rank,
        "semantic_score": item.semantic_score,
        "hybrid_semantic_rrf_score": item.fused_score,
    }


def run_semantic_ablation(
    candidate_report: dict[str, Any],
    candidate_report_digest: str,
    questions: list[dict[str, Any]],
    provider: EmbeddingProvider,
    cache: EmbeddingCache,
    *,
    top_k: int,
    candidate_k: int,
) -> dict[str, Any]:
    if top_k < 1 or top_k > candidate_k:
        raise ValueError("top-k must be positive and no larger than candidate-k")
    if candidate_k > 100:
        raise ValueError("candidate-k must be no larger than 100")
    source = candidate_report["retrievers"]["hybrid_rrf"]
    source_by_id = {item["id"]: item for item in source["questions"]}
    candidate_questions: list[dict[str, Any]] = []
    semantic_questions: list[dict[str, Any]] = []
    fused_questions: list[dict[str, Any]] = []
    cache_counts = {
        "query_hits": 0,
        "query_misses": 0,
        "document_hits": 0,
        "document_misses": 0,
    }

    for question in questions:
        source_question = source_by_id[question["id"]]
        candidates = [
            _deserialize_hit(item, question["id"])
            for item in source_question["results"][:candidate_k]
        ]
        result = rerank_candidates(
            question["question"],
            candidates,
            provider,
            cache,
        )
        cache_counts["query_hits"] += result.query_cache_hits
        cache_counts["query_misses"] += result.query_cache_misses
        cache_counts["document_hits"] += result.document_cache_hits
        cache_counts["document_misses"] += result.document_cache_misses
        common = {
            "id": question["id"],
            "question": question["question"],
            "query": question["question"],
            "candidate_count": len(candidates),
        }
        candidate_questions.append(
            {
                **common,
                "results": [_serialize_plain(hit) for hit in candidates],
            }
        )
        semantic_questions.append(
            {
                **common,
                "results": [
                    _serialize_semantic(item) for item in result.semantic_hits
                ],
            }
        )
        fused_questions.append(
            {
                **common,
                "results": [_serialize_semantic(item) for item in result.fused_hits],
            }
        )

    candidate_metrics = evaluate_structured_results(
        questions, candidate_questions, top_k
    )
    candidate_ceiling = evaluate_structured_results(
        questions, candidate_questions, candidate_k
    )
    semantic_metrics = evaluate_structured_results(
        questions, semantic_questions, top_k
    )
    fused_metrics = evaluate_structured_results(questions, fused_questions, top_k)
    return {
        "schema_version": 1,
        "kind": "semantic_candidate_ablation",
        "dataset": {
            "questions": len(questions),
            "required_evidence_ranges": sum(
                len(question.get("required_evidence", []))
                for question in questions
            ),
        },
        "scope": candidate_report["scope"],
        "top_k": top_k,
        "candidate_k": candidate_k,
        "candidate_report_digest": candidate_report_digest,
        "candidate_retriever": source["name"],
        "semantic_query_source": "question",
        "model": provider.spec.as_dict(),
        "fusion": {
            "method": "rrf",
            "k": SEMANTIC_RRF_K,
            "candidate_weight": 1.0,
            "semantic_weight": SEMANTIC_RRF_WEIGHT,
        },
        "cache": cache_counts,
        "retrievers": {
            "candidate_hybrid": {
                "name": f"{source['name']}@candidate-{candidate_k}",
                "metrics": candidate_metrics,
                "candidate_ceiling_metrics": candidate_ceiling,
                "questions": candidate_questions,
            },
            "semantic_rerank": {
                "name": f"{provider.spec.model_name}:cosine",
                "metrics": semantic_metrics,
                "questions": semantic_questions,
            },
            "hybrid_semantic_rrf": {
                "name": (
                    f"candidate_hybrid+semantic_rrf_k{SEMANTIC_RRF_K}"
                    f"_w{SEMANTIC_RRF_WEIGHT:g}"
                ),
                "metrics": fused_metrics,
                "questions": fused_questions,
            },
        },
    }


def _summary(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in metrics.items() if key != "per_question"}


def render_semantic_markdown(report: dict[str, Any]) -> str:
    top_k = report["top_k"]
    candidate_k = report["candidate_k"]
    retrievers = report["retrievers"]

    def value(metrics: dict[str, Any], key: str) -> str:
        metric = metrics[key]
        return "n/a" if metric is None else f"{metric:.4f}"

    lines = [
        "# 语义候选重排消融报告",
        "",
        f"- 问题数：{report['dataset']['questions']}",
        f"- Top K：{top_k}",
        f"- Candidate K：{candidate_k}",
        f"- 候选报告：`{report['candidate_report_digest']}`",
        f"- 模型：`{report['model']['model_name']}`",
        f"- 模型 revision：`{report['model']['model_revision']}`",
        f"- 权重 SHA-256：`{report['model']['weights_sha256']}`",
        f"- 向量维度：{report['model']['dimension']}",
        f"- 最大序列长度：{report['model']['max_sequence_length']}",
        f"- 模型指纹：`{report['model']['fingerprint']}`",
        (
            f"- 融合：RRF k={report['fusion']['k']}，candidate weight="
            f"{report['fusion']['candidate_weight']:g}，semantic weight="
            f"{report['fusion']['semantic_weight']:g}"
        ),
        "- 查询输入：自然语言问题；instruction 固定为代码证据检索任务。",
        "- 证据状态：沿用 draft 标注，只用于工程消融，不视为冻结黄金集。",
        "",
        "## Top-K 指标",
        "",
        "| 检索器 | File Recall | Range Recall | File MRR | Range MRR | 完整/部分/未命中 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key in ("candidate_hybrid", "semantic_rerank", "hybrid_semantic_rrf"):
        item = retrievers[key]
        metrics = item["metrics"]
        lines.append(
            f"| `{key}` | "
            f"{value(metrics, f'file_recall_at_{top_k}')} | "
            f"{value(metrics, f'evidence_range_recall_at_{top_k}')} | "
            f"{value(metrics, 'file_mrr')} | "
            f"{value(metrics, 'evidence_range_mrr')} | "
            f"{metrics['questions_complete']}/"
            f"{metrics['questions_partial']}/"
            f"{metrics['questions_missed']} |"
        )
    ceiling = retrievers["candidate_hybrid"]["candidate_ceiling_metrics"]
    candidate_metrics = retrievers["candidate_hybrid"]["metrics"]
    fused_metrics = retrievers["hybrid_semantic_rrf"]["metrics"]
    lines.extend(
        [
            "",
            "## 融合增量",
            "",
            (
                "- File Recall Δ："
                f"{fused_metrics[f'file_recall_at_{top_k}'] - candidate_metrics[f'file_recall_at_{top_k}']:+.4f}"
            ),
            (
                "- Range Recall Δ："
                f"{fused_metrics[f'evidence_range_recall_at_{top_k}'] - candidate_metrics[f'evidence_range_recall_at_{top_k}']:+.4f}"
            ),
            f"- File MRR Δ：{fused_metrics['file_mrr'] - candidate_metrics['file_mrr']:+.4f}",
            (
                "- Range MRR Δ："
                f"{fused_metrics['evidence_range_mrr'] - candidate_metrics['evidence_range_mrr']:+.4f}"
            ),
            "",
            "## 候选池上限",
            "",
            (
                f"Candidate@{candidate_k} 已包含 "
                f"{ceiling['files_found']}/{ceiling['files_total']} 个证据文件、"
                f"{ceiling['evidence_ranges_found']}/{ceiling['evidence_ranges_total']} "
                "个证据范围。重排只能移动已进入候选池的证据，不能找回池外证据。"
            ),
            "",
            "## 逐题结果",
            "",
            "| 问题 | Candidate | Semantic | Fused | Candidate 范围 | Fused 范围 |",
            "| --- | --- | --- | --- | ---: | ---: |",
        ]
    )
    metrics_by_retriever = {
        key: {
            item["id"]: item
            for item in retrievers[key]["metrics"]["per_question"]
        }
        for key in ("candidate_hybrid", "semantic_rerank", "hybrid_semantic_rrf")
    }
    for question_id in metrics_by_retriever["candidate_hybrid"]:
        candidate = metrics_by_retriever["candidate_hybrid"][question_id]
        semantic = metrics_by_retriever["semantic_rerank"][question_id]
        fused = metrics_by_retriever["hybrid_semantic_rrf"][question_id]
        candidate_found = sum(
            item["rank"] is not None for item in candidate["range_matches"]
        )
        fused_found = sum(
            item["rank"] is not None for item in fused["range_matches"]
        )
        total = len(candidate["range_matches"])
        lines.append(
            f"| `{question_id}` | {candidate['status']} | {semantic['status']} | "
            f"{fused['status']} | {candidate_found}/{total} | {fused_found}/{total} |"
        )
    lines.extend(
        [
            "",
            "## 缓存",
            "",
            f"- Query hit/miss：{report['cache']['query_hits']}/{report['cache']['query_misses']}",
            f"- Document hit/miss：{report['cache']['document_hits']}/{report['cache']['document_misses']}",
            "",
            "## 判定",
            "",
            "只有相对同一深候选基线的 Recall/MRR 有可解释净增益，并且本地延迟、显存和索引成本可接受，语义通道才进入正式检索。",
            "",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="rerank a validated structured candidate report"
    )
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown-output", type=Path)
    parser.add_argument("--cache-db", type=Path, default=Path(".aikb/embedding-cache.db"))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--candidate-k", type=int, default=100)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--model-revision", default=DEFAULT_MODEL_REVISION)
    parser.add_argument("--model-path", type=Path)
    parser.add_argument(
        "--model-weights-sha256",
        default=DEFAULT_MODEL_WEIGHTS_SHA256,
        help="verify model.safetensors when --model-path is used",
    )
    parser.add_argument("--dimension", type=int, default=512)
    parser.add_argument("--device")
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--max-seq-length", type=int, default=2_048)
    parser.add_argument("--query-instruction", default=DEFAULT_QUERY_INSTRUCTION)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.top_k < 1 or args.candidate_k < args.top_k or args.candidate_k > 100:
            raise ValueError("require 1 <= top-k <= candidate-k <= 100")
        questions = load_questions(args.questions)
        candidate_report, digest = load_candidate_report(
            args.input, questions, args.candidate_k
        )
        provider = SentenceTransformerEmbeddingProvider(
            args.model_name,
            args.model_revision,
            args.dimension,
            model_path=args.model_path,
            device=args.device,
            batch_size=args.batch_size,
            max_seq_length=args.max_seq_length,
            weights_sha256=args.model_weights_sha256 or None,
            query_instruction=args.query_instruction,
        )
        verified_weights_sha256 = None
        if args.model_path:
            weights_path = args.model_path / "model.safetensors"
            if not weights_path.is_file():
                raise ValueError("--model-path has no model.safetensors")
            verified_weights_sha256 = sha256_file(weights_path)
            if (
                args.model_weights_sha256
                and verified_weights_sha256.lower()
                != args.model_weights_sha256.lower()
            ):
                raise ValueError("local model weights SHA-256 does not match")
        with EmbeddingCache(args.cache_db) as cache:
            report = run_semantic_ablation(
                candidate_report,
                digest,
                questions,
                provider,
                cache,
                top_k=args.top_k,
                candidate_k=args.candidate_k,
            )
        report["model"]["verified_local_weights_sha256"] = verified_weights_sha256
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        if args.markdown_output:
            args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
            args.markdown_output.write_text(
                render_semantic_markdown(report),
                encoding="utf-8",
                newline="\n",
            )
        print(
            json.dumps(
                {
                    key: _summary(value["metrics"])
                    for key, value in report["retrievers"].items()
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
