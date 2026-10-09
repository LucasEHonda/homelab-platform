import re
from collections.abc import Mapping, Sequence

import httpx

ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class SecretsError(Exception):
    pass


class MissingSecretsError(SecretsError):
    def __init__(self, missing: Sequence[str]) -> None:
        self.missing = list(missing)
        super().__init__("missing secrets: " + ", ".join(self.missing))


class InfisicalClient:
    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=15)

    def login(self, client_id: str, client_secret: str) -> str:
        try:
            response = self._client.post(
                f"{self._base_url}/api/v1/auth/universal-auth/login",
                json={"clientId": client_id, "clientSecret": client_secret},
            )
        except httpx.HTTPError:
            raise SecretsError("infisical login failed: network error") from None
        if not response.is_success:
            raise SecretsError(f"infisical login failed: HTTP {response.status_code}")
        try:
            return response.json()["accessToken"]
        except (KeyError, ValueError, TypeError):
            raise SecretsError("infisical login failed: no access token") from None

    def list_secrets(
        self, token: str, project_id: str, environment: str, folder: str
    ) -> dict[str, str]:
        try:
            response = self._client.get(
                f"{self._base_url}/api/v3/secrets/raw",
                params={
                    "workspaceId": project_id,
                    "environment": environment,
                    "secretPath": folder,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError:
            raise SecretsError(
                f"infisical list secrets failed for {folder}: network error"
            ) from None
        if not response.is_success:
            raise SecretsError(
                f"infisical list secrets failed for {folder}: HTTP {response.status_code}"
            )
        return {
            item["secretKey"]: item["secretValue"]
            for item in response.json()["secrets"]
        }


def ensure_required(
    values: Mapping[str, str], required_keys: Sequence[str], folder: str
) -> None:
    missing = [f"{folder}/{key}" for key in required_keys if key not in values]
    if missing:
        raise MissingSecretsError(missing)


def render_env_file(values: Mapping[str, str]) -> str:
    lines = []
    for key in sorted(values):
        if not ENV_KEY.fullmatch(key):
            raise SecretsError(f"invalid secret name {key}")
        value = values[key]
        if "\n" in value or "\r" in value or "'" in value:
            raise SecretsError(f"secret {key} has an unsupported character")
        lines.append(f"{key}='{value}'")
    return "\n".join(lines) + "\n" if lines else ""
