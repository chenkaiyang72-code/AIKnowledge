from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

import jwt
from mcp.server.auth.provider import AccessToken
from sqlalchemy import Engine, create_engine, text


ASYMMETRIC_ALGORITHMS = frozenset(
    {
        "RS256",
        "RS384",
        "RS512",
        "PS256",
        "PS384",
        "PS512",
        "ES256",
        "ES384",
        "ES512",
        "EdDSA",
    }
)


@dataclass(frozen=True)
class OIDCVerifierConfig:
    issuer: str
    audience: str
    jwks_url: str
    required_scopes: frozenset[str] = frozenset({"aiknowledge.read"})
    algorithms: tuple[str, ...] = ("RS256", "PS256", "ES256", "EdDSA")
    allowed_token_types: frozenset[str] = frozenset({"at+jwt", "jwt"})
    leeway_seconds: int = 30
    jwks_timeout_seconds: int = 10

    def __post_init__(self) -> None:
        for label, value in (
            ("issuer", self.issuer),
            ("audience", self.audience),
            ("jwks_url", self.jwks_url),
        ):
            parsed = urlparse(value)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                raise ValueError(f"{label} must be an absolute HTTP(S) URL")
            if parsed.username or parsed.password or parsed.fragment:
                raise ValueError(f"{label} must not contain credentials or a fragment")
            if parsed.scheme != "https" and parsed.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }:
                raise ValueError(f"{label} must use HTTPS outside loopback")
        if not self.required_scopes or any(
            not scope.strip() for scope in self.required_scopes
        ):
            raise ValueError("at least one non-empty scope is required")
        if not self.algorithms or not set(self.algorithms).issubset(
            ASYMMETRIC_ALGORITHMS
        ):
            raise ValueError("only explicit asymmetric JWT algorithms are allowed")
        if not 0 <= self.leeway_seconds <= 300:
            raise ValueError("JWT leeway must be between 0 and 300 seconds")
        if not 1 <= self.jwks_timeout_seconds <= 30:
            raise ValueError("JWKS timeout must be between 1 and 30 seconds")


@dataclass(frozen=True)
class ResolvedPrincipal:
    principal_id: str
    security_domain_id: str
    tokens_valid_after: int


class PrincipalDirectory(Protocol):
    def resolve(self, issuer: str, subject: str) -> ResolvedPrincipal | None: ...


class PostgresPrincipalDirectory:
    """Map verified OIDC identity to an active principal using a narrow DB role."""

    def __init__(self, url: str, engine: Engine | None = None):
        self.engine = engine or create_engine(url, pool_pre_ping=True)
        self._owns_engine = engine is None

    def close(self) -> None:
        if self._owns_engine:
            self.engine.dispose()

    def resolve(self, issuer: str, subject: str) -> ResolvedPrincipal | None:
        with self.engine.connect() as connection:
            connection.execute(text("SET LOCAL ROLE aikb_authenticator"))
            row = connection.execute(
                text(
                    "SELECT p.id AS principal_id,p.security_domain_id,"
                    "EXTRACT(EPOCH FROM p.tokens_valid_after)::bigint AS valid_after "
                    "FROM principal p JOIN security_domain d "
                    "ON d.id=p.security_domain_id "
                    "WHERE p.issuer=:issuer AND p.subject=:subject "
                    "AND p.status='active' AND d.status='active'"
                ),
                {"issuer": issuer, "subject": subject},
            ).mappings().first()
        if row is None:
            return None
        return ResolvedPrincipal(
            principal_id=row["principal_id"],
            security_domain_id=row["security_domain_id"],
            tokens_valid_after=int(row["valid_after"]),
        )


class OIDCTokenVerifier:
    """Fail-closed JWT resource-server verifier for the MCP SDK."""

    def __init__(
        self,
        config: OIDCVerifierConfig,
        directory: PrincipalDirectory,
        key_resolver: Callable[[str], Any] | None = None,
    ):
        self.config = config
        self.directory = directory
        self._jwks_client = None
        if key_resolver is None:
            self._jwks_client = jwt.PyJWKClient(
                config.jwks_url,
                cache_keys=True,
                max_cached_keys=16,
                cache_jwk_set=True,
                lifespan=300,
                timeout=config.jwks_timeout_seconds,
            )
            self._key_resolver = self._resolve_jwks_key
        else:
            self._key_resolver = key_resolver

    def _resolve_jwks_key(self, token: str) -> Any:
        assert self._jwks_client is not None
        return self._jwks_client.get_signing_key_from_jwt(token).key

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 16_384:
            return None
        return await asyncio.to_thread(self._verify_token, token)

    def _verify_token(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            algorithm = header.get("alg")
            token_type = header.get("typ")
            if algorithm not in self.config.algorithms:
                return None
            if not isinstance(token_type, str) or (
                token_type.lower() not in self.config.allowed_token_types
            ):
                return None
            key = self._key_resolver(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=self.config.algorithms,
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat"],
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                },
            )
            subject = claims["sub"]
            issued_at = claims["iat"]
            expires_at = claims["exp"]
            if (
                not isinstance(subject, str)
                or not subject
                or isinstance(issued_at, bool)
                or not isinstance(issued_at, (int, float))
                or isinstance(expires_at, bool)
                or not isinstance(expires_at, (int, float))
            ):
                return None
            scopes = self._scopes(claims.get("scope"))
            if scopes is None or not self.config.required_scopes.issubset(scopes):
                return None
            client_id = claims.get("client_id") or claims.get("azp")
            if not isinstance(client_id, str) or not client_id:
                return None
            principal = self.directory.resolve(self.config.issuer, subject)
            if principal is None or int(issued_at) < principal.tokens_valid_after:
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=sorted(scopes),
                expires_at=int(expires_at),
                resource=self.config.audience,
                subject=subject,
                claims={
                    "iss": self.config.issuer,
                    "aikb_principal_id": principal.principal_id,
                    "aikb_security_domain_id": principal.security_domain_id,
                },
            )
        except Exception:
            # Authentication is an information boundary: malformed tokens, JWKS
            # failures, and directory failures all become the same invalid token.
            return None

    @staticmethod
    def _scopes(value: Any) -> frozenset[str] | None:
        if isinstance(value, str):
            return frozenset(scope for scope in value.split() if scope)
        if isinstance(value, list) and all(
            isinstance(scope, str) and scope for scope in value
        ):
            return frozenset(value)
        return None
