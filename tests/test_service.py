import json
import os
import stat
from pathlib import Path
from typing import Any, Callable

import pytest
import yaml

from deployer.config import parse_config
from deployer.runner import CommandResult, DockerError
from deployer.secrets import SecretsError
from deployer.service import (
    ConflictError,
    DeployRequest,
    DeploymentService,
    InfisicalSecretsSource,
    RequestError,
)

API = "ghcr.io/my-games-hub-br/games-api@sha256:" + "a" * 64
BUNDLE = "ghcr.io/my-games-hub-br/games-deploy@sha256:" + "b" * 64

MANIFEST: dict[str, Any] = {
    "project": "ix-games",
    "compose": "compose.yml",
    "image_vars": ["API_IMAGE_REF"],
    "env_files": {"../.envs/.production/.django": "/django"},
    "backup": {"service": "backup", "command": ["sh", "x", "now"]},
    "migrate_service": "migrate",
    "health_service": "api",
    "health_timeout_seconds": 30,
}


def default_inspect(container: str, template: str, index: int) -> str | None:
    if "State.Running" in template:
        return "false"
    if "State.Health" in template:
        return f"{API} healthy"
    return None


class FakeDocker:
    def __init__(self) -> None:
        self.manifest: dict[str, Any] = dict(MANIFEST)
        self.template: str | None = "A=\nB=\n"
        self.rendered: dict[str, Any] = {
            "services": {"migrate": {"image": API}, "api": {"image": API}}
        }
        self.fail_up_on: set[int] = set()
        self.exec_result = CommandResult(0, "", "")
        self.inspect_fn: Callable[[str, str, int], str | None] = default_inspect
        self.pulled: list[str] = []
        self.extracted: list[Path] = []
        self.pull_calls = 0
        self.up_calls = 0
        self.exec_calls: list[tuple[str, tuple[str, ...]]] = []
        self.inspect_calls = 0
        self.compose_config_calls = 0
        self.required_files: list[Path] = []

    def pull(self, ref: str) -> None:
        self.pulled.append(ref)

    def extract_bundle(self, ref: str, dest: Path) -> None:
        dest.mkdir(parents=True)
        (dest / "deploy.yml").write_text(yaml.safe_dump(self.manifest))
        (dest / "compose.yml").write_text("services: {}")
        if self.template is not None:
            (dest / "envs").mkdir()
            (dest / "envs" / ".django.example").write_text(self.template)
        self.extracted.append(dest)

    def compose_config(self, target: Any) -> dict:
        self.compose_config_calls += 1
        for path in self.required_files:
            assert path.exists(), f"{path} must exist before compose config"
        return self.rendered

    def compose_pull(self, target: Any) -> None:
        self.pull_calls += 1

    def compose_up(self, target: Any) -> None:
        self.up_calls += 1
        if self.up_calls in self.fail_up_on:
            raise DockerError("up failed")

    def exec(self, container: str, command: Any) -> CommandResult:
        self.exec_calls.append((container, tuple(command)))
        return self.exec_result

    def inspect(self, container: str, template: str) -> str | None:
        self.inspect_calls += 1
        return self.inspect_fn(container, template, self.inspect_calls)


class FakeSecrets:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, str]] = {"/django": {"A": "1", "B": "2"}}
        self.error: Exception | None = None

    def fetch(self, app: Any, folder: str) -> dict[str, str]:
        if self.error:
            raise self.error
        return self.values[folder]


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 5
        return self.now


class Env:
    def __init__(self, tmp_path: Path, run_async: Any = None) -> None:
        self.app_dir = tmp_path / "games"
        self.history = tmp_path / "history" / "deployments.jsonl"
        self.config = parse_config(
            {
                "infisical_url": "https://secrets.example.ts.net",
                "ntfy_url_env": "NTFY_URL",
                "allowed_workflow_refs": ["x/y/.github/workflows/deploy.yml@"],
                "admins": ["owner@example.com"],
                "apps": {
                    "games": {
                        "repository": "my-games-hub-br/my-games-hub",
                        "repository_id": "123456",
                        "app_dir": str(self.app_dir),
                        "image_prefixes": ["ghcr.io/my-games-hub-br/"],
                        "infisical": {
                            "project_id": "proj",
                            "environment": "prod",
                            "client_id_env": "GAMES_ID",
                            "client_secret_env": "GAMES_SECRET",
                        },
                    }
                },
            }
        )
        self.docker = FakeDocker()
        self.secrets = FakeSecrets()
        self.notifier = FakeNotifier()
        self.service = DeploymentService(
            self.config,
            self.docker,  # type: ignore[arg-type]
            self.secrets,
            self.notifier,
            self.history,
            sleep=lambda s: None,
            clock=Clock(),
            run_async=run_async or (lambda fn: fn()),
        )

    def deploy(self, version: str = "v1.0.0", images: dict[str, str] | None = None):
        return self.service.submit(
            DeployRequest(
                "games", version, images or {"API_IMAGE_REF": API}, BUNDLE
            )
        )

    @property
    def link(self) -> Path:
        return self.app_dir / "deploy"

    @property
    def releases(self) -> Path:
        return self.app_dir / ".releases"


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def migrate_failed(container: str, template: str, index: int) -> str | None:
    if "State.ExitCode" in template:
        return "exited 1 " + API
    return default_inspect(container, template, index)


