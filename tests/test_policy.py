from pathlib import Path

import pytest

from deployer.policy import PolicyError, check_compose, check_env_files, validate_image_ref

APP_DIR = Path("/mnt/svd/games")
PREFIXES = ("ghcr.io/my-games-hub-br/",)
DIGEST = "sha256:" + "a" * 64
IMAGE = f"ghcr.io/my-games-hub-br/games-api@{DIGEST}"


def _config(**service):
    return {"services": {"api": {"image": IMAGE, **service}}}


def _bind(source):
    return {"volumes": [{"type": "bind", "source": source, "target": "/data"}]}


def test_clean_config_is_allowed():
    config = {
        "services": {
            "api": {
                "image": IMAGE,
                "volumes": [
                    {"type": "bind", "source": "/mnt/svd/games/data/mysql", "target": "/var/lib/mysql"},
                    {"type": "bind", "source": "/mnt/svd/games", "target": "/app"},
                ],
            },
            "db": {"image": "mysql:8.0"},
        },
        "volumes": {"cache": {}},
    }
    assert check_compose(config, APP_DIR, PREFIXES) == []


@pytest.mark.parametrize(
    ("service", "message"),
    [
        ({"privileged": True}, "service api: privileged is not allowed"),
        ({"cap_add": ["NET_ADMIN"]}, "service api: cap_add is not allowed"),
        ({"devices": ["/dev/sda:/dev/sda"]}, "service api: devices is not allowed"),
        ({"network_mode": "host"}, "service api: network_mode host is not allowed"),
        ({"pid": "host"}, "service api: pid host is not allowed"),
        ({"ipc": "host"}, "service api: ipc host is not allowed"),
        ({"userns_mode": "host"}, "service api: userns_mode host is not allowed"),
        (
            {"security_opt": ["seccomp:unconfined"]},
            "service api: security_opt seccomp:unconfined is not allowed",
        ),
        ({"volumes_from": ["other"]}, "service api: volumes_from is not allowed"),
        (_bind("/var/run/docker.sock"), "service api: mounting /var/run/docker.sock is not allowed"),
        (_bind("/mnt/svd/photos"), "service api: bind mount /mnt/svd/photos is outside /mnt/svd/games"),
        (
            _bind("/mnt/svd/games/../photos"),
            "service api: bind mount /mnt/svd/games/../photos is outside /mnt/svd/games",
        ),
        (
            _bind("/mnt/svd/gamesX/data"),
            "service api: bind mount /mnt/svd/gamesX/data is outside /mnt/svd/games",
        ),
        (
            {"image": "ghcr.io/my-games-hub-br/games-api:v1.0.0"},
            "service api: image ghcr.io/my-games-hub-br/games-api:v1.0.0 must be pinned by digest",
        ),
    ],
)
def test_violations(service, message):
    assert check_compose(_config(**service), APP_DIR, PREFIXES) == [message]


