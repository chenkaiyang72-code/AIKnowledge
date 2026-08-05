from __future__ import annotations

import json
import asyncio
import socket
import sys
import time
from contextlib import redirect_stderr
from io import StringIO
import tempfile
import unittest
from pathlib import Path

import httpx2
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
import uvicorn

from aikb.catalog import Catalog
from aikb.context_pack import ContextPack, ContextScope
from aikb.ingestion import ingest_source
from aikb.mcp_server import MCPReadConfig, RetrievedSource, create_mcp_server
from aikb.mcp_cli import main as mcp_main
from aikb.solution import SolutionManifest, publish_solution_snapshot


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "catalog.db"
        with Catalog(self.database) as catalog:
            catalog.initialize()
            core = self._ingest(
                catalog,
                "mcp-core",
                "a",
                "int core_ready(void) { return 1; }\n",
            )
            driver = self._ingest(
                catalog,
                "mcp-driver",
                "b",
                "int core_ready(void);\n"
                "int driver_probe(void) { return core_ready(); }\n",
            )
            self.core_snapshot = core["id"]
            self.driver_snapshot = driver["id"]
            publish_solution_snapshot(
                catalog,
                SolutionManifest.model_validate(
                    {
                        "name": "mcp-solution",
                        "revision": "r1",
                        "members": [
                            {
                                "repository": "mcp-core",
                                "snapshot_id": self.core_snapshot,
                                "role": "core",
                            },
                            {
                                "repository": "mcp-driver",
                                "snapshot_id": self.driver_snapshot,
                                "role": "driver",
                            },
                        ],
                    }
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _ingest(
        self,
        catalog: Catalog,
        project: str,
        digest_character: str,
        content: str,
    ) -> dict[str, object]:
        source = self.root / project
        (source / "kernel").mkdir(parents=True)
        for directory in ["arch", "drivers", "fs", "mm", "include"]:
            (source / directory).mkdir()
        (source / "Makefile").write_text(
            "VERSION = 1\nPATCHLEVEL = 0\nSUBLEVEL = 0\nEXTRAVERSION =\n",
            encoding="utf-8",
        )
        (source / "Kconfig").write_text('mainmenu "mcp"\n', encoding="utf-8")
        (source / "kernel" / "fixture.c").write_text(content, encoding="utf-8")
        scope = {
            "scope_id": f"scope-{project}",
            "source": {
                "project": project,
                "version": "1.0.0",
                "kind": "release_archive",
                "archive_name": f"{project}.tar.xz",
                "archive_sha256": digest_character * 64,
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
        return ingest_source(catalog, scope, source)

    async def test_in_memory_protocol_exposes_only_three_read_tools(self) -> None:
        server = create_mcp_server(MCPReadConfig(database=self.database))
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            scope_result = await client.call_tool(
                "aikb_scope_resolve", {"solution": "mcp-solution"}
            )
            search_result = await client.call_tool(
                "aikb_context_search",
                {
                    "solution": "mcp-solution",
                    "query": "core_ready driver_probe",
                    "max_evidence_items": 4,
                },
            )
            get_result = await client.call_tool(
                "aikb_context_get",
                {
                    "repository": "mcp-core",
                    "snapshot_id": self.core_snapshot,
                    "path": "kernel/fixture.c",
                    "line": 1,
                },
            )

        self.assertEqual(
            set(tools),
            {"aikb_scope_resolve", "aikb_context_search", "aikb_context_get"},
        )
        for tool in tools.values():
            self.assertTrue(tool.annotations.read_only_hint)
            self.assertFalse(tool.annotations.destructive_hint)
            self.assertTrue(tool.annotations.idempotent_hint)
            self.assertFalse(tool.annotations.open_world_hint)
        self.assertFalse(scope_result.is_error)
        self.assertFalse(search_result.is_error)
        self.assertFalse(get_result.is_error)
        scope = ContextScope.model_validate(scope_result.structured_content)
        pack = ContextPack.model_validate(search_result.structured_content)
        source = RetrievedSource.model_validate(get_result.structured_content)
        self.assertEqual(scope.kind, "solution_snapshot")
        self.assertEqual(
            {item.repository for item in pack.evidence},
            {"mcp-core", "mcp-driver"},
        )
        self.assertEqual(source.citation, "mcp-core@release:1.0.0:kernel/fixture.c:1-1")

    async def test_visibility_filter_and_errors_do_not_leak_hidden_repository(self) -> None:
        server = create_mcp_server(
            MCPReadConfig(
                database=self.database,
                allowed_repositories=frozenset({"mcp-core"}),
            )
        )
        async with Client(server) as client:
            scope_result = await client.call_tool(
                "aikb_scope_resolve", {"solution": "mcp-solution"}
            )
            search_result = await client.call_tool(
                "aikb_context_search",
                {"solution": "mcp-solution", "query": "core_ready"},
            )
            forbidden = await client.call_tool(
                "aikb_scope_resolve", {"repository": "mcp-driver"}
            )
            traversal = await client.call_tool(
                "aikb_context_get",
                {
                    "repository": "mcp-core",
                    "path": "../secret.c",
                    "line": 1,
                },
            )

        serialized = json.dumps(
            {
                "scope": scope_result.structured_content,
                "search": search_result.structured_content,
            },
            ensure_ascii=False,
        )
        self.assertFalse(scope_result.is_error)
        self.assertFalse(search_result.is_error)
        self.assertTrue(forbidden.is_error)
        self.assertTrue(traversal.is_error)
        self.assertNotIn("mcp-driver", serialized)
        self.assertNotIn(self.driver_snapshot, serialized)
        self.assertNotIn("driver_probe", serialized)
        error_payload = forbidden.model_dump_json() + traversal.model_dump_json()
        self.assertNotIn("mcp-driver", error_payload)
        self.assertNotIn(self.driver_snapshot, error_payload)
        self.assertNotIn(str(self.root), error_payload)
        self.assertIsNone(
            scope_result.structured_content["requested_solution_snapshot_id"]
        )
        self.assertIsNone(scope_result.structured_content["solution_manifest_digest"])

    async def test_one_hundred_scope_calls_have_stable_protocol_results(self) -> None:
        server = create_mcp_server(MCPReadConfig(database=self.database))
        files_before = sorted(path.name for path in self.root.iterdir())
        stat_before = self.database.stat()
        payloads: list[dict[str, object]] = []
        async with Client(server) as client:
            for _ in range(100):
                result = await client.call_tool(
                    "aikb_scope_resolve", {"repository": "mcp-core"}
                )
                self.assertFalse(result.is_error)
                payloads.append(result.structured_content)

        self.assertTrue(all(payload == payloads[0] for payload in payloads))
        self.assertEqual(files_before, sorted(path.name for path in self.root.iterdir()))
        stat_after = self.database.stat()
        self.assertEqual(stat_before.st_size, stat_after.st_size)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)

    async def test_read_tool_does_not_create_a_missing_catalog(self) -> None:
        missing_database = self.root / "missing" / "catalog.db"
        server = create_mcp_server(MCPReadConfig(database=missing_database))
        async with Client(server) as client:
            result = await client.call_tool(
                "aikb_scope_resolve", {"repository": "mcp-core"}
            )

        self.assertTrue(result.is_error)
        self.assertFalse(missing_database.parent.exists())
        self.assertNotIn(str(missing_database), result.model_dump_json())

    async def test_stdio_cli_transport_lists_the_same_read_tools(self) -> None:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "aikb",
                "mcp-serve",
                "--db",
                str(self.database),
            ],
            cwd=str(Path(__file__).parents[1]),
        )
        async with Client(stdio_client(parameters)) as client:
            listed = await client.list_tools()

        self.assertEqual(
            {tool.name for tool in listed.tools},
            {"aikb_scope_resolve", "aikb_context_search", "aikb_context_get"},
        )

    async def test_stateless_streamable_http_transport_is_usable(self) -> None:
        server = create_mcp_server(MCPReadConfig(database=self.database))
        app = server.streamable_http_app(
            streamable_http_path="/mcp/read",
            stateless_http=True,
            json_response=True,
            max_request_body_size=1_048_576,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=port,
                log_level="critical",
            )
        )
        task = asyncio.create_task(uvicorn_server.serve())
        try:
            for _ in range(100):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(uvicorn_server.started)
            async with Client(f"http://127.0.0.1:{port}/mcp/read") as client:
                listed = await client.list_tools()
                result = await client.call_tool(
                    "aikb_scope_resolve", {"repository": "mcp-core"}
                )
            self.assertEqual(len(listed.tools), 3)
            self.assertFalse(result.is_error)
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=10)

    async def test_authenticated_http_returns_rfc9728_challenge_and_accepts_token(self) -> None:
        class StaticVerifier:
            async def verify_token(self, token: str) -> AccessToken | None:
                if token != "valid-token":
                    return None
                return AccessToken(
                    token=token,
                    client_id="cursor",
                    scopes=["aiknowledge.read"],
                    expires_at=int(time.time()) + 300,
                    resource="https://kb.example/mcp/read",
                    subject="alice",
                    claims={
                        "iss": "https://issuer.example",
                        "aikb_principal_id": "principal-alice",
                        "aikb_security_domain_id": "domain-kernel",
                    },
                )

        server = create_mcp_server(
            MCPReadConfig(
                postgres_engine=object(),
                token_verifier=StaticVerifier(),
                auth_settings=AuthSettings(
                    issuer_url="https://issuer.example",
                    resource_server_url="https://kb.example/mcp/read",
                    required_scopes=["aiknowledge.read"],
                ),
            )
        )
        app = server.streamable_http_app(
            streamable_http_path="/mcp/read",
            stateless_http=True,
            json_response=True,
        )
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        uvicorn_server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
        )
        task = asyncio.create_task(uvicorn_server.serve())
        try:
            for _ in range(100):
                if uvicorn_server.started:
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(uvicorn_server.started)
            base_url = f"http://127.0.0.1:{port}"
            async with httpx2.AsyncClient(base_url=base_url) as anonymous:
                denied = await anonymous.post("/mcp/read", json={})
                metadata = await anonymous.get(
                    "/.well-known/oauth-protected-resource/mcp/read"
                )
            self.assertEqual(denied.status_code, 401)
            self.assertIn("resource_metadata=", denied.headers["www-authenticate"])
            self.assertEqual(metadata.status_code, 200)
            self.assertEqual(metadata.json()["resource"], "https://kb.example/mcp/read")
            self.assertEqual(
                metadata.json()["authorization_servers"],
                ["https://issuer.example"],
            )

            async with httpx2.AsyncClient(
                headers={"Authorization": "Bearer valid-token"}
            ) as authenticated:
                transport = streamable_http_client(
                    f"{base_url}/mcp/read",
                    http_client=authenticated,
                )
                async with Client(transport) as client:
                    listed = await client.list_tools()
            self.assertEqual(len(listed.tools), 3)
        finally:
            uvicorn_server.should_exit = True
            await asyncio.wait_for(task, timeout=10)

    def test_unauthenticated_http_rejects_non_loopback_binding(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = mcp_main(
                [
                    "mcp-serve",
                    "--transport",
                    "streamable-http",
                    "--host",
                    "0.0.0.0",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("restricted to loopback", stderr.getvalue())

    def test_postgres_http_rejects_missing_oidc_configuration(self) -> None:
        stderr = StringIO()
        with redirect_stderr(stderr):
            result = mcp_main(
                [
                    "mcp-serve",
                    "--transport",
                    "streamable-http",
                    "--postgres-url",
                    "postgresql+psycopg://example.invalid/aikb",
                ]
            )

        self.assertEqual(result, 2)
        self.assertIn("requires OIDC", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
