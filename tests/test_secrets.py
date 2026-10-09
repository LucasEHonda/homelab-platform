import json

import httpx
import pytest

from deployer.secrets import (
    InfisicalClient,
    MissingSecretsError,
    SecretsError,
    ensure_required,
    render_env_file,
)


def make_client(handler):
    return InfisicalClient(
        "https://infisical.test/", httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_login_posts_credentials_and_returns_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"accessToken": "tok"})

    assert make_client(handler).login("id", "sec") == "tok"
    assert seen["url"] == "https://infisical.test/api/v1/auth/universal-auth/login"
    assert seen["body"] == {"clientId": "id", "clientSecret": "sec"}


def test_login_http_error_hides_secret():
    client = make_client(lambda request: httpx.Response(401))
    with pytest.raises(SecretsError) as exc:
        client.login("id", "sec")
    assert str(exc.value) == "infisical login failed: HTTP 401"
    assert "sec" not in str(exc.value)


def test_login_network_error():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(SecretsError, match="network error"):
        make_client(handler).login("id", "sec")


def test_list_secrets_sends_params_and_returns_mapping():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "secrets": [
                    {"secretKey": "A", "secretValue": "1"},
                    {"secretKey": "B", "secretValue": ""},
                ]
            },
        )

    result = make_client(handler).list_secrets("tok", "proj", "prod", "/django")
    assert result == {"A": "1", "B": ""}
    assert seen["path"] == "/api/v3/secrets/raw"
    assert seen["params"] == {
        "workspaceId": "proj",
        "environment": "prod",
        "secretPath": "/django",
    }
    assert seen["auth"] == "Bearer tok"


def test_list_secrets_http_error():
    client = make_client(lambda request: httpx.Response(403))
    with pytest.raises(SecretsError) as exc:
        client.list_secrets("tok", "proj", "prod", "/django")
    assert "/django" in str(exc.value)
    assert "HTTP 403" in str(exc.value)


def test_ensure_required():
    ensure_required({"A": "1", "B": ""}, ["A", "B"], "/django")
    with pytest.raises(MissingSecretsError) as exc:
        ensure_required({"A": "1", "B": ""}, ["A", "C", "D"], "/django")
    assert exc.value.missing == ["/django/C", "/django/D"]


def test_render_env_file():
    assert render_env_file({"B": "x$y", "A": "1"}) == "A='1'\nB='x$y'\n"
    assert render_env_file({}) == ""
    with pytest.raises(SecretsError) as exc:
        render_env_file({"A": "a'b"})
    assert "a'b" not in str(exc.value)
    with pytest.raises(SecretsError):
        render_env_file({"A": "a\nb"})
    with pytest.raises(SecretsError):
        render_env_file({"1BAD": "x"})
