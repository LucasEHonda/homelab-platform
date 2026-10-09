from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

import pytest

from deployer.breakglass import BreakGlassError, BreakGlassService
from deployer.config import PlatformConfig, parse_config
from deployer.runner import CommandResult

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

MYSQL_UNLOCK = [
    "sh",
    "-c",
    "MYSQL_PWD=\"$MYSQL_ROOT_PASSWORD\" mysql -uroot -e \"ALTER USER 'breakglass'@'%' ACCOUNT UNLOCK\"",
]
MYSQL_LOCK = [
    "sh",
    "-c",
    "MYSQL_PWD=\"$MYSQL_ROOT_PASSWORD\" mysql -uroot -e \"ALTER USER 'breakglass'@'%' ACCOUNT LOCK\"",
]
PG_UNLOCK = [
    "sh",
    "-c",
    "psql -v ON_ERROR_STOP=1 -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -c "
    "\"ALTER ROLE breakglass LOGIN VALID UNTIL '2026-01-01T13:00:00+00:00'\"",
]
PG_LOCK = [
    "sh",
    "-c",
    "psql -v ON_ERROR_STOP=1 -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -c "
    "\"ALTER ROLE breakglass NOLOGIN\"",
]


class FakeDocker:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[tuple[str, list[str]]] = []

    def exec(self, container: str, command: Any) -> CommandResult:
        self.calls.append((container, list(command)))
        return CommandResult(self.rc, "", "")


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)


class Scheduler:
    def __init__(self) -> None:
        self.scheduled: list[tuple[float, Callable[[], None]]] = []

    def __call__(self, seconds: float, fn: Callable[[], None]) -> None:
        self.scheduled.append((seconds, fn))


def _write_manifest(app_dir: Path, project: str, database: str | None) -> None:
    deploy = app_dir / "deploy"
    deploy.mkdir(parents=True)
    lines = [
        f"project: {project}",
        "compose: compose.yml",
        "image_vars: [API_IMAGE_REF]",
        "migrate_service: migrate",
        "health_service: api",
    ]
    if database is not None:
        lines.append(f"database: {{engine: {database}, service: {database}}}")
    (deploy / "deploy.yml").write_text("\n".join(lines) + "\n")


def _config(tmp_path: Path) -> PlatformConfig:
    def app(name: str, repo_id: str) -> dict[str, Any]:
        return {
            "repository": f"org/{name}",
            "repository_id": repo_id,
            "app_dir": str(tmp_path / name),
            "image_prefixes": ["ghcr.io/org/"],
            "infisical": {
                "project_id": "abc",
                "environment": "prod",
                "client_id_env": "ID",
                "client_secret_env": "SECRET",
            },
        }

    return parse_config(
        {
            "infisical_url": "https://secrets.example.ts.net",
            "ntfy_url_env": "NTFY_URL",
            "allowed_workflow_refs": ["org/repo/.github/workflows/deploy.yml@"],
            "admins": ["me@x"],
            "apps": {"games": app("games", "1"), "fin": app("fin", "2")},
        }
    )


class Setup:
    def __init__(self, tmp_path: Path, rc: int = 0) -> None:
        _write_manifest(tmp_path / "games", "ix-games", "mysql")
        _write_manifest(tmp_path / "fin", "ix-fin", "postgres")
        self.docker = FakeDocker(rc)
        self.notifier = FakeNotifier()
        self.scheduler = Scheduler()
        self.service = BreakGlassService(
            _config(tmp_path),
            self.docker,  # type: ignore[arg-type]
            self.notifier,
            now=lambda: NOW,
            schedule=self.scheduler,
        )


def test_open_mysql_unlocks_schedules_and_notifies(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    expires = s.service.open("games", "me@x")
    assert expires == datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    assert s.docker.calls == [("ix-games-mysql-1", MYSQL_UNLOCK)]
    assert [seconds for seconds, _ in s.scheduler.scheduled] == [3600]
    assert s.notifier.messages == ["games: break-glass opened until 13:00 UTC"]
    assert all("me@x" not in message for message in s.notifier.messages)
    s.scheduler.scheduled[0][1]()
    assert s.docker.calls[-1] == ("ix-games-mysql-1", MYSQL_LOCK)
    assert s.notifier.messages[-1] == "games: break-glass closed"


def test_open_postgres_uses_valid_until(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.service.open("fin", "me@x")
    assert s.docker.calls == [("ix-fin-postgres-1", PG_UNLOCK)]


def test_unlock_failure_raises_without_side_effects(tmp_path: Path) -> None:
    s = Setup(tmp_path, rc=1)
    with pytest.raises(BreakGlassError, match="could not unlock breakglass on games"):
        s.service.open("games", "me@x")
    assert s.scheduler.scheduled == []
    assert s.notifier.messages == []


def test_unknown_app(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    with pytest.raises(BreakGlassError, match="unknown app nope"):
        s.service.open("nope", "me@x")


def test_missing_manifest(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    (tmp_path / "games" / "deploy" / "deploy.yml").unlink()
    with pytest.raises(BreakGlassError, match="games has no deployed release"):
        s.service.open("games", "me@x")


def test_manifest_without_database(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    _write_manifest_path = tmp_path / "games" / "deploy" / "deploy.yml"
    _write_manifest_path.unlink()
    _write_manifest_path.parent.rmdir()
    _write_manifest(tmp_path / "games", "ix-games", None)
    with pytest.raises(BreakGlassError, match="games has no database"):
        s.service.open("games", "me@x")


def test_lock_all_locks_every_app_silently(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    s.service.lock_all()
    assert s.docker.calls == [
        ("ix-games-mysql-1", MYSQL_LOCK),
        ("ix-fin-postgres-1", PG_LOCK),
    ]
    assert s.notifier.messages == []


def test_lock_all_reports_failures(tmp_path: Path) -> None:
    s = Setup(tmp_path, rc=1)
    s.service.lock_all()
    assert s.notifier.messages == [
        "games: break-glass lock FAILED",
        "fin: break-glass lock FAILED",
    ]


def test_lock_all_skips_app_without_manifest(tmp_path: Path) -> None:
    s = Setup(tmp_path)
    (tmp_path / "games" / "deploy" / "deploy.yml").unlink()
    s.service.lock_all()
    assert s.docker.calls == [("ix-fin-postgres-1", PG_LOCK)]
    assert s.notifier.messages == []
