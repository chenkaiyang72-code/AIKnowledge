from __future__ import annotations

import os
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from aikb.security_admin import PostgresSecurityAdmin, SecurityManifest


def manifest_payload(suffix: str, repository_id: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "domain": {
            "id": f"domain_{suffix}",
            "name": f"Security admin {suffix}",
            "data_policy": {"source_export": "internal-only"},
        },
        "principals": [
            {
                "id": f"principal_{suffix}",
                "issuer": "https://issuer.example/",
                "subject": f"subject-{suffix}",
                "display_name": "Security Admin Test",
            }
        ],
        "teams": [{"id": f"team_{suffix}", "name": f"Team {suffix}"}],
        "memberships": [
            {
                "team_id": f"team_{suffix}",
                "principal_id": f"principal_{suffix}",
                "role": "maintainer",
            }
        ],
        "repository_grants": [
            {
                "repository_id": repository_id,
                "team_id": f"team_{suffix}",
                "permission": "read",
            }
        ],
    }


class SecurityManifestUnitTests(unittest.TestCase):
    def test_manifest_is_strict_and_digest_is_stable(self) -> None:
        payload = manifest_payload("unit", "repo_unit")
        manifest = SecurityManifest.model_validate(payload)
        duplicate = SecurityManifest.model_validate(payload)
        self.assertEqual(manifest.principals[0].issuer, "https://issuer.example/")
        self.assertEqual(manifest.digest(), duplicate.digest())
        self.assertEqual(len(manifest.digest()), 64)

    def test_manifest_rejects_ambiguous_grant_and_duplicate_membership(self) -> None:
        payload = manifest_payload("invalid", "repo_invalid")
        payload["repository_grants"] = [
            {
                "repository_id": "repo_invalid",
                "principal_id": "principal_invalid",
                "team_id": "team_invalid",
            }
        ]
        with self.assertRaises(ValueError):
            SecurityManifest.model_validate(payload)

        duplicate = manifest_payload("duplicate", "repo_duplicate")
        duplicate["memberships"] = [
            duplicate["memberships"][0],
            duplicate["memberships"][0],
        ]
        with self.assertRaises(ValueError):
            SecurityManifest.model_validate(duplicate)

    def test_manifest_rejects_insecure_remote_issuer_and_naive_expiry(self) -> None:
        payload = manifest_payload("issuer", "repo_issuer")
        payload["principals"][0]["issuer"] = "http://issuer.example"
        with self.assertRaises(ValueError):
            SecurityManifest.model_validate(payload)

        expiry = manifest_payload("expiry", "repo_expiry")
        expiry["repository_grants"][0]["expires_at"] = datetime(2030, 1, 1)
        with self.assertRaises(ValueError):
            SecurityManifest.model_validate(expiry)


POSTGRES_URL = os.environ.get("AIKB_TEST_POSTGRES_URL")


@unittest.skipUnless(POSTGRES_URL, "AIKB_TEST_POSTGRES_URL is not configured")
class SecurityAdminIntegrationTests(unittest.TestCase):
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

    def test_apply_is_atomic_idempotent_revocable_and_rls_visible(self) -> None:
        suffix = uuid.uuid4().hex
        domain_id = f"domain_{suffix}"
        principal_id = f"principal_{suffix}"
        repository_id = f"repo_{suffix}"
        repository_name = f"security-admin-{suffix}"
        payload = manifest_payload(suffix, repository_id)
        manifest = SecurityManifest.model_validate(payload)
        admin = PostgresSecurityAdmin(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO repository(id,name,source_kind,source_uri) "
                    "VALUES (:id,:name,'test','test://security-admin')"
                ),
                {"id": repository_id, "name": repository_name},
            )
        try:
            dry_run = admin.apply(manifest, dry_run=True)
            self.assertTrue(dry_run.dry_run)
            with self.engine.connect() as connection:
                self.assertIsNone(
                    connection.execute(
                        text("SELECT id FROM security_domain WHERE id=:id"),
                        {"id": domain_id},
                    ).first()
                )

            first = admin.apply(manifest)
            second = admin.apply(manifest)
            self.assertEqual(first.manifest_digest, second.manifest_digest)
            with self.engine.connect() as connection:
                counts = connection.execute(
                    text(
                        "SELECT "
                        "(SELECT count(*) FROM principal WHERE security_domain_id=:domain),"
                        "(SELECT count(*) FROM security_team WHERE security_domain_id=:domain),"
                        "(SELECT count(*) FROM repository_grant "
                        " WHERE security_domain_id=:domain)"
                    ),
                    {"domain": domain_id},
                ).one()
            self.assertEqual(tuple(counts), (1, 1, 1))

            def visible_repositories() -> list[str]:
                with self.engine.connect() as connection:
                    transaction = connection.begin()
                    connection.execute(text("SET LOCAL ROLE aikb_reader"))
                    connection.execute(
                        text("SELECT set_config('aikb.principal_id',:value,true)"),
                        {"value": principal_id},
                    )
                    connection.execute(
                        text(
                            "SELECT set_config('aikb.security_domain_id',:value,true)"
                        ),
                        {"value": domain_id},
                    )
                    rows = connection.execute(
                        text("SELECT name FROM repository ORDER BY name")
                    ).scalars().all()
                    transaction.rollback()
                return list(rows)

            self.assertEqual(visible_repositories(), [repository_name])

            removed_member_payload = manifest.model_dump(mode="json")
            removed_member_payload["memberships"][0]["active"] = False
            admin.apply(SecurityManifest.model_validate(removed_member_payload))
            with self.engine.connect() as connection:
                member_count = connection.execute(
                    text(
                        "SELECT count(*) FROM security_team_member "
                        "WHERE team_id=:team"
                    ),
                    {"team": f"team_{suffix}"},
                ).scalar_one()
            self.assertEqual(member_count, 0)
            self.assertEqual(visible_repositories(), [])
            admin.apply(manifest)
            self.assertEqual(visible_repositories(), [repository_name])

            revoked_payload = manifest.model_dump(mode="json")
            revoked_payload["repository_grants"][0]["active"] = False
            admin.apply(SecurityManifest.model_validate(revoked_payload))
            self.assertEqual(visible_repositories(), [])
            with self.engine.connect() as connection:
                revoked_at = connection.execute(
                    text(
                        "SELECT revoked_at FROM repository_grant "
                        "WHERE security_domain_id=:domain"
                    ),
                    {"domain": domain_id},
                ).scalar_one()
            self.assertIsNotNone(revoked_at)

            token_report = admin.revoke_tokens(principal_id)
            self.assertEqual(token_report["principal_id"], principal_id)
            with self.engine.connect() as connection:
                valid_after = connection.execute(
                    text("SELECT tokens_valid_after FROM principal WHERE id=:id"),
                    {"id": principal_id},
                ).scalar_one()
            self.assertGreater(int(valid_after.timestamp()), 0)
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
