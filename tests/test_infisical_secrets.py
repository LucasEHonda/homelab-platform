# Failure list (each test below guards one of these):
# - secret values are printed
# - a malformed pair still sends HTTP
# - a "folder already exists" error aborts the run
# - other folder errors are swallowed
# - the batch body is missing the upsert mode
# - a missing INFISICAL_TOKEN is not detected
import json

import httpx
import pytest

from deployer.tools.infisical_bootstrap import InfisicalAdmin
from deployer.tools.infisical_secrets import run


class Recorder:
    def __init__(self, folder_response=None):
        self.requests = []
        self.folder_response = folder_response or httpx.Response(200, json={})

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/v2/folders":
            return self.folder_response
        return httpx.Response(200, json={})

    def calls(self, path):
        return [body for _, p, body in self.requests if p == path]


def make_admin(recorder):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return InfisicalAdmin("http://infisical:8080", client, sleep=lambda s: None)


def argv(*pairs, environment=None):
    base = ["--url", "http://infisical:8080", "--project-id", "proj1", "--folder", "django"]
    if environment:
        base += ["--environment", environment]
    return [*base, *pairs]


ENV = {"INFISICAL_TOKEN": "tok"}


def test_upserts_all_pairs_in_one_batch_without_printing_values(capsys):
    recorder = Recorder()
    code = run(argv("A_KEY=s3cret", "B=x=y"), admin=make_admin(recorder), environ=ENV)
    assert code == 0
    assert recorder.calls("/api/v2/folders")[0]["name"] == "django"
    assert recorder.calls("/api/v4/secrets/batch") == [
        {
            "projectId": "proj1",
            "environment": "prod",
            "secretPath": "/django",
            "mode": "upsert",
            "secrets": [
                {"secretKey": "A_KEY", "secretValue": "s3cret"},
                {"secretKey": "B", "secretValue": "x=y"},
            ],
        }
    ]
    assert [m for m, p, _ in recorder.requests if p == "/api/v4/secrets/batch"] == ["PATCH"]
    captured = capsys.readouterr()
    assert captured.out.strip() == "infisical_secrets: 2 secret(s) set in /django"
    assert "s3cret" not in captured.out + captured.err
    assert "x=y" not in captured.out + captured.err


def test_environment_option(capsys):
    recorder = Recorder()
    run(argv("A=1", environment="dev"), admin=make_admin(recorder), environ=ENV)
    assert recorder.calls("/api/v4/secrets/batch")[0]["environment"] == "dev"


@pytest.mark.parametrize("pair", ["NOEQUALS", "=value", "lower=1", "1ABC=1", "A-B=1"])
def test_malformed_pair_fails_before_http(pair, capsys):
    recorder = Recorder()
    code = run(argv("OK=1", pair), admin=make_admin(recorder), environ=ENV)
    assert code == 1
    assert recorder.requests == []
    assert "infisical_secrets:" in capsys.readouterr().err


def test_missing_token(capsys):
    recorder = Recorder()
    code = run(argv("A=1"), admin=make_admin(recorder), environ={})
    assert code == 1
    assert recorder.requests == []
    assert "infisical_secrets: INFISICAL_TOKEN is required" in capsys.readouterr().err


@pytest.mark.parametrize("status", [400, 409])
def test_existing_folder_is_ignored(status, capsys):
    recorder = Recorder(httpx.Response(status, json={"message": "Folder with name django already exists"}))
    code = run(argv("A=1"), admin=make_admin(recorder), environ=ENV)
    assert code == 0
    assert len(recorder.calls("/api/v4/secrets/batch")) == 1


def test_other_folder_error_aborts(capsys):
    recorder = Recorder(httpx.Response(403, json={"message": "forbidden"}))
    code = run(argv("A=1"), admin=make_admin(recorder), environ=ENV)
    assert code == 1
    assert recorder.calls("/api/v4/secrets/batch") == []
    assert "HTTP 403" in capsys.readouterr().err


def test_other_400_folder_error_aborts(capsys):
    recorder = Recorder(httpx.Response(400, json={"message": "invalid name"}))
    assert run(argv("A=1"), admin=make_admin(recorder), environ=ENV) == 1
    assert recorder.calls("/api/v4/secrets/batch") == []


def test_batch_failure_reports_message(capsys):
    def handler(request):
        if request.url.path == "/api/v4/secrets/batch":
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(200, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    admin = InfisicalAdmin("http://infisical:8080", client, sleep=lambda s: None)
    assert run(argv("A=1"), admin=admin, environ=ENV) == 1
    err = capsys.readouterr().err
    assert "infisical_secrets:" in err and "HTTP 500 boom" in err
