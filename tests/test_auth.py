import json
import time
import uuid
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from deployer.auth import (
    AUDIENCE,
    GITHUB_ISSUER,
    AuthError,
    CallerIdentity,
    GitHubOidcVerifier,
    authorize_deploy,
    require_admin,
)

WORKFLOW = "LucasEHonda/homelab-platform/.github/workflows/deploy.yml@abc123"
PREFIXES = ["LucasEHonda/homelab-platform/.github/workflows/deploy.yml@"]


@dataclass(frozen=True)
class FakeApp:
    name: str
    repository: str
    repository_id: str


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwk(private_key):
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    data.update({"kid": "k1", "alg": "RS256", "use": "sig"})
    return data


class Fetch:
    def __init__(self, jwk):
        self.jwk = jwk
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        return {"keys": [self.jwk]}


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


@pytest.fixture
def fetch(jwk):
    return Fetch(jwk)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def verifier(fetch, clock):
    return GitHubOidcVerifier(fetch_jwks=fetch, clock=clock)


@pytest.fixture
def make_token(private_key):
    def _token(key=None, kid="k1", **overrides):
        now = int(time.time())
        claims = {
            "iss": GITHUB_ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "nbf": now,
            "exp": now + 300,
            "jti": uuid.uuid4().hex,
            "repository": "org/app",
            "repository_id": "42",
            "ref": "refs/heads/main",
            "job_workflow_ref": WORKFLOW,
        }
        for name, value in overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        return jwt.encode(
            claims, key or private_key, algorithm="RS256", headers={"kid": kid}
        )

    return _token


def test_valid_token_returns_identity(verifier, fetch, make_token):
    token = make_token(jti="j1")
    identity = verifier.verify(token)
    assert identity == CallerIdentity(
        repository="org/app",
        repository_id="42",
        ref="refs/heads/main",
        job_workflow_ref=WORKFLOW,
        jti="j1",
    )
    assert fetch.calls == 1


def test_jwks_is_cached_within_ttl(verifier, fetch, make_token):
    verifier.verify(make_token())
    verifier.verify(make_token())
    assert fetch.calls == 1


def test_jwks_refetched_after_ttl(verifier, fetch, clock, make_token):
    verifier.verify(make_token())
    clock.now += 3600
    verifier.verify(make_token())
    assert fetch.calls == 2


def test_token_signed_by_other_key_is_rejected(verifier, make_token):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(AuthError, match="invalid token") as exc:
        verifier.verify(make_token(key=other))
    assert exc.value.status_code == 401


def test_wrong_audience_is_rejected(verifier, make_token):
    with pytest.raises(AuthError, match="invalid token") as exc:
        verifier.verify(make_token(aud="other"))
    assert exc.value.status_code == 401


def test_wrong_issuer_is_rejected(verifier, make_token):
    with pytest.raises(AuthError, match="invalid token") as exc:
        verifier.verify(make_token(iss="https://evil.example.com"))
    assert exc.value.status_code == 401


def test_expired_token_is_rejected(verifier, make_token):
    now = int(time.time())
    token = make_token(exp=now - 3600, iat=now - 4000, nbf=now - 4000)
    with pytest.raises(AuthError, match="invalid token") as exc:
        verifier.verify(token)
    assert exc.value.status_code == 401


def test_missing_required_claim_is_rejected(verifier, make_token):
    with pytest.raises(AuthError, match="invalid token") as exc:
        verifier.verify(make_token(repository_id=None))
    assert exc.value.status_code == 401


def test_replayed_token_is_rejected(verifier, make_token):
    token = make_token()
    verifier.verify(token)
    with pytest.raises(AuthError, match="token already used") as exc:
        verifier.verify(token)
    assert exc.value.status_code == 401


def test_unknown_kid_refreshes_once_then_fails(verifier, fetch, make_token):
    verifier.verify(make_token())
    with pytest.raises(AuthError, match="unknown signing key") as exc:
        verifier.verify(make_token(kid="k2"))
    assert exc.value.status_code == 401
    assert fetch.calls == 2


def test_fetch_failure_returns_503(clock, make_token):
    def failing(url):
        raise RuntimeError("down")

    verifier = GitHubOidcVerifier(fetch_jwks=failing, clock=clock)
    with pytest.raises(AuthError, match="cannot fetch signing keys") as exc:
        verifier.verify(make_token())
    assert exc.value.status_code == 503


def test_non_rs256_algorithm_is_rejected(verifier):
    token = jwt.encode(
        {"sub": "x"}, "s" * 32, algorithm="HS256", headers={"kid": "k1"}
    )
    with pytest.raises(AuthError, match="unsupported algorithm") as exc:
        verifier.verify(token)
    assert exc.value.status_code == 401


def test_garbage_token_is_malformed(verifier):
    with pytest.raises(AuthError, match="malformed token") as exc:
        verifier.verify("abc")
    assert exc.value.status_code == 401


def _identity(**overrides):
    values = {
        "repository": "org/app",
        "repository_id": "42",
        "ref": "refs/heads/main",
        "job_workflow_ref": WORKFLOW,
        "jti": "j",
    }
    values.update(overrides)
    return CallerIdentity(**values)


APP = FakeApp(name="app", repository="org/app", repository_id="42")


def test_authorize_deploy_passes_for_matching_app():
    authorize_deploy(_identity(), APP, PREFIXES)


def test_authorize_deploy_rejects_repository_mismatch():
    with pytest.raises(AuthError, match="repository not allowed for app app") as exc:
        authorize_deploy(_identity(repository="org/other"), APP, PREFIXES)
    assert exc.value.status_code == 403


def test_authorize_deploy_rejects_repository_id_mismatch():
    with pytest.raises(AuthError, match="repository not allowed") as exc:
        authorize_deploy(_identity(repository_id="43"), APP, PREFIXES)
    assert exc.value.status_code == 403


def test_authorize_deploy_rejects_feature_branch():
    with pytest.raises(AuthError, match="ref not allowed") as exc:
        authorize_deploy(_identity(ref="refs/heads/feature"), APP, PREFIXES)
    assert exc.value.status_code == 403


def test_authorize_deploy_accepts_version_tag():
    authorize_deploy(_identity(ref="refs/tags/v1.2.3"), APP, PREFIXES)


def test_authorize_deploy_rejects_foreign_workflow():
    identity = _identity(
        job_workflow_ref="evil/homelab-platform/.github/workflows/deploy.yml@x"
    )
    with pytest.raises(AuthError, match="workflow not allowed") as exc:
        authorize_deploy(identity, APP, PREFIXES)
    assert exc.value.status_code == 403


def test_require_admin_rejects_missing_login():
    with pytest.raises(AuthError, match="admin identity required") as exc:
        require_admin(None, {"a@b"})
    assert exc.value.status_code == 403


def test_require_admin_rejects_non_admin():
    with pytest.raises(AuthError, match="not an admin") as exc:
        require_admin("x@y", {"a@b"})
    assert exc.value.status_code == 403


def test_require_admin_returns_login():
    assert require_admin("a@b", {"a@b"}) == "a@b"
