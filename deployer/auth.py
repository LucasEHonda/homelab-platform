from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import jwt

if TYPE_CHECKING:
    from deployer.config import AppConfig

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URL = "https://token.actions.githubusercontent.com/.well-known/jwks"
AUDIENCE = "homelab-deployer"
MAIN_REF = "refs/heads/main"
TAG_REF = re.compile(r"^refs/tags/v\d+\.\d+\.\d+$")
REQUIRED_CLAIMS = [
    "exp",
    "iat",
    "iss",
    "aud",
    "jti",
    "repository",
    "repository_id",
    "ref",
    "job_workflow_ref",
]


class AuthError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CallerIdentity:
    repository: str
    repository_id: str
    ref: str
    job_workflow_ref: str
    jti: str


def _fetch_jwks_http(url: str) -> dict:
    response = httpx.get(url, timeout=10)
    response.raise_for_status()
    return response.json()


class GitHubOidcVerifier:
    def __init__(
        self,
        *,
        jwks_url: str = GITHUB_JWKS_URL,
        issuer: str = GITHUB_ISSUER,
        audience: str = AUDIENCE,
        fetch_jwks: Callable[[str], dict] | None = None,
        clock: Callable[[], float] = time.time,
        jwks_ttl_seconds: int = 3600,
        leeway_seconds: int = 60,
    ) -> None:
        self._jwks_url = jwks_url
        self._issuer = issuer
        self._audience = audience
        self._fetch_jwks = fetch_jwks or _fetch_jwks_http
        self._clock = clock
        self._jwks_ttl_seconds = jwks_ttl_seconds
        self._leeway_seconds = leeway_seconds
        self._jwks: dict | None = None
        self._jwks_fetched_at = 0.0
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def _refresh(self) -> None:
        try:
            self._jwks = self._fetch_jwks(self._jwks_url)
        except Exception as exc:
            raise AuthError(503, "cannot fetch signing keys") from exc
        self._jwks_fetched_at = self._clock()

    def _find(self, kid: str) -> dict | None:
        for key in self._jwks["keys"]:
            if key["kid"] == kid:
                return key
        return None

    def _signing_key(self, kid: str):
        refreshed = False
        if (
            self._jwks is None
            or self._clock() - self._jwks_fetched_at >= self._jwks_ttl_seconds
        ):
            self._refresh()
            refreshed = True
        key_dict = self._find(kid)
        if key_dict is None and not refreshed:
            self._refresh()
            key_dict = self._find(kid)
        if key_dict is None:
            raise AuthError(401, "unknown signing key")
        return jwt.PyJWK(key_dict).key

    def verify(self, token: str) -> CallerIdentity:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise AuthError(401, "malformed token") from exc
        if header.get("alg") != "RS256":
            raise AuthError(401, "unsupported algorithm")
        kid = header.get("kid")
        if not kid:
            raise AuthError(401, "missing key id")
        key = self._signing_key(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway_seconds,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(401, f"invalid token: {exc}") from exc
        with self._lock:
            now = self._clock()
            expired = [
                jti
                for jti, exp in self._seen.items()
                if exp + self._leeway_seconds < now
            ]
            for jti in expired:
                del self._seen[jti]
            if claims["jti"] in self._seen:
                raise AuthError(401, "token already used")
            self._seen[claims["jti"]] = float(claims["exp"])
        return CallerIdentity(
            repository=str(claims["repository"]),
            repository_id=str(claims["repository_id"]),
            ref=str(claims["ref"]),
            job_workflow_ref=str(claims["job_workflow_ref"]),
            jti=str(claims["jti"]),
        )


def authorize_deploy(
    identity: CallerIdentity,
    app: "AppConfig",
    allowed_workflow_refs: Sequence[str],
) -> None:
    if (
        identity.repository != app.repository
        or identity.repository_id != app.repository_id
    ):
        raise AuthError(403, f"repository not allowed for app {app.name}")
    if identity.ref != MAIN_REF and not TAG_REF.fullmatch(identity.ref):
        raise AuthError(403, "ref not allowed")
    if not any(
        identity.job_workflow_ref.startswith(prefix) for prefix in allowed_workflow_refs
    ):
        raise AuthError(403, "workflow not allowed")


def require_admin(login: str | None, admins: Collection[str]) -> str:
    if not login:
        raise AuthError(403, "admin identity required")
    if login not in admins:
        raise AuthError(403, "not an admin")
    return login
