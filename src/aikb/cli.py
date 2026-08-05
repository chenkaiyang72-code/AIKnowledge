from __future__ import annotations

import sys

from aikb import evaluation, knowledge_cli, mcp_cli, semantic_evaluation


EVALUATION_COMMANDS = {"inspect", "baseline", "structured"}
SEMANTIC_COMMANDS = {"semantic-ablation"}
KNOWLEDGE_COMMANDS = {
    "kb-ingest",
    "kb-stats",
    "kb-search",
    "kb-retrieve",
    "kb-symbol",
    "kb-context",
    "kb-context-schema",
    "kb-solution-publish",
    "kb-solution-publish-postgres",
    "kb-solution-show",
    "kb-solution-context",
    "kb-publish-postgres",
    "kb-zoekt-export",
}


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        print(
            "AIKnowledge CLI\n\n"
            "Evaluation commands:\n"
            "  inspect       verify a source snapshot\n"
            "  baseline      run the Phase 0A lexical baseline\n"
            "  structured    compare catalog lexical retrieval with hybrid RRF\n"
            "  semantic-ablation  rerank a validated deep candidate report\n\n"
            "Knowledge-base commands:\n"
            "  kb-ingest     scan source into an immutable local snapshot\n"
            "  kb-stats      show catalog and active snapshot statistics\n"
            "  kb-search     search chunks and return versioned citations\n"
            "  kb-retrieve   fuse lexical, symbol, and relation candidates with RRF\n"
            "  kb-symbol     show source-only occurrences and relation candidates\n\n"
            "  kb-context    build a budgeted Context Pack with retrieval trace\n"
            "  kb-context-schema  print Context Pack v1 JSON Schema\n\n"
            "  kb-solution-publish  activate a pinned multi-repository version set\n"
            "  kb-solution-publish-postgres  publish that version set to the team store\n"
            "  kb-solution-show     resolve visible members of a solution snapshot\n"
            "  kb-solution-context  build a cross-repository Context Pack\n\n"
            "  kb-publish-postgres  atomically publish a snapshot to PostgreSQL\n\n"
            "  kb-zoekt-export  materialize an immutable snapshot for Zoekt\n\n"
            "MCP command:\n"
            "  mcp-serve      run the read-only stdio or Streamable HTTP server\n\n"
            "Run 'python -m aikb <command> --help' for command options."
        )
        return 0
    command = arguments[0]
    if command in EVALUATION_COMMANDS:
        return evaluation.main(arguments)
    if command in SEMANTIC_COMMANDS:
        return semantic_evaluation.main(arguments[1:])
    if command in KNOWLEDGE_COMMANDS:
        return knowledge_cli.main(arguments)
    if command == "mcp-serve":
        return mcp_cli.main(arguments)
    print(f"error: unknown command {command!r}; use --help", file=sys.stderr)
    return 2
