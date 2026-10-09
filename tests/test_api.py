from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from deployer.auth import AuthError, CallerIdentity
from deployer.config import parse_config

WORKFLOW = "LucasEHonda/homelab-platform/.github/workflows/deploy.yml@"
GOOD = CallerIdentity("org/games", "1", "refs/heads/main", WORKFLOW + "refs/heads/main", "j1")
OTHER = CallerIdentity("org/other", "2", "refs/heads/main", WORKFLOW + "refs/heads/main", "j2")
EXPIRES = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
BODY = {"app": "games", "version": "1.0.0", "images": {"web": "ghcr.io/org/web:1"}, "bundle": "ghcr.io/org/bundle:1"}
AUTH = {"Authorization": "Bearer tok"}


def make_config():
    return parse_config(
        {
            "infisical_url": "https://infisical.example.com",
            "ntfy_url_env": "NTFY_URL",
            "allowed_workflow_refs": [WORKFLOW],
            "admins": ["me@x"],
            "apps": {
                "games": {
                    "repository": "org/games",
                    "repository_id": "1",
                    "app_dir": "/srv/games",
                    "image_prefixes": ["ghcr.io/org/"],
                    "infisical": {
                        "project_id": "p",
                        "environment": "prod",
                        "client_id_env": "CID",
                        "client_secret_env": "CSEC",
                    },
                }
            },
        }
    )


class FakeVerifier:
    def __init__(self):
        self.identity = GOOD
        self.error = None

    def verify(self, token):
        if self.error:
            raise self.error
        return self.identity


@pytest.fixture
def env():
    from deployer.api import create_app
    from deployer.breakglass import BreakGlassError
    from deployer.service import (
        ConflictError,
        Deployment,
        DeploymentStatus,
        DeployRequest,
        RequestError,
    )

    class FakeDeployments:
        def __init__(self):
            self.submitted = []
            self.error = None
            self.known = {}

        def submit(self, request):
            if self.error:
                raise self.error
            self.submitted.append(request)
            return Deployment("d1", request.app, request.version, DeploymentStatus.QUEUED, "", "t")

        def get(self, deployment_id):
            return self.known.get(deployment_id)

    class FakeBreakGlass:
        def __init__(self):
            self.error = None
            self.calls = []

        def open(self, app_name, user):
            if self.error:
                raise self.error
            self.calls.append((app_name, user))
            return EXPIRES

    class Env:
        pass

    e = Env()
    e.verifier = FakeVerifier()
    e.deployments = FakeDeployments()
    e.break_glass = FakeBreakGlass()
    e.client = TestClient(create_app(make_config(), e.verifier, e.deployments, e.break_glass))
    e.Deployment = Deployment
    e.Status = DeploymentStatus
    e.DeployRequest = DeployRequest
    e.RequestError = RequestError
    e.ConflictError = ConflictError
    e.BreakGlassError = BreakGlassError
    return e


def test_healthz(env):
    response = env.client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_post_without_bearer_is_401(env):
    response = env.client.post("/v1/deployments", json=BODY)
    assert response.status_code == 401
    assert response.json() == {"detail": "bearer token required"}


def test_post_invalid_token_is_401(env):
    env.verifier.error = AuthError(401, "invalid token: x")
    response = env.client.post("/v1/deployments", json=BODY, headers=AUTH)
    assert response.status_code == 401


def test_post_unknown_app_is_403(env):
    response = env.client.post("/v1/deployments", json={**BODY, "app": "nope"}, headers=AUTH)
    assert response.status_code == 403
    assert response.json() == {"detail": "app not allowed"}


def test_post_other_repository_is_403(env):
    env.verifier.identity = OTHER
    response = env.client.post("/v1/deployments", json=BODY, headers=AUTH)
    assert response.status_code == 403


def test_post_valid_is_202(env):
    response = env.client.post("/v1/deployments", json=BODY, headers=AUTH)
    assert response.status_code == 202
    assert response.json() == {"id": "d1"}
    assert env.deployments.submitted == [
        env.DeployRequest("games", "1.0.0", BODY["images"], "ghcr.io/org/bundle:1")
    ]


def test_post_request_error_is_422(env):
    env.deployments.error = env.RequestError("bad")
    response = env.client.post("/v1/deployments", json=BODY, headers=AUTH)
    assert response.status_code == 422
    assert response.json() == {"detail": "bad"}


def test_post_conflict_is_409(env):
    env.deployments.error = env.ConflictError("busy")
    response = env.client.post("/v1/deployments", json=BODY, headers=AUTH)
    assert response.status_code == 409
    assert response.json() == {"detail": "busy"}


def test_get_unknown_is_404(env):
    response = env.client.get("/v1/deployments/zzz", headers=AUTH)
    assert response.status_code == 404


def test_get_known_is_200(env):
    env.deployments.known["d1"] = env.Deployment("d1", "games", "1.0.0", env.Status.QUEUED, "", "t")
    response = env.client.get("/v1/deployments/d1", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_get_other_repository_is_403(env):
    env.deployments.known["d1"] = env.Deployment("d1", "games", "1.0.0", env.Status.QUEUED, "", "t")
    env.verifier.identity = OTHER
    response = env.client.get("/v1/deployments/d1", headers=AUTH)
    assert response.status_code == 403


def test_break_glass_without_header_is_403(env):
    assert env.client.post("/v1/apps/games/break-glass").status_code == 403


def test_break_glass_non_admin_is_403(env):
    response = env.client.post("/v1/apps/games/break-glass", headers={"Tailscale-User-Login": "other@x"})
    assert response.status_code == 403


def test_break_glass_admin_is_200(env):
    response = env.client.post("/v1/apps/games/break-glass", headers={"Tailscale-User-Login": "me@x"})
    assert response.status_code == 200
    assert response.json() == {"expires_at": EXPIRES.isoformat()}
    assert env.break_glass.calls == [("games", "me@x")]


def test_break_glass_error_is_409(env):
    env.break_glass.error = env.BreakGlassError("no db")
    response = env.client.post("/v1/apps/games/break-glass", headers={"Tailscale-User-Login": "me@x"})
    assert response.status_code == 409
    assert response.json() == {"detail": "no db"}
