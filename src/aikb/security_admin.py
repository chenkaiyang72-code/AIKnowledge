from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Engine, text

from aikb.ingestion import stable_id


IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SecurityDomainSpec(_ManifestModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    status: Literal["active", "suspended"] = "active"
    data_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("data_policy")
    @classmethod
    def validate_data_policy_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(encoded) > 32_768:
            raise ValueError("domain data_policy must not exceed 32 KiB")
        return value


class PrincipalSpec(_ManifestModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    issuer: str = Field(min_length=1, max_length=2_048)
    subject: str = Field(min_length=1, max_length=2_048)
    display_name: str = Field(default="", max_length=256)
    status: Literal["active", "suspended", "revoked"] = "active"

    @field_validator("issuer")
    @classmethod
    def validate_issuer(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("principal issuer must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "principal issuer must not contain credentials, query, or fragment"
            )
        if parsed.scheme != "https" and parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("principal issuer must use HTTPS outside loopback")
        return value


class TeamSpec(_ManifestModel):
    id: str = Field(pattern=IDENTIFIER_PATTERN)
    name: str = Field(min_length=1, max_length=256)
    status: Literal["active", "suspended"] = "active"


class TeamMembershipSpec(_ManifestModel):
    team_id: str = Field(pattern=IDENTIFIER_PATTERN)
    principal_id: str = Field(pattern=IDENTIFIER_PATTERN)
    role: Literal["member", "maintainer"] = "member"
    active: bool = True


class RepositoryGrantSpec(_ManifestModel):
    repository_id: str = Field(pattern=IDENTIFIER_PATTERN)
    principal_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    team_id: str | None = Field(default=None, pattern=IDENTIFIER_PATTERN)
    permission: Literal["read", "write", "admin"] = "read"
    expires_at: datetime | None = None
    active: bool = True

    @model_validator(mode="after")
    def validate_grantee(self) -> RepositoryGrantSpec:
        if (self.principal_id is None) == (self.team_id is None):
            raise ValueError("repository grant requires exactly one principal_id or team_id")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("repository grant expires_at must include a timezone")
        return self


class SecurityManifest(_ManifestModel):
    schema_version: Literal[1]
    domain: SecurityDomainSpec
    principals: tuple[PrincipalSpec, ...] = ()
    teams: tuple[TeamSpec, ...] = ()
    memberships: tuple[TeamMembershipSpec, ...] = ()
    repository_grants: tuple[RepositoryGrantSpec, ...] = ()

    @model_validator(mode="after")
    def reject_duplicate_entries(self) -> SecurityManifest:
        self._unique("principal id", (item.id for item in self.principals))
        self._unique(
            "principal identity",
            ((item.issuer, item.subject) for item in self.principals),
        )
        self._unique("team id", (item.id for item in self.teams))
        self._unique("team name", (item.name for item in self.teams))
        self._unique(
            "team membership",
            ((item.team_id, item.principal_id) for item in self.memberships),
        )
        self._unique(
            "repository grant",
            (
                (item.repository_id, item.principal_id, item.team_id)
                for item in self.repository_grants
            ),
        )
        return self

    @staticmethod
    def _unique(label: str, values: Any) -> None:
        seen: set[Any] = set()
        for value in values:
            if value in seen:
                raise ValueError(f"duplicate {label}: {value!r}")
            seen.add(value)

    def digest(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_security_manifest(path: Path) -> SecurityManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"invalid security manifest JSON at line {error.lineno}, column {error.colno}"
        ) from error
    return SecurityManifest.model_validate(payload)


@dataclass(frozen=True)
class SecurityApplyReport:
    schema_version: int
    manifest_digest: str
    security_domain_id: str
    principal_count: int
    team_count: int
    membership_count: int
    repository_grant_count: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "manifest_digest": self.manifest_digest,
            "security_domain_id": self.security_domain_id,
            "principal_count": self.principal_count,
            "team_count": self.team_count,
            "membership_count": self.membership_count,
            "repository_grant_count": self.repository_grant_count,
            "dry_run": self.dry_run,
        }


class PostgresSecurityAdmin:
    """Apply an additive security manifest through an owner/admin connection."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def apply(
        self,
        manifest: SecurityManifest,
        *,
        dry_run: bool = False,
    ) -> SecurityApplyReport:
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                self._require_schema_v5(connection)
                self._upsert_domain(connection, manifest.domain)
                for principal in manifest.principals:
                    self._upsert_principal(connection, manifest.domain.id, principal)
                for team in manifest.teams:
                    self._upsert_team(connection, manifest.domain.id, team)
                for membership in manifest.memberships:
                    self._upsert_membership(
                        connection,
                        manifest.domain.id,
                        membership,
                    )
                for grant in manifest.repository_grants:
                    self._upsert_repository_grant(
                        connection,
                        manifest.domain.id,
                        grant,
                    )
                if dry_run:
                    transaction.rollback()
                else:
                    transaction.commit()
            except Exception:
                if transaction.is_active:
                    transaction.rollback()
                raise
        return SecurityApplyReport(
            schema_version=manifest.schema_version,
            manifest_digest=manifest.digest(),
            security_domain_id=manifest.domain.id,
            principal_count=len(manifest.principals),
            team_count=len(manifest.teams),
            membership_count=len(manifest.memberships),
            repository_grant_count=len(manifest.repository_grants),
            dry_run=dry_run,
        )

    def revoke_tokens(self, principal_id: str) -> dict[str, Any]:
        if not principal_id or len(principal_id) > 64:
            raise ValueError("principal ID is invalid")
        with self.engine.begin() as connection:
            self._require_schema_v5(connection)
            row = connection.execute(
                text(
                    "UPDATE principal SET tokens_valid_after="
                    "GREATEST(tokens_valid_after,now()) WHERE id=:principal "
                    "RETURNING id,security_domain_id,tokens_valid_after"
                ),
                {"principal": principal_id},
            ).mappings().first()
        if row is None:
            raise ValueError("principal does not exist")
        return {
            "principal_id": row["id"],
            "security_domain_id": row["security_domain_id"],
            "tokens_valid_after": row["tokens_valid_after"].isoformat(),
        }

    @staticmethod
    def _require_schema_v5(connection: Any) -> None:
        version = connection.execute(
            text(
                "SELECT value FROM schema_metadata "
                "WHERE key='postgres_schema_version'"
            )
        ).scalar_one_or_none()
        if version != "5":
            raise ValueError("security admin requires PostgreSQL schema v5")

    @staticmethod
    def _upsert_domain(connection: Any, domain: SecurityDomainSpec) -> None:
        connection.execute(
            text(
                "INSERT INTO security_domain(id,name,status,data_policy) "
                "VALUES (:id,:name,:status,CAST(:policy AS jsonb)) "
                "ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,"
                "status=EXCLUDED.status,data_policy=EXCLUDED.data_policy"
            ),
            {
                "id": domain.id,
                "name": domain.name,
                "status": domain.status,
                "policy": json.dumps(domain.data_policy, sort_keys=True),
            },
        )

    @staticmethod
    def _upsert_principal(
        connection: Any,
        domain_id: str,
        principal: PrincipalSpec,
    ) -> None:
        existing = connection.execute(
            text(
                "SELECT security_domain_id,issuer,subject FROM principal WHERE id=:id"
            ),
            {"id": principal.id},
        ).mappings().first()
        if existing is not None and (
            existing["security_domain_id"] != domain_id
            or existing["issuer"] != principal.issuer
            or existing["subject"] != principal.subject
        ):
            raise ValueError(
                f"principal {principal.id!r} cannot change domain or OIDC identity"
            )
        connection.execute(
            text(
                "INSERT INTO principal(id,security_domain_id,issuer,subject,"
                "display_name,status) VALUES (:id,:domain,:issuer,:subject,:name,:status) "
                "ON CONFLICT (id) DO UPDATE SET display_name=EXCLUDED.display_name,"
                "status=EXCLUDED.status"
            ),
            {
                "id": principal.id,
                "domain": domain_id,
                "issuer": principal.issuer,
                "subject": principal.subject,
                "name": principal.display_name,
                "status": principal.status,
            },
        )

    @staticmethod
    def _upsert_team(connection: Any, domain_id: str, team: TeamSpec) -> None:
        row = connection.execute(
            text(
                "INSERT INTO security_team(id,security_domain_id,name,status) "
                "VALUES (:id,:domain,:name,:status) ON CONFLICT (id) DO UPDATE "
                "SET name=EXCLUDED.name,status=EXCLUDED.status "
                "WHERE security_team.security_domain_id=EXCLUDED.security_domain_id "
                "RETURNING id"
            ),
            {
                "id": team.id,
                "domain": domain_id,
                "name": team.name,
                "status": team.status,
            },
        ).first()
        if row is None:
            raise ValueError(f"team {team.id!r} cannot move between security domains")

    @classmethod
    def _upsert_membership(
        cls,
        connection: Any,
        domain_id: str,
        membership: TeamMembershipSpec,
    ) -> None:
        cls._require_domain_row(
            connection,
            "security_team",
            membership.team_id,
            domain_id,
        )
        cls._require_domain_row(
            connection,
            "principal",
            membership.principal_id,
            domain_id,
        )
        if not membership.active:
            connection.execute(
                text(
                    "DELETE FROM security_team_member "
                    "WHERE team_id=:team AND principal_id=:principal"
                ),
                {
                    "team": membership.team_id,
                    "principal": membership.principal_id,
                },
            )
            return
        connection.execute(
            text(
                "INSERT INTO security_team_member(team_id,principal_id,role) "
                "VALUES (:team,:principal,:role) ON CONFLICT (team_id,principal_id) "
                "DO UPDATE SET role=EXCLUDED.role"
            ),
            {
                "team": membership.team_id,
                "principal": membership.principal_id,
                "role": membership.role,
            },
        )

    @classmethod
    def _upsert_repository_grant(
        cls,
        connection: Any,
        domain_id: str,
        grant: RepositoryGrantSpec,
    ) -> None:
        repository_exists = connection.execute(
            text("SELECT 1 FROM repository WHERE id=:id"),
            {"id": grant.repository_id},
        ).first()
        if repository_exists is None:
            raise ValueError(f"repository {grant.repository_id!r} does not exist")
        grantee_type = "principal" if grant.principal_id is not None else "team"
        grantee_id = grant.principal_id or grant.team_id
        assert grantee_id is not None
        cls._require_domain_row(
            connection,
            "principal" if grant.principal_id is not None else "security_team",
            grantee_id,
            domain_id,
        )
        grant_id = stable_id(
            "repository_grant",
            domain_id,
            grant.repository_id,
            grantee_type,
            grantee_id,
        )
        parameters = {
            "id": grant_id,
            "domain": domain_id,
            "repository": grant.repository_id,
            "principal": grant.principal_id,
            "team": grant.team_id,
            "permission": grant.permission,
            "expires_at": grant.expires_at,
            "active": grant.active,
        }
        if grant.principal_id is not None:
            conflict = (
                "(security_domain_id,repository_id,principal_id) "
                "WHERE principal_id IS NOT NULL"
            )
        else:
            conflict = (
                "(security_domain_id,repository_id,team_id) WHERE team_id IS NOT NULL"
            )
        connection.execute(
            text(
                "INSERT INTO repository_grant(id,security_domain_id,repository_id,"
                "principal_id,team_id,permission,expires_at,revoked_at) VALUES "
                "(:id,:domain,:repository,:principal,:team,:permission,:expires_at,"
                "CASE WHEN :active THEN NULL ELSE now() END) ON CONFLICT "
                f"{conflict} DO UPDATE SET permission=EXCLUDED.permission,"
                "expires_at=EXCLUDED.expires_at,revoked_at=CASE WHEN :active THEN NULL "
                "ELSE COALESCE(repository_grant.revoked_at,now()) END"
            ),
            parameters,
        )

    @staticmethod
    def _require_domain_row(
        connection: Any,
        table: str,
        row_id: str,
        domain_id: str,
    ) -> None:
        if table not in {"principal", "security_team"}:
            raise ValueError("unsupported security reference")
        row = connection.execute(
            text(
                f"SELECT 1 FROM {table} "
                "WHERE id=:id AND security_domain_id=:domain"
            ),
            {"id": row_id, "domain": domain_id},
        ).first()
        if row is None:
            raise ValueError(
                f"{table} {row_id!r} does not exist in domain {domain_id!r}"
            )
