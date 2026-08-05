from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, text

from aikb.security_admin import IDENTIFIER_PATTERN


GITHUB_API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 10 * 1024 * 1024


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects()).open


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GitHubRepositorySpec(_StrictModel):
    owner: str = Field(min_length=1, max_length=100)
    repository: str = Field(min_length=1, max_length=100)
    api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def validate_api_base_url(self) -> GitHubRepositorySpec:
        parsed = urlparse(self.api_base_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("GitHub API base URL must be an absolute HTTPS URL")
        return self

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repository}"


class GitHubPrincipalBinding(_StrictModel):
    github_user_id: int = Field(gt=0)
    principal_id: str = Field(pattern=IDENTIFIER_PATTERN)


class GitHubACLPlanConfig(_StrictModel):
    schema_version: Literal[1]
    security_domain_id: str = Field(pattern=IDENTIFIER_PATTERN)
    repository_id: str = Field(pattern=IDENTIFIER_PATTERN)
    github: GitHubRepositorySpec
    bindings: tuple[GitHubPrincipalBinding, ...]

    @model_validator(mode="after")
    def reject_duplicate_bindings(self) -> GitHubACLPlanConfig:
        user_ids = [binding.github_user_id for binding in self.bindings]
        principal_ids = [binding.principal_id for binding in self.bindings]
        if len(set(user_ids)) != len(user_ids):
            raise ValueError("duplicate GitHub user ID binding")
        if len(set(principal_ids)) != len(principal_ids):
            raise ValueError("one principal cannot bind to multiple GitHub users")
        return self


class GitHubCollaborator(_StrictModel):
    user_id: int = Field(gt=0)
    login: str = Field(min_length=1, max_length=100)
    permission: Literal["read", "write", "admin"]


class GitHubACLSnapshot(_StrictModel):
    schema_version: Literal[1] = 1
    repository: str
    captured_at: datetime
    collaborators: tuple[GitHubCollaborator, ...]

    @model_validator(mode="after")
    def validate_snapshot(self) -> GitHubACLSnapshot:
        if self.captured_at.tzinfo is None:
            raise ValueError("GitHub ACL snapshot time must include a timezone")
        ids = [item.user_id for item in self.collaborators]
        if len(set(ids)) != len(ids):
            raise ValueError("GitHub ACL snapshot contains duplicate user IDs")
        return self

    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def load_github_acl_config(path: Any) -> GitHubACLPlanConfig:
    return GitHubACLPlanConfig.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


def load_github_acl_snapshot(path: Any) -> GitHubACLSnapshot:
    return GitHubACLSnapshot.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )


class GitHubCollaboratorClient:
    """Read effective repository collaborators without mutating GitHub."""

    def __init__(
        self,
        token: str,
        *,
        opener: Callable[..., Any] = _NO_REDIRECT_OPENER,
        timeout_seconds: int = 15,
        max_pages: int = 100,
    ):
        if not token or len(token) > 8_192:
            raise ValueError("GitHub token is missing or invalid")
        if not 1 <= timeout_seconds <= 60:
            raise ValueError("GitHub timeout must be between 1 and 60 seconds")
        if not 1 <= max_pages <= 100:
            raise ValueError("GitHub max_pages must be between 1 and 100")
        self._token = token
        self._opener = opener
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages

    def fetch(self, repository: GitHubRepositorySpec) -> GitHubACLSnapshot:
        endpoint_path = (
            f"/repos/{quote(repository.owner, safe='')}/"
            f"{quote(repository.repository, safe='')}/collaborators"
        )
        base = repository.api_base_url.rstrip("/")
        expected_path = urlparse(base).path.rstrip("/") + endpoint_path
        next_url = f"{base}{endpoint_path}?{urlencode({'affiliation': 'all', 'per_page': 100})}"
        expected_origin = self._origin(next_url)
        visited: set[str] = set()
        collaborators: dict[int, GitHubCollaborator] = {}
        for _ in range(self.max_pages):
            if next_url in visited:
                raise RuntimeError("GitHub pagination loop detected")
            visited.add(next_url)
            payload, link = self._get_page(next_url)
            if not isinstance(payload, list):
                raise RuntimeError("GitHub collaborator response is not a list")
            for raw in payload:
                collaborator = self._parse_collaborator(raw)
                previous = collaborators.get(collaborator.user_id)
                if previous is not None and previous != collaborator:
                    raise RuntimeError(
                        "GitHub returned conflicting rows for one user ID"
                    )
                collaborators[collaborator.user_id] = collaborator
            next_url = self._next_link(link)
            if next_url is None:
                break
            self._validate_next_url(
                next_url,
                expected_origin=expected_origin,
                expected_path=expected_path,
            )
        else:
            raise RuntimeError("GitHub collaborator pagination exceeded max_pages")
        return GitHubACLSnapshot(
            repository=repository.full_name,
            captured_at=datetime.now(timezone.utc),
            collaborators=tuple(
                sorted(collaborators.values(), key=lambda item: item.user_id)
            ),
        )

    def _get_page(self, url: str) -> tuple[Any, str | None]:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "User-Agent": "AIKnowledge-GitHub-ACL-Reader/0.1",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("GitHub collaborator response is too large")
                link = response.headers.get("Link")
        except HTTPError as error:
            raise RuntimeError(
                f"GitHub collaborator request failed with HTTP {error.code}"
            ) from None
        except (TimeoutError, URLError) as error:
            raise RuntimeError("GitHub collaborator request failed") from error
        try:
            return json.loads(content.decode("utf-8")), link
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("GitHub collaborator response is invalid JSON") from error

    @staticmethod
    def _parse_collaborator(raw: Any) -> GitHubCollaborator:
        if not isinstance(raw, dict):
            raise RuntimeError("GitHub collaborator row is invalid")
        permissions = raw.get("permissions")
        if not isinstance(permissions, dict):
            raise RuntimeError("GitHub collaborator permissions are missing")
        if permissions.get("admin") is True:
            permission = "admin"
        elif permissions.get("push") is True or permissions.get("maintain") is True:
            permission = "write"
        elif permissions.get("pull") is True or permissions.get("triage") is True:
            permission = "read"
        else:
            raise RuntimeError("GitHub collaborator has no supported base permission")
        try:
            return GitHubCollaborator(
                user_id=raw["id"],
                login=raw["login"],
                permission=permission,
            )
        except (KeyError, ValueError, TypeError) as error:
            raise RuntimeError("GitHub collaborator identity is invalid") from error

    @staticmethod
    def _origin(url: str) -> tuple[str, str]:
        parsed = urlparse(url)
        return parsed.scheme.lower(), parsed.netloc.lower()

    @classmethod
    def _validate_next_url(
        cls,
        url: str,
        *,
        expected_origin: tuple[str, str],
        expected_path: str,
    ) -> None:
        parsed = urlparse(url)
        if cls._origin(url) != expected_origin or parsed.path != expected_path:
            raise RuntimeError("GitHub pagination URL escaped the configured endpoint")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if not set(query).issubset({"affiliation", "per_page", "page"}):
            raise RuntimeError("GitHub pagination URL contains unexpected parameters")

    @staticmethod
    def _next_link(link: str | None) -> str | None:
        if not link:
            return None
        for item in link.split(","):
            pieces = [piece.strip() for piece in item.split(";")]
            if len(pieces) >= 2 and any(
                piece == 'rel="next"' for piece in pieces[1:]
            ):
                target = pieces[0]
                if not target.startswith("<") or not target.endswith(">"):
                    raise RuntimeError("GitHub pagination Link header is invalid")
                return target[1:-1]
        return None


@dataclass(frozen=True)
class GitHubACLPlan:
    security_domain_id: str
    repository_id: str
    github_repository: str
    snapshot_digest: str
    captured_at: str
    activate_or_update: tuple[dict[str, Any], ...]
    revoke_candidates: tuple[dict[str, Any], ...]
    unmatched_collaborators: tuple[dict[str, Any], ...]
    stale_bindings: tuple[dict[str, Any], ...]
    unchanged_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "security_domain_id": self.security_domain_id,
            "repository_id": self.repository_id,
            "github_repository": self.github_repository,
            "snapshot_digest": self.snapshot_digest,
            "captured_at": self.captured_at,
            "mode": "read_only_review",
            "activate_or_update": list(self.activate_or_update),
            "revoke_candidates": list(self.revoke_candidates),
            "unmatched_collaborators": list(self.unmatched_collaborators),
            "stale_bindings": list(self.stale_bindings),
            "unchanged_count": self.unchanged_count,
        }


