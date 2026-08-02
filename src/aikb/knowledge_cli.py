from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aikb.catalog import Catalog
from aikb.context_pack import build_context_pack, context_pack_json_schema
from aikb.ingestion import (
    DEFAULT_CHUNK_LINES,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_MAX_FILE_BYTES,
    ingest_source,
    load_scope,
)
from aikb.retrieval import retrieve_hybrid


DEFAULT_DATABASE = Path(".aikb/catalog.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AIKnowledge Phase 0B local knowledge-base CLI"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "kb-ingest", help="scan a source snapshot into the local catalog"
    )
    ingest_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    ingest_parser.add_argument("--scope", type=Path, required=True)
    ingest_parser.add_argument("--source", type=Path, required=True)
    ingest_parser.add_argument("--archive", type=Path)
    ingest_parser.add_argument(
        "--include",
        action="append",
        dest="include_roots",
        help="override scope roots; repeat for multiple paths",
    )
    ingest_parser.add_argument("--max-files", type=int)
    ingest_parser.add_argument(
        "--max-file-bytes", type=int, default=DEFAULT_MAX_FILE_BYTES
    )
    ingest_parser.add_argument("--chunk-lines", type=int, default=DEFAULT_CHUNK_LINES)
    ingest_parser.add_argument(
        "--chunk-overlap", type=int, default=DEFAULT_CHUNK_OVERLAP
    )
    ingest_parser.add_argument(
        "--dependency-depth",
        type=int,
        help="override scope dependency expansion depth (0 disables expansion)",
    )
    ingest_parser.add_argument(
        "--dependency-max-files",
        type=int,
        help="maximum dependency files added beyond the seed scope",
    )
    ingest_parser.add_argument(
        "--dependency-max-candidates",
        type=int,
        help="maximum ambiguous paths accepted for one dependency reference",
    )

    stats_parser = subparsers.add_parser(
        "kb-stats", help="show repositories and active snapshots"
    )
    stats_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)

    search_parser = subparsers.add_parser(
        "kb-search", help="search indexed chunks and return stable citations"
    )
    search_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--top-k", type=int, default=10)
    search_parser.add_argument("--repository")
    search_parser.add_argument("--snapshot-id")

    retrieve_parser = subparsers.add_parser(
        "kb-retrieve",
        help="run deterministic lexical/symbol/relation RRF retrieval",
    )
    retrieve_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    retrieve_parser.add_argument("--query", required=True)
    retrieve_parser.add_argument("--top-k", type=int, default=10)
    retrieve_parser.add_argument("--repository")
    retrieve_parser.add_argument("--snapshot-id")

    symbol_parser = subparsers.add_parser(
        "kb-symbol",
        help="show source-only symbol occurrences and relation candidates",
    )
    symbol_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    symbol_parser.add_argument("--name", required=True)
    symbol_parser.add_argument("--top-k", type=int, default=50)
    symbol_parser.add_argument("--repository")
    symbol_parser.add_argument("--snapshot-id")

    context_parser = subparsers.add_parser(
        "kb-context",
        help="build a versioned Context Pack with citations and retrieval trace",
    )
    context_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    context_parser.add_argument("--query", required=True)
    context_parser.add_argument("--repository")
    context_parser.add_argument("--snapshot-id")
    context_parser.add_argument("--max-evidence-items", type=int, default=8)
    context_parser.add_argument("--evidence-token-budget", type=int, default=3_000)
    context_parser.add_argument("--max-symbols", type=int, default=5)
    context_parser.add_argument("--max-relations-per-symbol", type=int, default=8)

    subparsers.add_parser(
        "kb-context-schema",
        help="print the generated JSON Schema for Context Pack v1",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "kb-context-schema":
            print(json.dumps(context_pack_json_schema(), ensure_ascii=False, indent=2))
            return 0
        with Catalog(args.db) as catalog:
            catalog.initialize()
            if args.command == "kb-ingest":
                scope = load_scope(args.scope)
                report = ingest_source(
                    catalog=catalog,
                    scope=scope,
                    source=args.source,
                    archive=args.archive,
                    include_roots=args.include_roots,
                    max_files=args.max_files,
                    max_file_bytes=args.max_file_bytes,
                    chunk_lines=args.chunk_lines,
                    chunk_overlap=args.chunk_overlap,
                    dependency_depth=args.dependency_depth,
                    dependency_max_files=args.dependency_max_files,
                    dependency_max_candidates=args.dependency_max_candidates,
                )
            elif args.command == "kb-stats":
                report = catalog.summary()
            elif args.command == "kb-search":
                hits = catalog.search(
                    query=args.query,
                    top_k=args.top_k,
                    repository=args.repository,
                    snapshot_id=args.snapshot_id,
                )
                report = {
                    "query": args.query,
                    "top_k": args.top_k,
                    "result_count": len(hits),
                    "results": [hit.as_dict() for hit in hits],
                }
            elif args.command == "kb-retrieve":
                result = retrieve_hybrid(
                    catalog=catalog,
                    query=args.query,
                    top_k=args.top_k,
                    repository=args.repository,
                    snapshot_id=args.snapshot_id,
                )
                report = {
                    "query": result.query,
                    "identifier_terms": list(result.identifier_terms),
                    "rrf_k": result.rrf_k,
                    "channel_candidate_counts": result.channel_candidate_counts,
                    "result_count": len(result.hits),
                    "results": [
                        {
                            **item.hit.as_dict(),
                            "fused_score": item.fused_score,
                            "contributions": [
                                contribution.as_dict()
                                for contribution in item.contributions
                            ],
                        }
                        for item in result.hits
                    ],
                }
            elif args.command == "kb-symbol":
                report = catalog.find_symbol(
                    name=args.name,
                    top_k=args.top_k,
                    repository=args.repository,
                    snapshot_id=args.snapshot_id,
                )
            else:
                report = build_context_pack(
                    catalog=catalog,
                    query=args.query,
                    repository=args.repository,
                    snapshot_id=args.snapshot_id,
                    max_evidence_items=args.max_evidence_items,
                    evidence_token_budget=args.evidence_token_budget,
                    max_symbols=args.max_symbols,
                    max_relations_per_symbol=args.max_relations_per_symbol,
                ).model_dump(mode="json")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
