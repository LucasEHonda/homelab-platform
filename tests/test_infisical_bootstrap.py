import json
import stat

import httpx
import pytest
import yaml

from deployer.tools.infisical_bootstrap import (
    BootstrapError,
    InfisicalAdmin,
    parse_env_file,
    parse_generate,
    run,
    set_env_lines,
)


class Recorder:
    def __init__(self, fail_projects=False):
        self.requests = []
        self.fail_projects = fail_projects
        self.project_count = 0

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/api/status":
            return httpx.Response(200, json={})
        if path == "/api/v1/admin/bootstrap":
            return httpx.Response(
                200,
                json={"identity": {"credentials": {"token": "tok"}}, "organization": {"id": "org1"}},
            )
        if path == "/api/v1/projects":
            if self.fail_projects:
                return httpx.Response(400, json={"message": "bad project"})
            self.project_count += 1
            return httpx.Response(200, json={"project": {"id": "proj-games"}})
        if path == "/api/v1/identities":
            return httpx.Response(200, json={"identity": {"id": "ident1"}})
        if path == "/api/v1/auth/universal-auth/identities/ident1":
            return httpx.Response(200, json={"identityUniversalAuth": {"clientId": "cid"}})
        if path == "/api/v1/auth/universal-auth/identities/ident1/client-secrets":
            return httpx.Response(200, json={"clientSecret": "csecret"})
        return httpx.Response(200, json={})

    def calls(self, path):
        return [body for _, p, body in self.requests if p == path]


def make_admin(recorder):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return InfisicalAdmin("http://infisical:8080", client, sleep=lambda s: None)


def test_parse_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# c\n\nA=1\nB='x y'\nC=\"q\"\nD=\nE=a=b\nNOEQ\n")
    assert parse_env_file(path) == {"A": "1", "B": "x y", "C": "q", "D": "", "E": "a=b"}


def test_set_env_lines(tmp_path):
    path = tmp_path / "env"
    path.write_text("A=old\nB=keep\n")
    set_env_lines(path, {"A": "new", "C": "3"})
    assert path.read_text() == "B=keep\nA=new\nC=3\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fresh = tmp_path / "fresh"
    set_env_lines(fresh, {"X": "1"})
    assert stat.S_IMODE(fresh.stat().st_mode) == 0o600


def test_parse_generate():
    assert parse_generate(["games:mysql=A,B", "games:django=C"]) == {
        "games": {"mysql": ["A", "B"], "django": ["C"]}
    }
    for spec in ("games", "games:mysql", "games=A", "games:mysql="):
        with pytest.raises(BootstrapError):
            parse_generate([spec])