class PostgresGitHubACLPlanner:
    """Compare effective GitHub access with direct AIKnowledge grants; never writes."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def plan(
        self,
        config: GitHubACLPlanConfig,
        snapshot: GitHubACLSnapshot,
    ) -> GitHubACLPlan:
        if snapshot.repository.lower() != config.github.full_name.lower():
            raise ValueError("GitHub ACL snapshot repository does not match config")
        bindings = {
            binding.github_user_id: binding.principal_id
            for binding in config.bindings
        }
        with self.engine.connect() as connection:
            version = connection.execute(
                text(
                    "SELECT value FROM schema_metadata "
                    "WHERE key='postgres_schema_version'"
                )
            ).scalar_one_or_none()
            if version != "5":
                raise ValueError("GitHub ACL planning requires PostgreSQL schema v5")
            domain = connection.execute(
                text("SELECT id FROM security_domain WHERE id=:id"),
                {"id": config.security_domain_id},
            ).first()
            if domain is None:
                raise ValueError("configured security domain does not exist")
            repository = connection.execute(
                text("SELECT id FROM repository WHERE id=:id"),
                {"id": config.repository_id},
            ).first()
            if repository is None:
                raise ValueError("configured repository does not exist")
            known_principals = set(
                connection.execute(
                    text(
                        "SELECT id FROM principal "
                        "WHERE security_domain_id=:domain"
                    ),
                    {"domain": config.security_domain_id},
                ).scalars()
            )
            missing = sorted(set(bindings.values()) - known_principals)
            if missing:
                raise ValueError(
                    "GitHub bindings reference principals outside the security domain: "
                    + ", ".join(missing)
                )
            current = {
                row["principal_id"]: row
                for row in connection.execute(
                    text(
                        "SELECT id,principal_id,permission,revoked_at,expires_at,"
                        "(revoked_at IS NULL AND "
                        "(expires_at IS NULL OR expires_at>now())) AS active "
                        "FROM repository_grant WHERE security_domain_id=:domain "
                        "AND repository_id=:repository AND principal_id IS NOT NULL"
                    ),
                    {
                        "domain": config.security_domain_id,
                        "repository": config.repository_id,
                    },
                ).mappings()
            }

        desired: dict[str, GitHubCollaborator] = {}
        unmatched: list[dict[str, Any]] = []
        present_user_ids: set[int] = set()
        for collaborator in snapshot.collaborators:
            present_user_ids.add(collaborator.user_id)
            principal_id = bindings.get(collaborator.user_id)
            if principal_id is None:
                unmatched.append(
                    {
                        "github_user_id": collaborator.user_id,
                        "login": collaborator.login,
                        "permission": collaborator.permission,
                    }
                )
            else:
                desired[principal_id] = collaborator

        activate: list[dict[str, Any]] = []
        unchanged = 0
        for principal_id, collaborator in sorted(desired.items()):
            row = current.get(principal_id)
            if row is None:
                reason = "missing_direct_grant"
            elif not row["active"]:
                reason = "inactive_direct_grant"
            elif row["permission"] != collaborator.permission:
                reason = "permission_change"
            else:
                unchanged += 1
                continue
            activate.append(
                {
                    "principal_id": principal_id,
                    "permission": collaborator.permission,
                    "reason": reason,
                }
            )

        revoke: list[dict[str, Any]] = []
        for principal_id, row in sorted(current.items()):
            if row["active"] and principal_id not in desired:
                revoke.append(
                    {
                        "grant_id": row["id"],
                        "principal_id": principal_id,
                        "current_permission": row["permission"],
                        "reason": "not_present_in_bound_github_collaborators",
                        "requires_review": True,
                    }
                )
        stale = [
            {
                "github_user_id": binding.github_user_id,
                "principal_id": binding.principal_id,
            }
            for binding in config.bindings
            if binding.github_user_id not in present_user_ids
        ]
        return GitHubACLPlan(
            security_domain_id=config.security_domain_id,
            repository_id=config.repository_id,
            github_repository=config.github.full_name,
            snapshot_digest=snapshot.digest(),
            captured_at=snapshot.captured_at.isoformat(),
            activate_or_update=tuple(activate),
            revoke_candidates=tuple(revoke),
            unmatched_collaborators=tuple(
                sorted(unmatched, key=lambda item: item["github_user_id"])
            ),
            stale_bindings=tuple(
                sorted(stale, key=lambda item: item["github_user_id"])
            ),
            unchanged_count=unchanged,
        )
