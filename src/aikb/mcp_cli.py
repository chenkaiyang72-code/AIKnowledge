from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the read-only AIKnowledge MCP server"
    )
    parser.add_argument("command", nargs="?", choices=("mcp-serve",))
    parser.add_argument("--db", type=Path)
    parser.add_argument("--postgres-url")
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
    parser.add_argument("--oidc-issuer")
    parser.add_argument("--oidc-jwks-url")
    parser.add_argument(
        "--resource-server-url",
        help="Canonical public MCP URL; also required as the JWT audience",
    )
    parser.add_argument(
        "--required-scope",
        action="append",
        help="Required OAuth scope; defaults to aiknowledge.read",
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
    is_loopback = args.host in {
        "127.0.0.1",
        "localhost",
        "::1",
    }
    auth_values = (
        args.oidc_issuer,
        args.oidc_jwks_url,
        args.resource_server_url,
    )
    has_auth = all(auth_values)
    if any(auth_values) and not has_auth:
        print(
            "error: oidc issuer, JWKS URL, and resource server URL are required together",
            file=sys.stderr,
        )
        return 2
    if args.db is not None and args.postgres_url is not None:
        print("error: configure either --db or --postgres-url", file=sys.stderr)
        return 2
    if args.postgres_url is not None and args.transport != "streamable-http":
        print("error: authenticated PostgreSQL MCP requires HTTP transport", file=sys.stderr)
        return 2
    if args.postgres_url is not None and not has_auth:
        print("error: PostgreSQL MCP requires OIDC authentication", file=sys.stderr)
        return 2
    if has_auth and args.postgres_url is None:
        print("error: authenticated MCP requires the PostgreSQL RLS catalog", file=sys.stderr)
        return 2
    if args.transport == "streamable-http" and not is_loopback and not has_auth:
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
    try:
        if args.postgres_url is not None:
            from mcp.server.auth.settings import AuthSettings
            from sqlalchemy import create_engine

            from aikb.oidc import (
                OIDCTokenVerifier,
                OIDCVerifierConfig,
                PostgresPrincipalDirectory,
            )

            scopes = frozenset(args.required_scope or ["aiknowledge.read"])
            engine = create_engine(args.postgres_url, pool_pre_ping=True)
            verifier = OIDCTokenVerifier(
                OIDCVerifierConfig(
                    issuer=args.oidc_issuer,
                    audience=args.resource_server_url,
                    jwks_url=args.oidc_jwks_url,
                    required_scopes=scopes,
                ),
                PostgresPrincipalDirectory(args.postgres_url, engine=engine),
            )
            config = MCPReadConfig(
                postgres_url=args.postgres_url,
                postgres_engine=engine,
                token_verifier=verifier,
                auth_settings=AuthSettings(
                    issuer_url=args.oidc_issuer,
                    resource_server_url=args.resource_server_url,
                    required_scopes=sorted(scopes),
                ),
            )
        else:
            config = MCPReadConfig(
                database=args.db or Path(".aikb/catalog.db"),
                allowed_repositories=(
                    frozenset(args.allowed_repositories)
                    if args.allowed_repositories is not None
                    else None
                ),
            )
        server = create_mcp_server(config)
    except (ImportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
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