def test_third_party_tag_image_is_allowed():
    config = {"services": {"db": {"image": "mysql:8.0"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == []


def test_top_level_volume_device_outside():
    config = {
        "volumes": {
            "photos": {"driver_opts": {"type": "none", "o": "bind", "device": "/mnt/svd/photos"}}
        }
    }
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "volume photos: device /mnt/svd/photos is outside /mnt/svd/games"
    ]


def test_top_level_volume_device_inside():
    config = {
        "volumes": {
            "data": {
                "driver_opts": {"type": "none", "o": "bind", "device": "/mnt/svd/games/data/x"}
            }
        }
    }
    assert check_compose(config, APP_DIR, PREFIXES) == []


def test_top_level_volume_external():
    config = {"volumes": {"ext": {"external": True}}}
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "volume ext: external volumes are not allowed"
    ]


def test_service_build_is_rejected():
    config = {"services": {"api": {"build": {"context": "/mnt/svd/photos"}}}}
    assert check_compose(config, APP_DIR, PREFIXES) == ["service api: build is not allowed"]


def test_config_file_outside_is_rejected():
    config = {"services": {}, "configs": {"c": {"file": "/mnt/svd/photos/a.jpg"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "configs c: file /mnt/svd/photos/a.jpg is outside /mnt/svd/games"
    ]


def test_secret_file_outside_is_rejected():
    config = {"services": {}, "secrets": {"s": {"file": "/mnt/svd/photos/a.jpg"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "secrets s: file /mnt/svd/photos/a.jpg is outside /mnt/svd/games"
    ]


def test_config_file_inside_is_allowed():
    config = {"services": {}, "configs": {"c": {"file": "/mnt/svd/games/deploy/x.conf"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == []


def test_config_without_file_is_allowed():
    config = {"services": {}, "configs": {"c": {"environment": "X"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == []


def test_multiple_violations_in_sorted_service_order():
    config = {
        "services": {
            "zeta": {"image": "mysql:8.0", "privileged": True},
            "alpha": {"image": "mysql:8.0", "cap_add": ["NET_ADMIN"]},
        }
    }
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "service alpha: cap_add is not allowed",
        "service zeta: privileged is not allowed",
    ]


def test_validate_image_ref_accepts_digest_ref():
    validate_image_ref(IMAGE, PREFIXES)


def test_validate_image_ref_accepts_tag_with_digest():
    validate_image_ref(f"ghcr.io/my-games-hub-br/games-api:v1.0.0@{DIGEST}", PREFIXES)


def test_validate_image_ref_rejects_tag_only():
    with pytest.raises(PolicyError, match="pinned by digest"):
        validate_image_ref("ghcr.io/my-games-hub-br/games-api:v1.0.0", PREFIXES)


def test_validate_image_ref_rejects_uppercase():
    with pytest.raises(PolicyError):
        validate_image_ref(f"ghcr.io/Org/x@{DIGEST}", PREFIXES)


def test_validate_image_ref_rejects_other_registry():
    with pytest.raises(PolicyError, match="outside the allowed registries"):
        validate_image_ref(f"ghcr.io/other/x@{DIGEST}", PREFIXES)


# FAILS IF: unknown external network passes; external network named via name: bypasses the check; allowed network rejected; internal networks rejected
def test_unknown_external_network_is_rejected():
    config = {"networks": {"data": {"external": True, "name": "other"}}}
    assert check_compose(config, APP_DIR, PREFIXES, ("platform-data",)) == [
        "network data: external network other is not allowed"
    ]


def test_external_network_without_name_uses_key():
    config = {"networks": {"platform-data": {"external": True}}}
    assert check_compose(config, APP_DIR, PREFIXES, ("platform-data",)) == []
    assert check_compose(config, APP_DIR, PREFIXES) == [
        "network platform-data: external network platform-data is not allowed"
    ]


def test_name_not_key_decides_allowed_network():
    config = {"networks": {"platform-data": {"external": True, "name": "host"}}}
    assert check_compose(config, APP_DIR, PREFIXES, ("platform-data",)) == [
        "network platform-data: external network host is not allowed"
    ]


def test_allowed_external_network_via_name_passes():
    config = {"networks": {"data": {"external": True, "name": "platform-data"}}}
    assert check_compose(config, APP_DIR, PREFIXES, ("platform-data",)) == []


def test_internal_networks_are_not_checked():
    config = {"networks": {"default": {}, "back": None, "x": {"name": "whatever"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == []


# FAILS IF: a service can join the namespaces of a container outside its own project
# (e.g. the deployer, which holds the Docker socket), or of the host.
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("network_mode", "container:platform-deployer-1"),
        ("network_mode", "bridge"),
        ("network_mode", "service:nope"),
        ("pid", "container:platform-deployer-1"),
        ("pid", "service:nope"),
        ("ipc", "container:platform-infisical-1"),
        ("ipc", "host"),
        ("cgroup", "host"),
        ("uts", "host"),
        ("userns_mode", "private"),
    ],
)
def test_namespace_outside_the_project_is_rejected(key, value):
    config = {"services": {"api": {"image": IMAGE, key: value}, "db": {"image": "postgres:16"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == [f"service api: {key} {value} is not allowed"]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("network_mode", "none"),
        ("network_mode", "service:db"),
        ("pid", "service:db"),
        ("ipc", "private"),
        ("ipc", "shareable"),
        ("ipc", "none"),
        ("ipc", "service:db"),
        ("cgroup", "private"),
    ],
)
def test_namespace_inside_the_project_is_allowed(key, value):
    config = {"services": {"api": {"image": IMAGE, key: value}, "db": {"image": "postgres:16"}}}
    assert check_compose(config, APP_DIR, PREFIXES) == []


# FAILS IF: a symlink planted inside the app dir (a container can write one into its own bind
# mount) lets a later bind mount, volume device or env_file reach outside the app dir.
def test_bind_through_symlink_out_of_app_dir_is_rejected(tmp_path):
    app_dir = tmp_path / "games"
    (app_dir / "data").mkdir(parents=True)
    (tmp_path / "photos").mkdir()
    (app_dir / "data" / "evil").symlink_to(tmp_path / "photos")
    (app_dir / "data" / "root").symlink_to("/")
    (app_dir / "data" / "media").mkdir()
    config = {
        "services": {
            "api": {
                "image": IMAGE,
                "volumes": [
                    {"type": "bind", "source": str(app_dir / "data" / "evil"), "target": "/a"},
                    {"type": "bind", "source": str(app_dir / "data" / "root" / "etc"), "target": "/b"},
                    {"type": "bind", "source": str(app_dir / "data" / "media"), "target": "/c"},
                ],
            }
        }
    }
    assert check_compose(config, app_dir, PREFIXES) == [
        f"service api: bind mount {app_dir}/data/evil is outside {app_dir}",
        f"service api: bind mount {app_dir}/data/root/etc is outside {app_dir}",
    ]


def test_symlink_staying_inside_app_dir_is_allowed(tmp_path):
    app_dir = tmp_path / "games"
    (app_dir / ".releases" / "v1").mkdir(parents=True)
    (app_dir / "deploy").symlink_to(".releases/v1")
    config = {
        "services": {
            "api": {
                "image": IMAGE,
                "volumes": [{"type": "bind", "source": str(app_dir / "deploy" / "x.json"), "target": "/a"}],
            }
        }
    }
    assert check_compose(config, app_dir, PREFIXES) == []


# FAILS IF: an app's compose file can load another app's secrets (or any file the deployer can
# read) through env_file, which compose reads in the deployer and drops from its rendered config.
def _app_with_envs(tmp_path):
    app_dir = tmp_path / "games"
    (app_dir / ".envs" / ".production").mkdir(parents=True)
    (app_dir / ".envs" / ".production" / ".django").write_text("A=1\n")
    (app_dir / "deploy").mkdir()
    (tmp_path / "financas" / ".envs").mkdir(parents=True)
    (tmp_path / "financas" / ".envs" / ".django").write_text("SECRET=x\n")
    return app_dir


def test_env_files_of_the_real_apps_are_allowed(tmp_path):
    app_dir = _app_with_envs(tmp_path)
    raw = {
        "x-app": {"env_file": ["../.envs/.production/.django", "../.envs/.production/.postgres"]},
        "services": {
            "api": {"image": "x", "env_file": ["../.envs/.production/.django"]},
            "tailscale": {"image": "x", "env_file": "../.envs/.production/.tailscale"},
            "web": {"image": "x", "env_file": [{"path": "../.envs/.production/.web", "required": False}]},
        },
    }
    assert check_env_files(raw, app_dir / "deploy", app_dir) == []


@pytest.mark.parametrize(
    ("service", "message"),
    [
        ({"env_file": "../../financas/.envs/.django"}, "env_file ../../financas/.envs/.django is outside"),
        ({"env_file": ["/proc/1/environ"]}, "env_file /proc/1/environ is outside"),
        ({"env_file": [{"path": "/etc/shadow"}]}, "env_file /etc/shadow is outside"),
        ({"env_file": "${HOME}/.env"}, "env_file ${HOME}/.env must not use variables"),
        ({"extends": {"file": "../../financas/compose.yml", "service": "api"}}, "extends from another file is not allowed"),
    ],
)
def test_env_file_outside_the_app_dir_is_rejected(tmp_path, service, message):
    app_dir = _app_with_envs(tmp_path)
    raw = {"services": {"api": {"image": "x", **service}}}
    violations = check_env_files(raw, app_dir / "deploy", app_dir)
    assert len(violations) == 1 and violations[0].startswith(f"service api: {message}")


def test_env_file_through_symlink_is_rejected(tmp_path):
    app_dir = _app_with_envs(tmp_path)
    (app_dir / "data").mkdir()
    (app_dir / "data" / "leak").symlink_to(tmp_path / "financas" / ".envs" / ".django")
    raw = {"services": {"api": {"image": "x", "env_file": "../data/leak"}}}
    assert check_env_files(raw, app_dir / "deploy", app_dir) == [
        f"service api: env_file ../data/leak is outside {app_dir}"
    ]


def test_include_is_rejected(tmp_path):
    app_dir = _app_with_envs(tmp_path)
    raw = {"include": ["../../financas/compose.yml"], "services": {}}
    assert check_env_files(raw, app_dir / "deploy", app_dir) == ["include is not allowed"]
