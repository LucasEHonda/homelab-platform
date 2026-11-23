import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


class PolicyError(Exception):
    pass


IMAGE_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")

# Namespace settings are allow-listed: "host" or "container:<name>" would let a service join the
# host or a container of another stack (the deployer holds the Docker socket). "service:<name>"
# is allowed only for a service of the same compose file.
_NAMESPACE_VALUES: dict[str, frozenset[str]] = {
    "network_mode": frozenset({"none"}),
    "pid": frozenset(),
    "ipc": frozenset({"private", "shareable", "none"}),
    "cgroup": frozenset({"private"}),
    "uts": frozenset(),
    "userns_mode": frozenset(),
}
_SERVICE_SHARABLE = frozenset({"network_mode", "pid", "ipc"})


def validate_image_ref(ref: str, prefixes: Sequence[str]) -> None:
    if IMAGE_DIGEST_REF.fullmatch(ref) is None:
        raise PolicyError(f"image {ref} must be pinned by digest")
    if not ref.startswith(tuple(prefixes)):
        raise PolicyError(f"image {ref} is outside the allowed registries")


def _is_own_service(key: str, value: object, services: Mapping[str, Any]) -> bool:
    if key not in _SERVICE_SHARABLE or not isinstance(value, str):
        return False
    prefix, _, target = value.partition(":")
    return prefix == "service" and target in services


def _inside(path: str, app_dir: Path) -> bool:
    p = Path(os.path.normpath(path))
    if not p.is_absolute():
        return False
    # Docker follows symlinks in a bind source on the host, and an app's containers can write
    # symlinks into their own mounts, so the check is on where the path really leads. The
    # deployer mounts each app dir at its host path, so the resolution matches the host's.
    real, root = Path(os.path.realpath(p)), Path(os.path.realpath(app_dir))
    return real == root or root in real.parents


def check_compose(
    config: Mapping[str, Any],
    app_dir: Path,
    image_prefixes: Sequence[str],
    shared_networks: Sequence[str] = (),
) -> list[str]:
    violations: list[str] = []
    services = config.get("services") or {}
    for name in sorted(services):
        service = services[name] or {}
        if service.get("privileged"):
            violations.append(f"service {name}: privileged is not allowed")
        if service.get("cap_add"):
            violations.append(f"service {name}: cap_add is not allowed")
        if service.get("devices"):
            violations.append(f"service {name}: devices is not allowed")
        for key, allowed in _NAMESPACE_VALUES.items():
            value = service.get(key)
            if value is None or value in allowed or _is_own_service(key, value, services):
                continue
            violations.append(f"service {name}: {key} {value} is not allowed")
        for item in service.get("security_opt") or []:
            if "unconfined" in item:
                violations.append(f"service {name}: security_opt {item} is not allowed")
        if service.get("volumes_from"):
            violations.append(f"service {name}: volumes_from is not allowed")
        if service.get("build"):
            violations.append(f"service {name}: build is not allowed")
        for item in service.get("volumes") or []:
            if item.get("type") == "bind":
                source = item.get("source", "")
                if "docker.sock" in source:
                    violations.append(f"service {name}: mounting {source} is not allowed")
                elif not _inside(source, app_dir):
                    violations.append(f"service {name}: bind mount {source} is outside {app_dir}")
        image = service.get("image", "")
        if image.startswith(tuple(image_prefixes)) and DIGEST_SUFFIX.search(image) is None:
            violations.append(f"service {name}: image {image} must be pinned by digest")
    networks = config.get("networks") or {}
    for key in sorted(networks):
        network = networks[key] or {}
        if network.get("external"):
            effective = network.get("name") or key
            if effective not in shared_networks:
                violations.append(f"network {key}: external network {effective} is not allowed")
    volumes = config.get("volumes") or {}
    for vname in sorted(volumes):
        vol = volumes[vname] or {}
        if vol.get("external"):
            violations.append(f"volume {vname}: external volumes are not allowed")
        device = (vol.get("driver_opts") or {}).get("device")
        if device:
            if "docker.sock" in device or not _inside(device, app_dir):
                violations.append(f"volume {vname}: device {device} is outside {app_dir}")
    for section in ("configs", "secrets"):
        entries = config.get(section) or {}
        for entry_name in sorted(entries):
            source = (entries[entry_name] or {}).get("file")
            if source and ("docker.sock" in source or not _inside(source, app_dir)):
                violations.append(f"{section} {entry_name}: file {source} is outside {app_dir}")
    return violations


def _env_file_paths(value: object) -> list[str]:
    items = value if isinstance(value, list) else [value]
    paths: list[str] = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("path")
        paths.append(str(item))
    return paths


def check_env_files(raw: Mapping[str, Any], project_dir: Path, app_dir: Path) -> list[str]:
    """Checks the env_file entries of the compose file as written.

    Compose reads env files inside the deployer, which can read every app dir, and the
    rendered config no longer lists them, so they are checked before rendering. include and
    extends from another file would bring in services this check never sees.
    """
    violations: list[str] = []
    if raw.get("include"):
        violations.append("include is not allowed")
    services = raw.get("services") or {}
    for name in sorted(services):
        service = services[name] or {}
        extends = service.get("extends")
        if isinstance(extends, dict) and extends.get("file"):
            violations.append(f"service {name}: extends from another file is not allowed")
        if "env_file" not in service:
            continue
        for path in _env_file_paths(service["env_file"]):
            if "$" in path:
                violations.append(f"service {name}: env_file {path} must not use variables")
            elif not _inside(os.path.join(project_dir, path), app_dir):
                violations.append(f"service {name}: env_file {path} is outside {app_dir}")
    return violations
