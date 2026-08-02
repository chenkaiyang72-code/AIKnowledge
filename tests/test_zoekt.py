from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aikb.catalog import Catalog
from aikb.context_pack import build_context_pack
from aikb.ingestion import ingest_source
from aikb.retrieval import retrieve_hybrid
from aikb.zoekt import (
    ZoektClient,
    ZoektReadCatalog,
    export_snapshot_for_zoekt,
    zoekt_repository_name,
)


class ZoektTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "linux"
        self.source.mkdir()
        (self.source / "Makefile").write_text(
            "VERSION = 6\nPATCHLEVEL = 18\nSUBLEVEL = 40\nEXTRAVERSION =\n",
            encoding="utf-8",
        )
        (self.source / "Kconfig").write_text('mainmenu "test"\n', encoding="utf-8")
        for directory in ["arch", "drivers", "fs", "kernel", "mm", "include"]:
            (self.source / directory).mkdir()
        (self.source / "kernel" / "demo.c").write_text(
            "static int helper(void) { return 1; }\n"
            "int exact_zoekt_marker(void) { return helper(); }\n",
            encoding="utf-8",
        )
        self.scope = {
            "scope_id": "zoekt-test",
            "source": {
                "project": "linux-zoekt-test",
                "version": "6.18.40",
                "kind": "release_archive",
                "archive_name": "linux-zoekt-test.tar.xz",
                "archive_sha256": "d" * 64,
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
        self.database = self.root / "catalog.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_export_is_immutable_idempotent_and_source_only(self) -> None:
        output = self.root / "export"
        with Catalog(self.database) as catalog:
            catalog.initialize()
            snapshot = ingest_source(catalog, self.scope, self.source)
            first = export_snapshot_for_zoekt(catalog, output, snapshot["id"])
            second = export_snapshot_for_zoekt(catalog, output, snapshot["id"])

        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.export_digest, second.export_digest)
        self.assertEqual(first.file_count, 1)
        self.assertTrue((first.source / "kernel" / "demo.c").is_file())
        self.assertFalse((first.source / "Makefile").exists())
        metadata = json.loads(first.metadata.read_text(encoding="utf-8"))
        self.assertEqual(metadata["Name"], zoekt_repository_name(snapshot["id"]))
        self.assertEqual(metadata["Branches"][0]["Version"], snapshot["revision"])

        (first.source / "kernel" / "demo.c").write_text(
            "tampered\n", encoding="utf-8"
        )
        with Catalog(self.database) as catalog:
            catalog.initialize()
            with self.assertRaisesRegex(RuntimeError, "size mismatch|digest mismatch"):
                export_snapshot_for_zoekt(catalog, output, snapshot["id"])

    def test_zoekt_results_map_back_to_authoritative_chunks(self) -> None:
        requests: list[dict[str, object]] = []
        with Catalog(self.database) as catalog:
            catalog.initialize()
            snapshot = ingest_source(catalog, self.scope, self.source)
            internal_repository = zoekt_repository_name(snapshot["id"])

            class Handler(BaseHTTPRequestHandler):
                def do_POST(self) -> None:  # noqa: N802
                    length = int(self.headers["Content-Length"])
                    requests.append(json.loads(self.rfile.read(length)))
                    payload = {
                        "Result": {
                            "Files": [
                                {
                                    "FileName": "kernel/demo.c",
                                    "Repository": internal_repository,
                                    "Version": snapshot["revision"],
                                    "Score": 12.5,
                                    "LineMatches": [
                                        {"LineNumber": 2, "Score": 8.0}
                                    ],
                                }
                            ]
                        }
                    }
                    encoded = json.dumps(payload).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

                def log_message(self, format: str, *args: object) -> None:
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                wrapped = ZoektReadCatalog(
                    catalog,
                    ZoektClient(f"http://127.0.0.1:{server.server_port}"),
                )
                result = retrieve_hybrid(
                    wrapped,
                    "exact_zoekt_marker",
                    repository="linux-zoekt-test",
                )
                pack = build_context_pack(
                    wrapped,
                    "exact_zoekt_marker",
                    repository="linux-zoekt-test",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

        lexical_hits = [
            item
            for item in result.hits
            if "lexical_zoekt"
            in {contribution.channel for contribution in item.contributions}
        ]
        self.assertGreaterEqual(len(lexical_hits), 1)
        self.assertEqual(lexical_hits[0].hit.path, "kernel/demo.c")
        self.assertEqual(lexical_hits[0].hit.snapshot_id, snapshot["id"])
        self.assertEqual(pack.schema_version, "1.2")
        self.assertIn("lexical_zoekt", pack.retrieval_trace.channel_candidate_counts)
        self.assertEqual(len(requests), 2)
        self.assertIn(f"repo:{internal_repository}", requests[0]["Q"])
        self.assertTrue(requests[0]["Opts"]["UseBM25Scoring"])

    def test_unavailable_zoekt_uses_fts_fallback(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        with Catalog(self.database) as catalog:
            catalog.initialize()
            ingest_source(catalog, self.scope, self.source)
            wrapped = ZoektReadCatalog(
                catalog,
                ZoektClient(
                    f"http://127.0.0.1:{port}", timeout_seconds=0.2
                ),
            )
            result = retrieve_hybrid(wrapped, "exact_zoekt_marker")

        self.assertIn("lexical_fts5", result.channel_candidate_counts)
        self.assertNotIn("lexical_zoekt", result.channel_candidate_counts)


ZOEKT_TEST_URL = os.environ.get("AIKB_TEST_ZOEKT_URL")
ZOEKT_TEST_DB = os.environ.get("AIKB_TEST_ZOEKT_DB")
ZOEKT_TEST_SNAPSHOT = os.environ.get("AIKB_TEST_ZOEKT_SNAPSHOT")


@unittest.skipUnless(
    ZOEKT_TEST_URL and ZOEKT_TEST_DB and ZOEKT_TEST_SNAPSHOT,
    "live Zoekt integration is not configured",
)
class ZoektLiveIntegrationTests(unittest.TestCase):
    def test_live_zoekt_image_returns_versioned_chunk(self) -> None:
        with Catalog(Path(ZOEKT_TEST_DB)) as catalog:
            catalog.initialize()
            wrapped = ZoektReadCatalog(
                catalog,
                ZoektClient(ZOEKT_TEST_URL),
                fallback_on_unavailable=False,
            )
            result = wrapped.search_lexical(
                "zoekt_live_marker",
                snapshot_id=ZOEKT_TEST_SNAPSHOT,
            )

        self.assertEqual(result.channel, "lexical_zoekt")
        self.assertGreaterEqual(len(result.hits), 1)
        self.assertEqual(result.hits[0].snapshot_id, ZOEKT_TEST_SNAPSHOT)
        self.assertEqual(result.hits[0].path, "kernel/live.c")


if __name__ == "__main__":
    unittest.main()
