import argparse
import os
import secrets
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import httpx
import yaml

PLACEHOLDER = "replace-with-project-id"
ENVIRONMENT = "prod"


class BootstrapError(Exception):
    pass


class InfisicalAdmin:
    def __init__(
        self,
        base_url: str,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=30)
        self._sleep = sleep
        self._token: str | None = None

    def _request(
        self, method: str, path: str, json: object = None, *, auth: bool = True
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._token}"} if auth and self._token else {}
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", json=json, headers=headers
            )
        except httpx.HTTPError:
            raise BootstrapError(f"{method} {path}: network error") from None
        if not response.is_success:
            message = ""
            try:
                body = response.json()
                if isinstance(body, dict):
                    message = str(body.get("message", ""))
            except ValueError:
                message = ""
            raise BootstrapError(f"{method} {path}: HTTP {response.status_code} {message}")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {}

    def wait_ready(self, timeout_seconds: float = 900) -> None:
        attempts = int(timeout_seconds / 5)
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.get(f"{self._base_url}/api/status")
                if response.is_success:
                    if attempt > 1:
                        print("Infisical is ready", flush=True)
                    return
            except httpx.HTTPError:
                pass
            if (attempt - 1) % 6 == 0:
                elapsed = (attempt - 1) * 5
                print(
                    f"waiting for Infisical to finish starting ({elapsed}s of {int(timeout_seconds)}s)",
                    flush=True,
                )
            if attempt < attempts:
                self._sleep(5)
        raise BootstrapError("infisical did not become ready")

    def bootstrap(self, email: str, password: str, organization: str) -> tuple[str, str]:
        data = self._request(
            "POST",
            "/api/v1/admin/bootstrap",
            {"email": email, "password": password, "organization": organization},
            auth=False,
        )
        try:
            token = data["identity"]["credentials"]["token"]
            org_id = data["organization"]["id"]
        except (KeyError, TypeError):
            raise BootstrapError("POST /api/v1/admin/bootstrap: unexpected response") from None
        self._token = token
        return token, org_id

    def use_token(self, token: str) -> None:
        self._token = token

    def create_project(self, name: str) -> str:
        data = self._request("POST", "/api/v1/projects", {"projectName": name})
        try:
            return data["project"]["id"]
        except (KeyError, TypeError):
            raise BootstrapError("POST /api/v1/projects: unexpected response") from None

    def create_folder(self, project_id: str, name: str) -> None:
        self._request(
            "POST",
            "/api/v2/folders",
            {"projectId": project_id, "environment": ENVIRONMENT, "name": name, "path": "/"},
        )

    def create_secrets(self, project_id: str, folder: str, values: Mapping[str, str]) -> None:
        if not values:
            return
        self._request(
            "POST",
            "/api/v4/secrets/batch",
            {
                "projectId": project_id,
                "environment": ENVIRONMENT,
                "secretPath": f"/{folder}",
                "secrets": [
                    {"secretKey": key, "secretValue": value} for key, value in values.items()
                ],
            },
        )

    def create_deployer_identity(
        self, name: str, org_id: str, project_id: str
    ) -> tuple[str, str]:
        identity = self._request(
            "POST",
            "/api/v1/identities",
            {"name": name, "organizationId": org_id, "role": "no-access"},
        )
        try:
            identity_id = identity["identity"]["id"]
        except (KeyError, TypeError):
            raise BootstrapError("POST /api/v1/identities: unexpected response") from None
        auth_path = f"/api/v1/auth/universal-auth/identities/{identity_id}"
        attached = self._request("POST", auth_path, {})
        secret = self._request(
            "POST", f"{auth_path}/client-secrets", {"description": "homelab deployer"}
        )
        self._request(
            "POST",
            f"/api/v1/projects/{project_id}/identity-memberships/{identity_id}",
            {"role": "viewer"},
        )
        try:
            return attached["identityUniversalAuth"]["clientId"], secret["clientSecret"]
        except (KeyError, TypeError):
            raise BootstrapError("universal auth: unexpected response") from None


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def set_env_lines(path: Path, values: Mapping[str, str]) -> None:
    lines: list[str] = []
    if path.exists():
        lines = [
            line
            for line in path.read_text().splitlines()
            if line.split("=", 1)[0] not in values
        ]
    lines.extend(f"{key}={value}" for key, value in values.items())
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def parse_generate(specs: Sequence[str]) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {}
    for spec in specs:
        target, separator, keys_part = spec.partition("=")
        app, colon, folder = target.partition(":")
        keys = [key.strip() for key in keys_part.split(",")]
        if not separator or not colon or not app or not folder or not all(keys):
            raise BootstrapError(f"invalid --generate {spec}")
        result.setdefault(app, {}).setdefault(folder, []).extend(keys)
    return result


