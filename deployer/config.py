import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

_APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DIGITS = re.compile(r"^\d+$")
_IMAGE_PREFIX = re.compile(r"^ghcr\.io/[a-z0-9._-]+/$")

_TOP_KEYS = ("infisical_url", "ntfy_url_env", "allowed_workflow_refs", "admins", "apps")
_APP_KEYS = ("repository", "repository_id", "app_dir", "image_prefixes", "infisical")
_INFISICAL_KEYS = ("project_id", "environment", "client_id_env", "client_secret_env")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class InfisicalConfig:
    project_id: str
    environment: str
    client_id_env: str
    client_secret_env: str


@dataclass(frozen=True)
class AppConfig:
    name: str
    repository: str
    repository_id: str
    app_dir: Path
    image_prefixes: tuple[str, ...]
    infisical: InfisicalConfig


@dataclass(frozen=True)
class PlatformConfig:
    apps: Mapping[str, AppConfig]
    allowed_workflow_refs: tuple[str, ...]
    admins: frozenset[str]
    infisical_url: str
    ntfy_url_env: str


def _check_keys(data: Mapping[object, object], allowed: tuple[str, ...], prefix: str) -> None:
    for key in allowed:
        if key not in data:
            raise ConfigError(f"{prefix}{key}: is required")
    for key in data:
        if key not in allowed:
            raise ConfigError(f"{prefix}{key}: unknown key")


def _non_empty_str(value: object, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{path}: must be a non-empty string")
    return value


def _str_list(value: object, path: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(f"{path}: must be a list")
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{path}[{index}]: must be a string")
    return value


def _parse_infisical(value: object, path: str) -> InfisicalConfig:
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a mapping")
    _check_keys(value, _INFISICAL_KEYS, f"{path}.")
    return InfisicalConfig(
        project_id=_non_empty_str(value["project_id"], f"{path}.project_id"),
        environment=_non_empty_str(value["environment"], f"{path}.environment"),
        client_id_env=_non_empty_str(value["client_id_env"], f"{path}.client_id_env"),
        client_secret_env=_non_empty_str(value["client_secret_env"], f"{path}.client_secret_env"),
    )


def _parse_app(name: str, value: object) -> AppConfig:
    path = f"apps.{name}"
    if not isinstance(value, dict):
        raise ConfigError(f"{path}: must be a mapping")
    _check_keys(value, _APP_KEYS, f"{path}.")

    repository = value["repository"]
    if not isinstance(repository, str) or not _REPOSITORY.match(repository):
        raise ConfigError(f"{path}.repository: must look like owner/name")

    raw_id = value["repository_id"]
    if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
        raise ConfigError(f"{path}.repository_id: must be digits")
    repository_id = str(raw_id)
    if not _DIGITS.match(repository_id):
        raise ConfigError(f"{path}.repository_id: must be digits")

    raw_dir = value["app_dir"]
    if not isinstance(raw_dir, str) or not os.path.isabs(raw_dir):
        raise ConfigError(f"{path}.app_dir: must be an absolute path")
    app_dir = Path(os.path.normpath(raw_dir))
    if app_dir == Path("/"):
        raise ConfigError(f"{path}.app_dir: must be an absolute path other than /")

    prefixes = value["image_prefixes"]
    if not isinstance(prefixes, list) or not prefixes:
        raise ConfigError(f"{path}.image_prefixes: must be a non-empty list")
    for index, prefix in enumerate(prefixes):
        if not isinstance(prefix, str) or not _IMAGE_PREFIX.match(prefix):
            raise ConfigError(f"{path}.image_prefixes[{index}]: must look like ghcr.io/<org>/")

    return AppConfig(
        name=name,
        repository=repository,
        repository_id=repository_id,
        app_dir=app_dir,
        image_prefixes=tuple(prefixes),
        infisical=_parse_infisical(value["infisical"], f"{path}.infisical"),
    )


def _overlaps(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _check_overlaps(apps: Mapping[str, AppConfig]) -> None:
    seen: list[AppConfig] = []
    for app in apps.values():
        for earlier in seen:
            if _overlaps(earlier.app_dir, app.app_dir):
                raise ConfigError(
                    f"apps.{app.name}.app_dir: overlaps apps.{earlier.name}.app_dir"
                )
        seen.append(app)


def parse_config(data: object) -> PlatformConfig:
    if not isinstance(data, dict):
        raise ConfigError("config: must be a mapping")
    _check_keys(data, _TOP_KEYS, "")

    infisical_url = data["infisical_url"]
    if not isinstance(infisical_url, str) or not infisical_url.startswith(("https://", "http://")):
        raise ConfigError("infisical_url: must start with https:// or http://")

    ntfy_url_env = _non_empty_str(data["ntfy_url_env"], "ntfy_url_env")

    refs = _str_list(data["allowed_workflow_refs"], "allowed_workflow_refs")
    if not refs:
        raise ConfigError("allowed_workflow_refs: must not be empty")
    for index, ref in enumerate(refs):
        if not ref.endswith("@"):
            raise ConfigError(f"allowed_workflow_refs[{index}]: must end with @")

    admins = _str_list(data["admins"], "admins")

    raw_apps = data["apps"]
    if not isinstance(raw_apps, dict) or not raw_apps:
        raise ConfigError("apps: must be a non-empty mapping")
    apps: dict[str, AppConfig] = {}
    for name, value in raw_apps.items():
        if not isinstance(name, str) or not _APP_NAME.match(name):
            raise ConfigError(f"apps.{name}: invalid app name")
        apps[name] = _parse_app(name, value)
    _check_overlaps(apps)

    return PlatformConfig(
        apps=apps,
        allowed_workflow_refs=tuple(refs),
        admins=frozenset(admins),
        infisical_url=infisical_url.rstrip("/"),
        ntfy_url_env=ntfy_url_env,
    )


def load_config(path: Path) -> PlatformConfig:
    if not path.exists():
        raise ConfigError(f"{path}: not found")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: invalid YAML") from error
    return parse_config(data)
