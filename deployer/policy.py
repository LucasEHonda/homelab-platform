import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence


class PolicyError(Exception):
    pass


IMAGE_DIGEST_REF = re.compile(r"^[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$")
DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")


def validate_image_ref(ref: str, prefixes: Sequence[str]) -> None:
    if IMAGE_DIGEST_REF.fullmatch(ref) is None:
        raise PolicyError(f"image {ref} must be pinned by digest")
    if not ref.startswith(tuple(prefixes)):
        raise PolicyError(f"image {ref} is outside the allowed registries")


def _inside(path: str, app_dir: Path) -> bool:
    p = Path(os.path.normpath(path))
    return p.is_absolute() and (p == app_dir or app_dir in p.parents)


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
        for key in ("network_mode", "pid", "ipc", "userns_mode"):
            if service.get(key) == "host":
                violations.append(f"service {name}: {key} host is not allowed")
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