def test_wait_ready():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(503 if state["n"] <= 2 else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    InfisicalAdmin("http://x", client, sleep=lambda s: None).wait_ready()
    assert state["n"] == 3
    always = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(BootstrapError):
        InfisicalAdmin("http://x", always, sleep=lambda s: None).wait_ready(10)


def test_wait_ready_reports_progress(capsys):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(503 if state["n"] <= 2 else 200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    InfisicalAdmin("http://x", client, sleep=lambda s: None).wait_ready()
    out = capsys.readouterr().out
    assert "waiting for Infisical to finish starting (0s of 900s)" in out
    assert "Infisical is ready" in out

    ready = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    InfisicalAdmin("http://x", ready, sleep=lambda s: None).wait_ready()
    assert capsys.readouterr().out == ""


def write_apps(tmp_path):
    app_dir = tmp_path / "games"
    env_dir = app_dir / ".envs" / ".production"
    env_dir.mkdir(parents=True)
    (env_dir / ".django").write_text("A=1\n")
    (env_dir / ".mysql").write_text("MYSQL_PASSWORD=p\n")
    (env_dir / ".django.example").write_text("A=example\n")
    (env_dir / ".mysql.tmp").write_text("T=tmp\n")

    def infisical(project_id, prefix):
        return {
            "project_id": project_id,
            "environment": "prod",
            "client_id_env": f"{prefix}_INFISICAL_CLIENT_ID",
            "client_secret_env": f"{prefix}_INFISICAL_CLIENT_SECRET",
        }

    apps = {
        "apps": {
            "games": {"app_dir": str(app_dir), "infisical": infisical("replace-with-project-id", "GAMES")},
            "fin": {"app_dir": str(tmp_path / "fin"), "infisical": infisical("real-fin", "FIN")},
        }
    }
    path = tmp_path / "apps.yml"
    path.write_text(yaml.safe_dump(apps, sort_keys=False))
    return path


def argv(apps, env, *extra):
    return [
        "--url", "http://infisical:8080",
        "--apps-file", str(apps),
        "--deployer-env", str(env),
        "--admin-email", "a@b.c",
        *extra,
    ]


def test_full_run(tmp_path, capsys):
    recorder = Recorder()
    apps = write_apps(tmp_path)
    env = tmp_path / "deployer.env"
    code = run(
        argv(apps, env, "--generate", "games:mysql=MYSQL_READONLY_PASSWORD"),
        admin=make_admin(recorder),
        environ={"INFISICAL_ADMIN_PASSWORD": "pw"},
    )
    assert code == 0
    assert recorder.calls("/api/v1/admin/bootstrap") == [
        {"email": "a@b.c", "password": "pw", "organization": "homelab"}
    ]
    assert recorder.project_count == 1
    assert recorder.calls("/api/v1/projects") == [{"projectName": "games"}]
    assert [b["name"] for b in recorder.calls("/api/v2/folders")] == ["django", "mysql"]
    batches = {b["secretPath"]: b for b in recorder.calls("/api/v4/secrets/batch")}
    mysql = {s["secretKey"]: s["secretValue"] for s in batches["/mysql"]["secrets"]}
    assert mysql["MYSQL_PASSWORD"] == "p"
    assert mysql["MYSQL_READONLY_PASSWORD"]
    assert [s["secretKey"] for s in batches["/django"]["secrets"]] == ["A"]
    identity = recorder.calls("/api/v1/identities")[0]
    assert identity["name"] == "games-deployer" and identity["role"] == "no-access"
    membership = recorder.calls("/api/v1/projects/proj-games/identity-memberships/ident1")
    assert membership == [{"role": "viewer"}]
    text = env.read_text()
    assert "GAMES_INFISICAL_CLIENT_ID=cid" in text
    assert "GAMES_INFISICAL_CLIENT_SECRET=csecret" in text
    data = yaml.safe_load(apps.read_text())
    assert data["apps"]["games"]["infisical"]["project_id"] == "proj-games"
    assert data["apps"]["fin"]["infisical"]["project_id"] == "real-fin"
    out = capsys.readouterr().out
    for secret in ("pw", "csecret", "MYSQL_PASSWORD=p"):
        assert secret not in out
    assert " p " not in out


def test_nothing_pending(tmp_path, capsys):
    recorder = Recorder()
    apps = write_apps(tmp_path)
    data = yaml.safe_load(apps.read_text())
    data["apps"]["games"]["infisical"]["project_id"] = "done"
    apps.write_text(yaml.safe_dump(data))
    code = run(argv(apps, tmp_path / "e"), admin=make_admin(recorder), environ={})
    assert code == 0
    assert recorder.requests == []


def test_missing_password(tmp_path, capsys):
    recorder = Recorder()
    code = run(argv(write_apps(tmp_path), tmp_path / "e"), admin=make_admin(recorder), environ={})
    assert code == 1
    assert "INFISICAL_ADMIN_PASSWORD" in capsys.readouterr().err


def test_project_http_error(tmp_path, capsys):
    recorder = Recorder(fail_projects=True)
    code = run(
        argv(write_apps(tmp_path), tmp_path / "e"),
        admin=make_admin(recorder),
        environ={"INFISICAL_ADMIN_PASSWORD": "pw"},
    )
    assert code == 1
    assert "HTTP 400" in capsys.readouterr().err
