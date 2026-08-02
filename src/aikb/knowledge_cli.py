from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aikb.catalog import Catalog
from aikb.ingestion import (
    DEFAULT_CHUNK_LINES,
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_MAX_FILE_BYTES,
    ingest_source,
    load_scope,
)


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

    symbol_parser = subparsers.add_parser(
        "kb-symbol",
        help="show source-only symbol occurrences and relation candidates",
    )
    symbol_parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    symbol_parser.add_argument("--name", required=True)
    symbol_parser.add_argument("--top-k", type=int, default=50)
    symbol_parser.add_argument("--repository")
    symbol_parser.add_argument("--snapshot-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
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
            else:
                report = catalog.find_symbol(
                    name=args.name,
                    top_k=args.top_k,
                    repository=args.repository,
                    snapshot_id=args.snapshot_id,
                )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (KeyError, OSError, RuntimeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
