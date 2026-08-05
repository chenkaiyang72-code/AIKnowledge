from __future__ import annotations

import json
import os
import unittest
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from sqlalchemy import create_engine, text

from aikb.github_acl import (
    GitHubACLPlanConfig,
    GitHubACLSnapshot,
    GitHubCollaboratorClient,
    GitHubRepositorySpec,
    PostgresGitHubACLPlanner,
)


class FakeResponse:
    def __init__(self, payload: object, link: str | None = None):
        self._stream = BytesIO(json.dumps(payload).encode("utf-8"))
        self.headers = {"Link": link} if link is not None else {}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class GitHubACLUnitTests(unittest.TestCase):
    def test_client_paginates_and_maps_effective_base_permissions(self) -> None:
        requests: list[object] = []
        responses = iter(
            [
                FakeResponse(
                    [
                        {
                            "id": 1,
                            "login": "reader",
                            "permissions": {"pull": True},
                        },
                        {
                            "id": 2,
                            "login": "writer",
                            "permissions": {"push": True, "pull": True},
                        },
                    ],
                    '<https://api.github.com/repos/acme/kernel/collaborators?'
                    'affiliation=all&per_page=100&page=2>; rel="next"',
                ),
                FakeResponse(
                    [
                        {
                            "id": 3,
                            "login": "owner",
                            "permissions": {"admin": True},
                        }
                    ]
                ),
            ]
        )

        def opener(request: object, **_: object) -> FakeResponse:
            requests.append(request)
            return next(responses)

        snapshot = GitHubCollaboratorClient("test-token", opener=opener).fetch(
            GitHubRepositorySpec(owner="acme", repository="kernel")
        )
        self.assertEqual(snapshot.repository, "acme/kernel")
        self.assertEqual(
            [item.permission for item in snapshot.collaborators],
            ["read", "write", "admin"],
        )
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer test-token")
        self.assertEqual(len(snapshot.digest()), 64)

    def test_client_rejects_pagination_that_escapes_configured_endpoint(self) -> None:
        response = FakeResponse(
            [],
            '<https://attacker.example/collect?page=2>; rel="next"',
        )

        def opener(*_: object, **__: object) -> FakeResponse:
            return response

        with self.assertRaises(RuntimeError):
            GitHubCollaboratorClient("test-token", opener=opener).fetch(
                GitHubRepositorySpec(owner="acme", repository="kernel")
            )

    def test_config_rejects_duplicate_or_insecure_bindings(self) -> None:
        payload = {
            "schema_version": 1,
            "security_domain_id": "domain-unit",
            "repository_id": "repo-unit",
            "github": {"owner": "acme", "repository": "kernel"},
            "bindings": [
                {"github_user_id": 1, "principal_id": "principal-one"},
                {"github_user_id": 1, "principal_id": "principal-two"},
            ],
        }
        with self.assertRaises(ValueError):
            GitHubACLPlanConfig.model_validate(payload)
        payload["bindings"] = []
        payload["github"]["api_base_url"] = "http://api.github.com"
        with self.assertRaises(ValueError):
            GitHubACLPlanConfig.model_validate(payload)