def _collect_folders(
    env_dir: Path, generate: Mapping[str, list[str]]
) -> dict[str, dict[str, str]]:
    folders: dict[str, dict[str, str]] = {}
    if env_dir.exists():
        for entry in sorted(env_dir.iterdir()):
            name = entry.name
            if not name.startswith(".") or name.endswith((".tmp", ".example")):
                continue
            folders[name[1:]] = parse_env_file(entry)
    for folder, keys in generate.items():
        values = folders.setdefault(folder, {})
        for key in keys:
            if key not in values:
                values[key] = secrets.token_urlsafe(24)
    return folders


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infisical_bootstrap")
    parser.add_argument("--url", required=True)
    parser.add_argument("--apps-file", required=True, type=Path)
    parser.add_argument("--deployer-env", required=True, type=Path)
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--organization", default="homelab")
    parser.add_argument("--generate", action="append", default=[])
    return parser


def _bootstrap_app(
    admin: InfisicalAdmin,
    name: str,
    app: dict,
    org_id: str,
    generate: Mapping[str, list[str]],
    deployer_env: Path,
) -> str:
    env_dir = Path(app["app_dir"]) / ".envs" / ".production"
    project_id = admin.create_project(name)
    folders = _collect_folders(env_dir, generate)
    for folder in sorted(folders):
        admin.create_folder(project_id, folder)
        admin.create_secrets(project_id, folder, folders[folder])
    infisical = app["infisical"]
    client_id, client_secret = admin.create_deployer_identity(
        f"{name}-deployer", org_id, project_id
    )
    set_env_lines(
        deployer_env,
        {
            infisical["client_id_env"]: client_id,
            infisical["client_secret_env"]: client_secret,
        },
    )
    infisical["project_id"] = project_id
    total = sum(len(values) for values in folders.values())
    return f"{name}: project {project_id}, {len(folders)} folders, {total} secrets imported"


def run(
    argv: Sequence[str] | None = None,
    *,
    admin: InfisicalAdmin | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        generate = parse_generate(args.generate)
        data = yaml.safe_load(args.apps_file.read_text())
        pending = [
            name
            for name, app in data["apps"].items()
            if app["infisical"]["project_id"] == PLACEHOLDER
        ]
        if not pending:
            print("nothing to bootstrap")
            return 0
        admin = admin or InfisicalAdmin(args.url)
        admin.wait_ready()
        if environ.get("INFISICAL_TOKEN") and environ.get("INFISICAL_ORG_ID"):
            admin.use_token(environ["INFISICAL_TOKEN"])
            org_id = environ["INFISICAL_ORG_ID"]
        else:
            password = environ.get("INFISICAL_ADMIN_PASSWORD")
            if not password:
                raise BootstrapError("INFISICAL_ADMIN_PASSWORD is required on the first run")
            _, org_id = admin.bootstrap(args.admin_email, password, args.organization)
        for name in pending:
            line = _bootstrap_app(
                admin, name, data["apps"][name], org_id, generate.get(name, {}), args.deployer_env
            )
            args.apps_file.write_text(yaml.safe_dump(data, sort_keys=False))
            print(line)
    except BootstrapError as exc:
        print(f"bootstrap: {exc}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
