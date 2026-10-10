import copy
from pathlib import Path
from typing import Any, Callable

import pytest

from deployer.manifest import (
    BackupStep,
    DatabaseSpec,
    ManifestError,
    parse_manifest,
    read_template_keys,
    resolve_in_app_dir,
)

VALID: dict[str, Any] = {
    "project": "ix-games",
    "compose": "compose.yml",
    "image_vars": ["API_IMAGE_REF", "WEB_IMAGE_REF"],
    "env_files": {
        "../.envs/.production/.django": "/django",
        "../.envs/.production/.mysql": "/mysql",
    },
    "backup": {"service": "backup", "command": ["sh", "/usr/local/bin/mysql-backup.sh", "now"]},
    "migrate_service": "migrate",
    "health_service": "api",
    "health_timeout_seconds": 300,
    "database": {"engine": "mysql", "service": "mysql"},
}


def _valid() -> dict[str, Any]:
    return copy.deepcopy(VALID)


def test_valid_manifest_parses() -> None:
    manifest = parse_manifest(_valid())
    assert manifest.project == "ix-games"
    assert manifest.compose == "compose.yml"
    assert manifest.image_vars == ("API_IMAGE_REF", "WEB_IMAGE_REF")
    assert manifest.env_files == VALID["env_files"]
    assert isinstance(manifest.env_files, dict)
    assert manifest.backup == BackupStep("backup", ("sh", "/usr/local/bin/mysql-backup.sh", "now"))
    assert manifest.migrate_service == "migrate"
    assert manifest.health_service == "api"
    assert manifest.health_timeout_seconds == 300
    assert manifest.database == DatabaseSpec("mysql", "mysql")


def test_defaults_when_optional_keys_absent() -> None:
    data = _valid()
    for key in ("env_files", "backup", "health_timeout_seconds", "database"):
        del data[key]
    manifest = parse_manifest(data)
    assert manifest.env_files == {}
    assert manifest.backup is None
    assert manifest.health_timeout_seconds == 300
    assert manifest.database is None


def _unknown(data: dict[str, Any]) -> None:
    data["extra"] = 1


def _no_project(data: dict[str, Any]) -> None:
    del data["project"]


def _bad_compose(data: dict[str, Any]) -> None:
    data["compose"] = "sub/compose.yml"


def _bad_var(data: dict[str, Any]) -> None:
    data["image_vars"] = ["api_ref"]


def _dup_var(data: dict[str, Any]) -> None:
    data["image_vars"] = ["A_REF", "A_REF"]


def _abs_key(data: dict[str, Any]) -> None:
    data["env_files"] = {"/etc/x": "/django"}


def _bad_folder(data: dict[str, Any]) -> None:
    data["env_files"] = {"../x": "django"}


def _empty_command(data: dict[str, Any]) -> None:
    data["backup"]["command"] = []


def _low_timeout(data: dict[str, Any]) -> None:
    data["health_timeout_seconds"] = 10


def _bool_timeout(data: dict[str, Any]) -> None:
    data["health_timeout_seconds"] = True


def _bad_engine(data: dict[str, Any]) -> None:
    data["database"]["engine"] = "sqlite"


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_unknown, "extra: unknown key"),
        (_no_project, "project: is required"),
        (_bad_compose, "compose"),
        (_bad_var, "image_vars"),
        (_dup_var, "image_vars: duplicate A_REF"),
        (_abs_key, "env_files./etc/x: must be relative"),
        (_bad_folder, "env_files.../x: invalid folder"),
        (_empty_command, "backup.command"),
        (_low_timeout, "health_timeout_seconds"),
        (_bool_timeout, "health_timeout_seconds"),
        (_bad_engine, "database.engine"),
    ],
)
def test_invalid_manifest_raises(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data = _valid()
    mutate(data)
    with pytest.raises(ManifestError, match=message):
        parse_manifest(data)


def test_resolve_in_app_dir_valid() -> None:
    app_dir = Path("/mnt/svd/games")
    result = resolve_in_app_dir(app_dir, "../.envs/.production/.django")
    assert result == Path("/mnt/svd/games/.envs/.production/.django")


@pytest.mark.parametrize(
    "relative",
    ["../../etc/passwd", ".env", "../.releases/x", "/abs", "../"],
)
def test_resolve_in_app_dir_rejects(relative: str) -> None:
    with pytest.raises(ManifestError, match="must point inside"):
        resolve_in_app_dir(Path("/mnt/svd/games"), relative)


def test_read_template_keys(tmp_path: Path) -> None:
    path = tmp_path / ".env.template"
    path.write_text("# comment\n\nA=1\nB=\nA=2\n C = x\n")
    assert read_template_keys(path) == ["A", "B", "C"]


def test_read_template_keys_invalid_line(tmp_path: Path) -> None:
    path = tmp_path / ".env.template"
    path.write_text("NOPE\n")
    with pytest.raises(ManifestError, match="invalid line 'NOPE'"):
        read_template_keys(path)


# FAILS IF: shared mysql accepted; shared flag dropped; backup shared form rejected; backup shared with extra keys accepted; old manifests break
def test_shared_postgres_database_and_backup_parse() -> None:
    data = _valid()
    data["database"] = {"engine": "postgres", "service": "data-postgres", "shared": True}
    data["backup"] = {"shared": True}
    manifest = parse_manifest(data)
    assert manifest.database == DatabaseSpec("postgres", "data-postgres", shared=True)
    assert manifest.backup == BackupStep(shared=True)


def test_database_shared_defaults_to_false() -> None:
    assert parse_manifest(_valid()).database == DatabaseSpec("mysql", "mysql")
    assert parse_manifest(_valid()).backup.shared is False


def test_shared_mysql_is_rejected() -> None:
    data = _valid()
    data["database"]["shared"] = True
    with pytest.raises(ManifestError, match="database.shared: only postgres"):
        parse_manifest(data)


def test_database_shared_must_be_bool() -> None:
    data = _valid()
    data["database"] = {"engine": "postgres", "service": "p", "shared": "yes"}
    with pytest.raises(ManifestError, match="database.shared"):
        parse_manifest(data)


@pytest.mark.parametrize(
    "backup",
    [
        {"shared": True, "service": "backup"},
        {"shared": True, "command": ["x"]},
        {"shared": False},
        {"shared": "yes"},
        {"service": "backup"},
    ],
)
def test_invalid_backup_forms_are_rejected(backup: dict[str, Any]) -> None:
    data = _valid()
    data["backup"] = backup
    with pytest.raises(ManifestError, match="backup"):
        parse_manifest(data)