POSTGRES_URL = os.environ.get("AIKB_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "AIKB_TEST_POSTGRES_URL is not configured")
class GitHubACLPlannerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from alembic import command
        from alembic.config import Config

        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", POSTGRES_URL)
        command.upgrade(config, "head")
        cls.engine = create_engine(POSTGRES_URL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()

    def test_plan_is_read_only_and_separates_updates_revokes_and_gaps(self) -> None:
        suffix = uuid.uuid4().hex
        domain_id = f"domain_{suffix}"
        repository_id = f"repo_{suffix}"
        principal_read = f"principal_read_{suffix}"
        principal_write = f"principal_write_{suffix}"
        principal_stale = f"principal_stale_{suffix}"
        with self.engine.begin() as connection:
            connection.execute(
                text("INSERT INTO security_domain(id,name) VALUES (:id,:name)"),
                {"id": domain_id, "name": f"GitHub ACL {suffix}"},
            )
            connection.execute(
                text(
                    "INSERT INTO repository(id,name,source_kind,source_uri) "
                    "VALUES (:id,:name,'git','https://github.com/acme/kernel')"
                ),
                {"id": repository_id, "name": f"github-acl-{suffix}"},
            )
            for ordinal, principal_id in enumerate(
                (principal_read, principal_write, principal_stale),
                start=1,
            ):
                connection.execute(
                    text(
                        "INSERT INTO principal(id,security_domain_id,issuer,subject) "
                        "VALUES (:id,:domain,'https://issuer.example',:subject)"
                    ),
                    {
                        "id": principal_id,
                        "domain": domain_id,
                        "subject": f"github-acl-{suffix}-{ordinal}",
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO repository_grant(id,security_domain_id,repository_id,"
                    "principal_id,permission) VALUES "
                    "(:read_id,:domain,:repository,:read_principal,'read'),"
                    "(:stale_id,:domain,:repository,:stale_principal,'admin')"
                ),
                {
                    "read_id": f"grant_read_{suffix}",
                    "stale_id": f"grant_stale_{suffix}",
                    "domain": domain_id,
                    "repository": repository_id,
                    "read_principal": principal_read,
                    "stale_principal": principal_stale,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO repository_grant_source(id,repository_grant_id,"
                    "source_kind,source_key,permission) VALUES "
                    "(:read_source,:read_grant,'manual','integration-test','read'),"
                    "(:stale_source,:stale_grant,'manual','integration-test','admin')"
                ),
                {
                    "read_source": f"source_read_{suffix}",
                    "read_grant": f"grant_read_{suffix}",
                    "stale_source": f"source_stale_{suffix}",
                    "stale_grant": f"grant_stale_{suffix}",
                },
            )
        try:
            config = GitHubACLPlanConfig.model_validate(
                {
                    "schema_version": 1,
                    "security_domain_id": domain_id,
                    "repository_id": repository_id,
                    "github": {"owner": "acme", "repository": "kernel"},
                    "bindings": [
                        {"github_user_id": 1, "principal_id": principal_read},
                        {"github_user_id": 2, "principal_id": principal_write},
                        {"github_user_id": 3, "principal_id": principal_stale},
                    ],
                }
            )
            snapshot = GitHubACLSnapshot.model_validate(
                {
                    "repository": "ACME/Kernel",
                    "captured_at": datetime.now(timezone.utc),
                    "collaborators": [
                        {"user_id": 1, "login": "reader", "permission": "read"},
                        {"user_id": 2, "login": "writer", "permission": "write"},
                        {"user_id": 99, "login": "unbound", "permission": "read"},
                    ],
                }
            )
            plan = PostgresGitHubACLPlanner(self.engine).plan(config, snapshot)
            self.assertEqual(plan.unchanged_count, 1)
            self.assertEqual(
                plan.activate_or_update,
                (
                    {
                        "principal_id": principal_write,
                        "permission": "write",
                        "reason": "missing_direct_grant",
                    },
                ),
            )
            self.assertEqual(plan.revoke_candidates[0]["principal_id"], principal_stale)
            self.assertTrue(plan.revoke_candidates[0]["requires_review"])
            self.assertEqual(
                plan.revoke_candidates[0]["active_source_kinds"],
                ["manual"],
            )
            self.assertEqual(plan.unmatched_collaborators[0]["github_user_id"], 99)
            self.assertEqual(plan.stale_bindings[0]["github_user_id"], 3)
            with self.engine.connect() as connection:
                grant_count = connection.execute(
                    text(
                        "SELECT count(*) FROM repository_grant "
                        "WHERE security_domain_id=:domain"
                    ),
                    {"domain": domain_id},
                ).scalar_one()
            self.assertEqual(grant_count, 2)
        finally:
            with self.engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM security_domain WHERE id=:id"),
                    {"id": domain_id},
                )
                connection.execute(
                    text("DELETE FROM repository WHERE id=:id"),
                    {"id": repository_id},
                )
