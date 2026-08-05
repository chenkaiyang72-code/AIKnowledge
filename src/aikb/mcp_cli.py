from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the read-only AIKnowledge MCP server"
    )
    parser.add_argument("command", nargs="?", choices=("mcp-serve",))
    parser.add_argument("--db", type=Path, default=Path(".aikb/catalog.db"))
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--path", default="/mcp/read")
    parser.add_argument(
        "--allow-repository",
        action="append",
        dest="allowed_repositories",
        help="PoC visibility boundary; repeat for each visible repository",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        print("error: port must be between 1 and 65535", file=sys.stderr)
        return 2
    if not args.path.startswith("/"):
        print("error: path must start with /", file=sys.stderr)
        return 2
    if args.transport == "streamable-http" and args.host not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        print(
            "error: unauthenticated Phase 1A HTTP is restricted to loopback; "
            "remote binding requires the Phase 1B authentication boundary",
            file=sys.stderr,
        )
        return 2
    try:
        from aikb.mcp_server import MCPReadConfig, create_mcp_server
    except ImportError as error:
        print(
            'error: MCP support is not installed; run python -m pip install -e ".[mcp]"',
            file=sys.stderr,
        )
        return 2
    server = create_mcp_server(
        MCPReadConfig(
            database=args.db,
            allowed_repositories=(
                frozenset(args.allowed_repositories)
                if args.allowed_repositories is not None
                else None
            ),
        )
    )
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(
            transport="streamable-http",
            host=args.host,
            port=args.port,
            streamable_http_path=args.path,
            stateless_http=True,
            json_response=True,
            max_request_body_size=1_048_576,
        )
    return 0
