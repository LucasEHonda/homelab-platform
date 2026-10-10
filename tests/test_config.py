import copy
from pathlib import Path
from typing import Any, Callable

import pytest

from deployer.config import ConfigError, load_config, parse_config

VALID: dict[str, Any] = {
    "infisical_url": "https://secrets.example.ts.net",
    "ntfy_url_env": "NTFY_URL",
    "allowed_workflow_refs": ["LucasEHonda/homelab-platform/.github/workflows/deploy.yml@"],
    "admins": ["owner@example.com"],
    "apps": {
        "games": {
            "repository": "my-games-hub-br/my-games-hub",
            "repository_id": "123456",
            "app_dir": "/mnt/svd/games",
            "image_prefixes": ["ghcr.io/my-games-hub-br/"],
            "infisical": {
                "project_id": "abc",
                "environment": "prod",
                "client_id_env": "GAMES_INFISICAL_CLIENT_ID",
                "client_secret_env": "GAMES_INFISICAL_CLIENT_SECRET",
            },
        }
    },
}


def _valid() -> dict[str, Any]:
    return copy.deepcopy(VALID)


def test_valid_config_parses() -> None:
    config = parse_config(_valid())
    assert config.infisical_url == "https://secrets.example.ts.net"
    assert config.ntfy_url_env == "NTFY_URL"
    assert config.allowed_workflow_refs == (
        "LucasEHonda/homelab-platform/.github/workflows/deploy.yml@",
    )
    assert isinstance(config.allowed_workflow_refs, tuple)
    assert config.admins == frozenset({"owner@example.com"})
    assert isinstance(config.admins, frozenset)
    app = config.apps["games"]
    assert app.name == "games"
    assert app.repository == "my-games-hub-br/my-games-hub"
    assert app.repository_id == "123456"
    assert app.app_dir == Path("/mnt/svd/games")
    assert isinstance(app.app_dir, Path)
    assert app.image_prefixes == ("ghcr.io/my-games-hub-br/",)
    assert isinstance(app.image_prefixes, tuple)
    assert app.infisical.project_id == "abc"
    assert app.infisical.environment == "prod"
    assert app.infisical.client_id_env == "GAMES_INFISICAL_CLIENT_ID"
    assert app.infisical.client_secret_env == "GAMES_INFISICAL_CLIENT_SECRET"


def test_infisical_url_trailing_slash_is_stripped() -> None:
    data = _valid()
    data["infisical_url"] = "https://x/"
    assert parse_config(data).infisical_url == "https://x"


def test_integer_repository_id_is_stringified() -> None:
    data = _valid()
    data["apps"]["games"]["repository_id"] = 123456
    assert parse_config(data).apps["games"].repository_id == "123456"


def _del_apps(data: dict[str, Any]) -> None:
    del data["apps"]


def _unknown_top(data: dict[str, Any]) -> None:
    data["extra"] = 1


def _bad_name(data: dict[str, Any]) -> None:
    data["apps"]["Games"] = data["apps"].pop("games")


def _bad_repository(data: dict[str, Any]) -> None:
    data["apps"]["games"]["repository"] = "bad"


def _bad_repo_id(data: dict[str, Any]) -> None:
    data["apps"]["games"]["repository_id"] = "12a"


def _bool_repo_id(data: dict[str, Any]) -> None:
    data["apps"]["games"]["repository_id"] = True


def _relative_dir(data: dict[str, Any]) -> None:
    data["apps"]["games"]["app_dir"] = "relative/x"


def _root_dir(data: dict[str, Any]) -> None:
    data["apps"]["games"]["app_dir"] = "/"


def _upper_prefix(data: dict[str, Any]) -> None:
    data["apps"]["games"]["image_prefixes"] = ["ghcr.io/Org/"]


def _no_slash_prefix(data: dict[str, Any]) -> None:
    data["apps"]["games"]["image_prefixes"] = ["ghcr.io/org"]


def _ref_without_at(data: dict[str, Any]) -> None:
    data["allowed_workflow_refs"] = ["a/b/c.yml"]


def _empty_refs(data: dict[str, Any]) -> None:
    data["allowed_workflow_refs"] = []


def _missing_project_id(data: dict[str, Any]) -> None:
    del data["apps"]["games"]["infisical"]["project_id"]


def _unknown_app_key(data: dict[str, Any]) -> None:
    data["apps"]["games"]["extra"] = 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_del_apps, "apps: is required"),
        (_unknown_top, "extra: unknown key"),
        (_bad_name, "apps.Games"),
        (_bad_repository, "apps.games.repository"),
        (_bad_repo_id, "apps.games.repository_id"),
        (_bool_repo_id, "apps.games.repository_id"),
        (_relative_dir, "apps.games.app_dir: must be an absolute path"),
        (_root_dir, "apps.games.app_dir"),
        (_upper_prefix, "apps.games.image_prefixes"),
        (_no_slash_prefix, "apps.games.image_prefixes"),
        (_ref_without_at, "allowed_workflow_refs"),
        (_empty_refs, "allowed_workflow_refs"),
        (_missing_project_id, "apps.games.infisical.project_id: is required"),
        (_unknown_app_key, "apps.games.extra: unknown key"),
    ],
)
def test_invalid_config_raises(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data = _valid()
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        parse_config(data)


def test_overlapping_app_dirs_raise() -> None:
    data = _valid()
    second = copy.deepcopy(data["apps"]["games"])
    second["app_dir"] = "/mnt/svd/games/sub"
    data["apps"]["other"] = second
    with pytest.raises(ConfigError, match=r"apps\.other\.app_dir: overlaps apps\.games\.app_dir"):
        parse_config(data)


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yml")


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yml"
    path.write_text("a: [b")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(path)


# FAILS IF: shared_networks default not empty; shared_networks value dropped; non-list shared_networks accepted
def test_shared_networks_defaults_to_empty() -> None:
    assert parse_config(_valid()).shared_networks == ()


def test_shared_networks_are_parsed() -> None:
    data = _valid()
    data["shared_networks"] = ["platform-data"]
    assert parse_config(data).shared_networks == ("platform-data",)


def test_shared_networks_must_be_list_of_strings() -> None:
    data = _valid()
    data["shared_networks"] = "platform-data"
    with pytest.raises(ConfigError, match="shared_networks"):
        parse_config(data)
