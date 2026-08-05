from __future__ import annotations

import time
import unittest
from dataclasses import replace

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from aikb.oidc import (
    OIDCTokenVerifier,
    OIDCVerifierConfig,
    ResolvedPrincipal,
)


class FakeDirectory:
    def __init__(self, principal: ResolvedPrincipal | None):
        self.principal = principal

    def resolve(self, issuer: str, subject: str) -> ResolvedPrincipal | None:
        if issuer != "https://issuer.example" or subject != "alice":
            return None
        return self.principal


class OIDCTokenVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()
        self.now = int(time.time())
        self.config = OIDCVerifierConfig(
            issuer="https://issuer.example",
            audience="https://kb.example/mcp/read",
            jwks_url="https://issuer.example/.well-known/jwks.json",
            required_scopes=frozenset({"aiknowledge.read"}),
            algorithms=("RS256",),
            leeway_seconds=0,
        )
        self.principal = ResolvedPrincipal(
            principal_id="principal-alice",
            security_domain_id="domain-kernel",
            tokens_valid_after=self.now - 60,
        )

    def token(self, **overrides: object) -> str:
        claims: dict[str, object] = {
            "iss": self.config.issuer,
            "sub": "alice",
            "aud": self.config.audience,
            "client_id": "cursor",
            "scope": "aiknowledge.read profile",
            "iat": self.now,
            "nbf": self.now - 1,
            "exp": self.now + 300,
        }
        claims.update(overrides)
        return jwt.encode(
            claims,
            self.private_key,
            algorithm="RS256",
            headers={"typ": "at+jwt", "kid": "test-key"},
        )

    def verifier(
        self,
        principal: ResolvedPrincipal | None = None,
        config: OIDCVerifierConfig | None = None,
    ) -> OIDCTokenVerifier:
        return OIDCTokenVerifier(
            config or self.config,
            FakeDirectory(self.principal if principal is None else principal),
            key_resolver=lambda token: self.public_key,
        )

    async def test_valid_token_maps_only_server_resolved_principal_claims(self) -> None:
        access = await self.verifier().verify_token(self.token())

        self.assertIsNotNone(access)
        assert access is not None
        self.assertEqual(access.client_id, "cursor")
        self.assertEqual(access.resource, self.config.audience)
        self.assertEqual(access.subject, "alice")
        self.assertEqual(
            access.claims,
            {
                "iss": self.config.issuer,
                "aikb_principal_id": "principal-alice",
                "aikb_security_domain_id": "domain-kernel",
            },
        )

    async def test_wrong_audience_issuer_scope_expiry_and_revocation_fail_closed(self) -> None:
        cases = {
            "audience": self.token(aud="https://other.example/mcp"),
            "issuer": self.token(iss="https://other-issuer.example"),
            "scope": self.token(scope="profile"),
            "expired": self.token(exp=self.now - 1),
            "not_before": self.token(nbf=self.now + 60),
            "missing_iat": self.token(iat=None),
        }
        for name, token in cases.items():
            with self.subTest(name=name):
                self.assertIsNone(await self.verifier().verify_token(token))

        revoked = replace(self.principal, tokens_valid_after=self.now + 1)
        self.assertIsNone(await self.verifier(revoked).verify_token(self.token()))

    async def test_disallowed_algorithm_token_type_and_unknown_principal_fail_closed(self) -> None:
        hs_token = jwt.encode(
            {
                "iss": self.config.issuer,
                "sub": "alice",
                "aud": self.config.audience,
                "client_id": "cursor",
                "scope": "aiknowledge.read",
                "iat": self.now,
                "exp": self.now + 300,
            },
            "not-a-public-key-but-long-enough-for-hmac",
            algorithm="HS256",
            headers={"typ": "at+jwt"},
        )
        wrong_type = jwt.encode(
            jwt.decode(
                self.token(),
                options={"verify_signature": False},
                algorithms=["RS256"],
            ),
            self.private_key,
            algorithm="RS256",
            headers={"typ": "id+jwt"},
        )
        unknown = OIDCTokenVerifier(
            self.config,
            FakeDirectory(None),
            key_resolver=lambda token: self.public_key,
        )

        self.assertIsNone(await self.verifier().verify_token(hs_token))
        self.assertIsNone(await self.verifier().verify_token(wrong_type))
        self.assertIsNone(await unknown.verify_token(self.token()))

    def test_config_rejects_symmetric_algorithms_and_non_https_remote_urls(self) -> None:
        with self.assertRaises(ValueError):
            replace(self.config, algorithms=("HS256",))
        with self.assertRaises(ValueError):
            replace(self.config, jwks_url="http://issuer.example/jwks.json")


if __name__ == "__main__":
    unittest.main()