def never_healthy(container: str, template: str, index: int) -> str | None:
    if "State.Health" in template:
        return f"{API} starting"
    return default_inspect(container, template, index)


def test_happy_path_first_deploy(env: Env) -> None:
    deployment = env.deploy()
    assert deployment.status == "live"
    assert env.link.is_symlink()
    target = os.readlink(env.link)
    assert target == f".releases/v1.0.0-{deployment.id[:8]}"
    env_file = env.app_dir / ".envs" / ".production" / ".django"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert env_file.read_text() == "A='1'\nB='2'\n"
    release_env = (env.app_dir / target / ".env").read_text()
    assert "APP_VERSION='v1.0.0'" in release_env
    assert env.notifier.messages == ["games v1.0.0: live"]
    lines = env.history.read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "live"
    assert env.service.get(deployment.id) is deployment


def test_legacy_directory_is_moved(env: Env) -> None:
    env.link.mkdir(parents=True)
    (env.link / "old.txt").write_text("old")
    deployment = env.deploy()
    assert deployment.status == "live"
    legacy = env.releases / f"legacy-{deployment.id[:8]}"
    assert (legacy / "old.txt").read_text() == "old"
    assert env.link.is_symlink()


def test_first_deploy_composes_from_app_dir_deploy(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dirs: list[Path] = []
    original = env.docker.compose_config

    def record(target: Any) -> dict:
        project_dirs.append(target.project_dir)
        return original(target)

    monkeypatch.setattr(env.docker, "compose_config", record)
    deployment = env.deploy()
    assert deployment.status == "live"
    assert project_dirs == [env.app_dir / "deploy"]
    assert env.link.is_symlink()
    assert list(env.releases.glob("legacy-*"))


def test_migrate_failure_rolls_back(env: Env) -> None:
    first = env.deploy("v1.0.0")
    assert first.status == "live"
    env.docker.inspect_fn = migrate_failed
    second = env.deploy("v1.1.0")
    assert second.status == "rolled_back"
    assert second.detail.startswith("migrations failed; back on v1.0.0-")
    assert os.readlink(env.link) == f".releases/v1.0.0-{first.id[:8]}"
    assert env.docker.up_calls == 3


def test_compose_up_error_with_migrate_failure_rolls_back(env: Env) -> None:
    first = env.deploy("v1.0.0")
    env.docker.inspect_fn = migrate_failed
    env.docker.fail_up_on = {2}
    second = env.deploy("v1.1.0")
    assert second.status == "rolled_back"
    assert second.detail.startswith("migrations failed; back on v1.0.0-")
    assert os.readlink(env.link) == f".releases/v1.0.0-{first.id[:8]}"
    assert env.docker.up_calls == 3


def test_health_timeout_fails_and_keeps_link(env: Env) -> None:
    env.docker.inspect_fn = never_healthy
    deployment = env.deploy()
    assert deployment.status == "failed"
    assert "did not become healthy" in deployment.detail
    assert os.readlink(env.link) == f".releases/v1.0.0-{deployment.id[:8]}"
    assert env.notifier.messages == ["games v1.0.0: failed"]


def test_env_files_exist_before_compose_config(env: Env) -> None:
    env.docker.required_files = [env.app_dir / ".envs" / ".production" / ".django"]
    deployment = env.deploy()
    assert deployment.status == "live"


def test_missing_secret_blocks(env: Env) -> None:
    env.docker.template = "A=\nB=\nC=\n"
    deployment = env.deploy()
    assert deployment.status == "blocked"
    assert deployment.detail == "missing secrets: /django/C"
    assert env.docker.compose_config_calls == 0
    assert not (env.app_dir / ".envs").exists()
    assert not env.link.exists() and not env.link.is_symlink()
    assert list(env.releases.iterdir()) == []
    assert env.notifier.messages == [
        "games v1.0.0: blocked (missing secrets: /django/C)"
    ]


def test_policy_violation_blocks(env: Env) -> None:
    env.docker.rendered = {
        "services": {
            "migrate": {"image": API},
            "api": {
                "image": API,
                "volumes": [{"type": "bind", "source": "/etc", "target": "/x"}],
            },
        }
    }
    deployment = env.deploy()
    assert deployment.status == "blocked"
    assert (env.app_dir / ".envs" / ".production" / ".django").exists()
    assert not env.link.is_symlink()
    assert env.docker.pull_calls == 0
    assert env.notifier.messages == ["games v1.0.0: blocked (see the Deploy job)"]


def test_image_var_mismatch_blocks(env: Env) -> None:
    deployment = env.deploy(images={"WEB_IMAGE_REF": API})
    assert deployment.status == "blocked"
    assert "API_IMAGE_REF" in deployment.detail
    assert not env.link.is_symlink()
    assert env.notifier.messages == ["games v1.0.0: blocked (see the Deploy job)"]


def test_backup_failure_fails_without_switch(env: Env) -> None:
    def running(container: str, template: str, index: int) -> str | None:
        if "State.Running" in template:
            return "true"
        return default_inspect(container, template, index)

    env.docker.inspect_fn = running
    env.docker.exec_result = CommandResult(1, "", "boom")
    deployment = env.deploy()
    assert deployment.status == "failed"
    assert deployment.detail == "database backup failed"
    assert env.docker.exec_calls == [("ix-games-backup-1", ("sh", "x", "now"))]
    assert not env.link.is_symlink()
    assert env.notifier.messages == ["games v1.0.0: failed"]


def test_submit_validation(env: Env) -> None:
    with pytest.raises(RequestError):
        env.service.submit(DeployRequest("nope", "v1.0.0", {"A": API}, BUNDLE))
    with pytest.raises(RequestError):
        env.deploy("1.0")
    with pytest.raises(RequestError):
        env.service.submit(DeployRequest("games", "v1.0.0", {}, BUNDLE))
    with pytest.raises(RequestError):
        env.deploy(images={"API_IMAGE_REF": "ghcr.io/my-games-hub-br/games-api:latest"})
    with pytest.raises(RequestError):
        env.service.submit(
            DeployRequest(
                "games",
                "v1.0.0",
                {"API_IMAGE_REF": API},
                "ghcr.io/other/games-deploy@sha256:" + "b" * 64,
            )
        )


def test_service_missing_from_compose_blocks(env: Env) -> None:
    env.docker.rendered = {"services": {"api": {"image": API}}}
    deployment = env.deploy()
    assert deployment.status == "blocked"
    assert deployment.detail == "service migrate is not in compose.yml"
    assert not env.link.is_symlink()


def test_unexpected_error_fails_releases_lock_and_restores_previous(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = env.deploy("v1.0.0")
    assert first.status == "live"

    def crash(target: Any) -> None:
        raise KeyError("x")

    monkeypatch.setattr(env.docker, "compose_pull", crash)
    second = env.deploy("v1.1.0")
    assert second.status == "failed"
    assert second.detail == "internal error"
    assert os.readlink(env.link) == f".releases/v1.0.0-{first.id[:8]}"
    monkeypatch.undo()
    third = env.deploy("v1.0.1")
    assert third.status == "live"


def test_conflict_while_running(tmp_path: Path) -> None:
    stored: list[Callable[[], None]] = []
    env = Env(tmp_path, run_async=stored.append)
    first = env.deploy()
    assert first.status == "queued"
    with pytest.raises(ConflictError):
        env.deploy("v1.0.1")
    stored.pop()()
    assert first.status == "live"
    second = env.deploy("v1.0.1")
    assert second.id != first.id
    stored.pop()()
    assert second.status == "live"


class FakeInfisical:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def login(self, client_id: str, client_secret: str) -> str:
        self.calls.append(("login", client_id, client_secret))
        return "tok"

    def list_secrets(
        self, token: str, project_id: str, environment: str, folder: str
    ) -> dict[str, str]:
        self.calls.append(("list", token, project_id, environment, folder))
        return {"A": "1"}


def test_infisical_source_requires_credentials(env: Env) -> None:
    app = env.config.apps["games"]
    client = FakeInfisical()
    source = InfisicalSecretsSource(client, {"GAMES_ID": "id"})  # type: ignore[arg-type]
    with pytest.raises(SecretsError, match="credentials for games are not configured"):
        source.fetch(app, "/django")
    assert client.calls == []


def test_infisical_source_fetches(env: Env) -> None:
    app = env.config.apps["games"]
    client = FakeInfisical()
    source = InfisicalSecretsSource(
        client, {"GAMES_ID": "id", "GAMES_SECRET": "sec"}  # type: ignore[arg-type]
    )
    assert source.fetch(app, "/django") == {"A": "1"}
    assert client.calls == [
        ("login", "id", "sec"),
        ("list", "tok", "proj", "prod", "/django"),
    ]
