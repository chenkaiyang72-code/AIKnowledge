from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from aikb.catalog import Catalog, SCHEMA_VERSION
from aikb.context_pack import build_solution_context_pack
from aikb.ingestion import ingest_source
from aikb.solution import (
    SolutionManifest,
    publish_solution_snapshot,
    resolve_solution_scope,
    retrieve_solution_hybrid,
)


class SolutionSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "catalog.db"
        self.core_source = self._source_tree(
            "core",
            "int core_ready(void) { return 1; }\n"
            "int core_submit(void) { return 2; }\n"
            "int core_complete(void) { return 3; }\n"
            "int core_suspend(void) { return 4; }\n"
            "int core_resume(void) { return 5; }\n",
        )
        self.driver_source = self._source_tree(
            "driver",
            "int core_ready(void);\n"
            "int core_submit(void);\n"
            "int core_complete(void);\n"
            "int core_suspend(void);\n"
            "int core_resume(void);\n"
            "int driver_probe(void) { return core_ready(); }\n"
            "int driver_queue(void) { return core_submit(); }\n"
            "int driver_irq(void) { return core_complete(); }\n"
            "int driver_suspend(void) { return core_suspend(); }\n"
            "int driver_resume(void) { return core_resume(); }\n",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _source_tree(self, name: str, content: str) -> Path:
        source = self.root / name
        (source / "kernel").mkdir(parents=True)
        for directory in ["arch", "drivers", "fs", "mm", "include"]:
            (source / directory).mkdir()
        (source / "Makefile").write_text(
            "VERSION = 1\nPATCHLEVEL = 0\nSUBLEVEL = 0\nEXTRAVERSION =\n",
            encoding="utf-8",
        )
        (source / "Kconfig").write_text('mainmenu "fixture"\n', encoding="utf-8")
        (source / "kernel" / f"{name}.c").write_text(content, encoding="utf-8")
        return source

    @staticmethod
    def _scope(project: str, digest: str) -> dict[str, object]:
        return {
            "scope_id": f"scope-{project}",
            "source": {
                "project": project,
                "version": "1.0.0",
                "kind": "release_archive",
                "archive_name": f"{project}.tar.xz",
                "archive_sha256": digest * 64,
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

    @staticmethod
    def _manifest(core_snapshot: str, driver_snapshot: str, revision: str = "r1"):
        return SolutionManifest.model_validate(
            {
                "schema_version": 1,
                "name": "kernel-platform",
                "revision": revision,
                "description": "Pinned cross-repository fixture",
                "members": [
                    {
                        "repository": "kernel-core",
                        "snapshot_id": core_snapshot,
                        "role": "core",
                    },
                    {
                        "repository": "platform-driver",
                        "snapshot_id": driver_snapshot,
                        "role": "driver",
                    },
                ],
            }
        )

    def test_publish_is_immutable_idempotent_and_version_pinned(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            core = ingest_source(
                catalog, self._scope("kernel-core", "a"), self.core_source
            )
            driver = ingest_source(
                catalog, self._scope("platform-driver", "b"), self.driver_source
            )
            manifest = self._manifest(core["id"], driver["id"])
            first = publish_solution_snapshot(catalog, manifest)
            second = publish_solution_snapshot(catalog, manifest)

            (self.core_source / "kernel" / "core.c").write_text(
                "int core_newest_only(void) { return 2; }\n", encoding="utf-8"
            )
            newest = ingest_source(
                catalog, self._scope("kernel-core", "a"), self.core_source
            )
            resolved = resolve_solution_scope(catalog, "kernel-platform")
            retrieval = retrieve_solution_hybrid(
                catalog, "core_ready driver_probe", resolved, top_k=10
            )
            pack = build_solution_context_pack(
                catalog, "core_ready driver_probe", resolved, max_evidence_items=6
            )
            version = catalog.connection.execute(
                "SELECT value FROM schema_metadata WHERE key='schema_version'"
            ).fetchone()["value"]

        self.assertFalse(first.idempotent)
        self.assertTrue(second.idempotent)
        self.assertEqual(first.solution_snapshot_id, second.solution_snapshot_id)
        self.assertEqual(version, str(SCHEMA_VERSION))
        self.assertNotEqual(newest["id"], core["id"])
        self.assertEqual(
            {row["snapshot_id"] for row in resolved.snapshots},
            {core["id"], driver["id"]},
        )
        self.assertNotIn(newest["id"], {row["snapshot_id"] for row in resolved.snapshots})
        repositories = {item.hit.repository for item in retrieval.hits}
        self.assertEqual(repositories, {"kernel-core", "platform-driver"})
        self.assertTrue(
            any(
                link.symbol == "core_ready"
                and link.source_repository == "platform-driver"
                and link.target_repository == "kernel-core"
                for link in pack.cross_repository_links
            )
        )
        self.assertFalse(
            any("core_newest_only" in item.hit.content for item in retrieval.hits)
        )

    def test_new_solution_revision_supersedes_and_old_can_be_reactivated(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            core = ingest_source(
                catalog, self._scope("kernel-core", "a"), self.core_source
            )
            driver = ingest_source(
                catalog, self._scope("platform-driver", "b"), self.driver_source
            )
            first_manifest = self._manifest(core["id"], driver["id"])
            first = publish_solution_snapshot(catalog, first_manifest)
            second_manifest = self._manifest(core["id"], driver["id"], revision="r2")
            second = publish_solution_snapshot(catalog, second_manifest)
            reactivated = publish_solution_snapshot(catalog, first_manifest)
            states = catalog.connection.execute(
                "SELECT id, state FROM solution_snapshot ORDER BY id"
            ).fetchall()

        self.assertIn(first.solution_snapshot_id, second.superseded_solution_snapshot_ids)
        self.assertTrue(reactivated.idempotent)
        self.assertTrue(reactivated.reactivated)
        self.assertIn(second.solution_snapshot_id, reactivated.superseded_solution_snapshot_ids)
        active = [row["id"] for row in states if row["state"] == "active"]
        self.assertEqual(active, [first.solution_snapshot_id])

    def test_visibility_filter_does_not_return_hidden_member_metadata(self) -> None:
        with Catalog(self.database) as catalog:
            catalog.initialize()
            core = ingest_source(
                catalog, self._scope("kernel-core", "a"), self.core_source
            )
            driver = ingest_source(
                catalog, self._scope("platform-driver", "b"), self.driver_source
            )
            publish_solution_snapshot(catalog, self._manifest(core["id"], driver["id"]))
            resolved = resolve_solution_scope(
                catalog,
                "kernel-platform",
                allowed_repositories={"kernel-core"},
            )
            retrieval = retrieve_solution_hybrid(
                catalog, "core_ready", resolved, top_k=10
            )
            pack = build_solution_context_pack(
                catalog, "core_ready", resolved, max_evidence_items=6
            )

        serialized = json.dumps(
            {
                "scope": resolved.as_dict(),
                "context_pack": pack.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
        self.assertTrue(resolved.partial_visibility)
        self.assertTrue(pack.scope.partial_visibility)
        self.assertIsNone(pack.scope.requested_solution_snapshot_id)
        self.assertIsNone(pack.scope.solution_manifest_digest)
        self.assertTrue(pack.coverage.partial_visibility)
        self.assertEqual(pack.scope.kind, "solution_snapshot")
        self.assertEqual(pack.retrieval_trace.routing, "all_visible_solution_members")
        self.assertEqual(len(resolved.snapshots), 1)
        self.assertNotIn("platform-driver", serialized)
        self.assertNotIn(driver["id"], serialized)
        self.assertNotIn("driver_probe", serialized)
        self.assertEqual(
            {item.hit.repository for item in retrieval.hits}, {"kernel-core"}
        )

    def test_ten_cross_repository_questions_have_full_routing_recall(self) -> None:
        questions = [
            "core_ready driver_probe",
            "driver_probe 如何通过 core_ready 完成初始化",
            "core_submit driver_queue",
            "driver_queue 调用 core_submit 的路径",
            "core_complete driver_irq",
            "driver_irq 如何通知 core_complete",
            "core_suspend driver_suspend",
            "driver_suspend 与 core_suspend 的关系",
            "core_resume driver_resume",
            "driver_resume 如何恢复 core_resume",
        ]
        with Catalog(self.database) as catalog:
            catalog.initialize()
            core = ingest_source(
                catalog, self._scope("kernel-core", "a"), self.core_source
            )
            driver = ingest_source(
                catalog, self._scope("platform-driver", "b"), self.driver_source
            )
            publish_solution_snapshot(catalog, self._manifest(core["id"], driver["id"]))
            scope = resolve_solution_scope(catalog, "kernel-platform")
            expected_snapshots = {core["id"], driver["id"]}
            successful_routes = 0
            version_correct = 0
            evidence_count = 0
            for question in questions:
                result = retrieve_solution_hybrid(catalog, question, scope, top_k=10)
                repositories = {item.hit.repository for item in result.hits}
                if repositories == {"kernel-core", "platform-driver"}:
                    successful_routes += 1
                for item in result.hits:
                    evidence_count += 1
                    if item.hit.snapshot_id in expected_snapshots:
                        version_correct += 1

        self.assertEqual(successful_routes / len(questions), 1.0)
        self.assertGreater(evidence_count, 0)
        self.assertEqual(version_correct / evidence_count, 1.0)


if __name__ == "__main__":
    unittest.main()
