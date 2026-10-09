import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import yaml

_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_COMPOSE_FILE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
_IMAGE_VAR = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_FOLDER = re.compile(r"^/[A-Za-z0-9_/-]*$")

_KEYS = (
    "project",
    "compose",
    "image_vars",
    "env_files",
    "backup",
    "migrate_service",
    "health_service",
    "health_timeout_seconds",
    "database",
)
_REQUIRED = ("project", "compose", "image_vars", "migrate_service", "health_service")


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class BackupStep:
    service: str
    command: tuple[str, ...]


@dataclass(frozen=True)
class DatabaseSpec:
    engine: Literal["mysql", "postgres"]
    service: str


@dataclass(frozen=True)
class Manifest:
    project: str
    compose: str
    image_vars: tuple[str, ...]
    env_files: Mapping[str, str]
    backup: BackupStep | None
    migrate_service: str
    health_service: str
    health_timeout_seconds: int
    database: DatabaseSpec | None


def _matching_str(value: object, pattern: re.Pattern[str], path: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ManifestError(f"{path}: must match {pattern.pattern}")
    return value


def _exact_keys(data: Mapping[object, object], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if key not in data:
            raise ManifestError(f"{path}.{key}: is required")
    for key in data:
        if key not in keys:
            raise ManifestError(f"{path}.{key}: unknown key")


def _parse_image_vars(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ManifestError("image_vars: must be a non-empty list")
    seen: set[str] = set()
    for item in value:
        _matching_str(item, _IMAGE_VAR, "image_vars")
        if item in seen:
            raise ManifestError(f"image_vars: duplicate {item}")
        seen.add(item)
    return tuple(value)


def _parse_env_files(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ManifestError("env_files: must be a mapping")
    result: dict[str, str] = {}
    for key, folder in value.items():
        if not isinstance(key, str) or not key:
            raise ManifestError(f"env_files.{key}: must be a non-empty string")
        if os.path.isabs(key):
            raise ManifestError(f"env_files.{key}: must be relative")
        if not isinstance(folder, str) or not _ENV_FOLDER.match(folder):
            raise ManifestError(f"env_files.{key}: invalid folder")
        result[key] = folder
    return result


def _parse_backup(value: object) -> BackupStep:
    if not isinstance(value, dict):
        raise ManifestError("backup: must be a mapping")
    _exact_keys(value, ("service", "command"), "backup")
    service = value["service"]
    if not isinstance(service, str) or not service:
        raise ManifestError("backup.service: must be a non-empty string")
    command = value["command"]
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ManifestError("backup.command: must be a non-empty list of strings")
    return BackupStep(service=service, command=tuple(command))


def _parse_timeout(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 1800:
        raise ManifestError("health_timeout_seconds: must be an integer between 30 and 1800")
    return value


def _parse_database(value: object) -> DatabaseSpec:
    if not isinstance(value, dict):
        raise ManifestError("database: must be a mapping")
    _exact_keys(value, ("engine", "service"), "database")
    engine = value["engine"]
    if engine == "mysql":
        spec_engine: Literal["mysql", "postgres"] = "mysql"
    elif engine == "postgres":
        spec_engine = "postgres"
    else:
        raise ManifestError("database.engine: must be mysql or postgres")
    service = _matching_str(value["service"], _SERVICE_NAME, "database.service")
    return DatabaseSpec(engine=spec_engine, service=service)


def parse_manifest(data: object) -> Manifest:
    if not isinstance(data, dict):
        raise ManifestError("manifest: must be a mapping")
    for key in data:
        if key not in _KEYS:
            raise ManifestError(f"{key}: unknown key")
    for key in _REQUIRED:
        if key not in data:
            raise ManifestError(f"{key}: is required")

    return Manifest(
        project=_matching_str(data["project"], _SERVICE_NAME, "project"),
        compose=_matching_str(data["compose"], _COMPOSE_FILE, "compose"),
        image_vars=_parse_image_vars(data["image_vars"]),
        env_files=_parse_env_files(data.get("env_files", {})),
        backup=_parse_backup(data["backup"]) if data.get("backup") is not None else None,
        migrate_service=_matching_str(data["migrate_service"], _SERVICE_NAME, "migrate_service"),
        health_service=_matching_str(data["health_service"], _SERVICE_NAME, "health_service"),
        health_timeout_seconds=_parse_timeout(data.get("health_timeout_seconds", 300)),
        database=_parse_database(data["database"]) if data.get("database") is not None else None,
    )


def load_manifest(path: Path) -> Manifest:
    if not path.exists():
        raise ManifestError(f"{path}: not found")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ManifestError(f"{path}: invalid YAML") from error
    return parse_manifest(data)


def resolve_in_app_dir(app_dir: Path, relative: str) -> Path:
    result = Path(os.path.normpath(app_dir / "deploy" / relative))
    forbidden = (app_dir / "deploy", app_dir / ".releases")
    if (
        os.path.isabs(relative)
        or app_dir not in result.parents
        or any(result == root or root in result.parents for root in forbidden)
    ):
        raise ManifestError(
            f"env_files.{relative}: must point inside {app_dir} and outside deploy/ and .releases/"
        )
    return result


def read_template_keys(path: Path) -> list[str]:
    keys: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise ManifestError(f"{path.name}: invalid line {stripped!r}")
        key = stripped.split("=", 1)[0].strip()
        if key not in keys:
            keys.append(key)
    return keys
