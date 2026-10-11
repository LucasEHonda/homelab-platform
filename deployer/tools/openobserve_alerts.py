"""Applies the OpenObserve alerts in deploy/openobserve/alerts.yml.

Creates or updates the ntfy template and destination, creates or updates every alert in the
file, and deletes the alerts it manages that are no longer in the file. Alerts made by hand in
the UI are never deleted.
"""

import argparse
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from deployer.tools.infisical_bootstrap import parse_env_file

ORG = "default"
STREAM = "default"
TEMPLATE_NAME = "ntfy"
DESTINATION_NAME = "ntfy"
MANAGED_TAG = "managed:homelab-platform"
TEMPLATE_BODY = "{alert_name}\n{rows}"
NTFY_HEADERS = {"Title": "OpenObserve alert", "Tags": "warning"}
ALERT_FIELDS = (
    "name",
    "description",
    "sql",
    "period",
    "frequency",
    "operator",
    "threshold",
    "silence",
    "row_template",
)
OPERATORS = ("=", "!=", ">", ">=", "<", "<=")
NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_STRING_FIELDS = ("name", "description", "sql", "row_template")
_MINIMUMS = {"period": 1, "frequency": 1, "threshold": 0, "silence": 0}


class AlertsError(Exception):
    pass


@dataclass(frozen=True)
class AlertSpec:
    name: str
    description: str
    sql: str
    period: int
    frequency: int
    operator: str
    threshold: int
    silence: int
    row_template: str


@dataclass(frozen=True)
class ExistingAlert:
    alert_id: str
    name: str
    tags: tuple[str, ...]


@dataclass(frozen=True)
class AlertPlan:
    create: tuple[AlertSpec, ...]
    update: tuple[tuple[str, AlertSpec], ...]  # (alert_id, spec)
    delete: tuple[str, ...]  # alert_ids


def _check_keys(index: int, item: object) -> dict:
    if not isinstance(item, dict):
        raise AlertsError(f"alerts file: alert {index} must be a mapping")
    missing = [field for field in ALERT_FIELDS if field not in item]
    if missing:
        raise AlertsError(f"alerts file: alert {index} lacks {missing}")
    unknown = sorted(key for key in item if key not in ALERT_FIELDS)
    if unknown:
        raise AlertsError(f"alerts file: alert {index} has unknown keys {unknown}")
    return item


def _check_strings(index: int, item: dict) -> None:
    for field in _STRING_FIELDS:
        value = item[field]
        if not isinstance(value, str) or not value:
            raise AlertsError(
                f"alerts file: alert {index} field {field} must be a non-empty string"
            )


def _check_name(index: int, item: dict) -> None:
    name = item["name"]
    if not NAME_PATTERN.fullmatch(name):
        raise AlertsError(
            f"alerts file: alert {index} name {name!r} must be lowercase words joined by '-'"
        )


def _check_numbers(index: int, item: dict) -> None:
    value = item["operator"]
    if value not in OPERATORS:
        raise AlertsError(
            f"alerts file: alert {index} operator {value!r} is not one of {OPERATORS}"
        )
    for field, minimum in _MINIMUMS.items():
        number = item[field]
        # bool is an int subclass, but `period: true` is never intended
        if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
            raise AlertsError(
                f"alerts file: alert {index} field {field} must be an integer >= {minimum}"
            )


def parse_alerts(text: str) -> list[AlertSpec]:
    data = yaml.safe_load(text)
    items = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise AlertsError("alerts file: 'alerts' must be a non-empty list")
    specs: list[AlertSpec] = []
    for index, raw in enumerate(items):
        item = _check_keys(index, raw)
        _check_strings(index, item)
        _check_name(index, item)
        _check_numbers(index, item)
        specs.append(AlertSpec(**{field: item[field] for field in ALERT_FIELDS}))
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AlertsError(f"alerts file: duplicate alert names {duplicates}")
    return specs


def alert_payload(spec: AlertSpec) -> dict:
    return {
        "name": spec.name,
        "description": spec.description,
        "stream_type": "traces",
        "stream_name": STREAM,
        "is_real_time": False,
        "enabled": True,
        "tags": [MANAGED_TAG],
        "query_condition": {"type": "sql", "sql": spec.sql},
        "trigger_condition": {
            "period": spec.period,
            "operator": spec.operator,
            "threshold": spec.threshold,
            "frequency": spec.frequency,
            "frequency_type": "minutes",
            "silence": spec.silence,
        },
        "row_template": spec.row_template,
        "destinations": [DESTINATION_NAME],
    }


def plan_changes(specs: Sequence[AlertSpec], existing: Sequence[ExistingAlert]) -> AlertPlan:
    by_name: dict[str, ExistingAlert] = {}
    for alert in existing:
        by_name.setdefault(alert.name, alert)
    spec_names = {spec.name for spec in specs}
    return AlertPlan(
        create=tuple(spec for spec in specs if spec.name not in by_name),
        update=tuple((by_name[spec.name].alert_id, spec) for spec in specs if spec.name in by_name),
        delete=tuple(
            alert.alert_id
            for alert in existing
            if MANAGED_TAG in alert.tags and alert.name not in spec_names
        ),
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return ""
    return str(body.get("message", "")) if isinstance(body, dict) else ""


class OpenObserveAdmin:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        client: httpx.Client | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = (email, password)
        self._client = client or httpx.Client(timeout=30, auth=self._auth)
        self._sleep = sleep

    def _request(
        self, method: str, path: str, json: object = None, *, allow_404: bool = False
    ) -> httpx.Response:
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", json=json, auth=self._auth
            )
        except httpx.HTTPError:
            raise AlertsError(f"{method} {path}: network error") from None
        if response.status_code == 404 and allow_404:
            return response
        if not response.is_success:
            message = _error_message(response)
            raise AlertsError(f"{method} {path}: HTTP {response.status_code} {message}")
        return response

    def wait_ready(self, timeout_seconds: float = 300) -> None:
        attempts = int(timeout_seconds / 5)
        for attempt in range(1, attempts + 1):
            if self._is_ready():
                if attempt > 1:
                    print("OpenObserve is ready", flush=True)
                return
            if (attempt - 1) % 6 == 0:
                elapsed = (attempt - 1) * 5
                print(
                    f"waiting for OpenObserve to finish starting ({elapsed}s of {int(timeout_seconds)}s)",
                    flush=True,
                )
            if attempt < attempts:
                self._sleep(5)
        raise AlertsError("OpenObserve did not become ready")

    def _is_ready(self) -> bool:
        try:
            response = self._client.get(f"{self._base_url}/healthz", auth=self._auth)
        except httpx.HTTPError:
            return False
        return response.is_success

    def _upsert(self, collection: str, name: str, body: dict) -> None:
        response = self._request("PUT", f"{collection}/{name}", body, allow_404=True)
        if response.status_code == 404:
            self._request("POST", collection, body)

    def put_template(self, name: str, body: str) -> None:
        payload = {"name": name, "type": "http", "title": "", "body": body}
        self._upsert(f"/api/{ORG}/alerts/templates", name, payload)

    def put_destination(
        self, name: str, url: str, template: str, headers: Mapping[str, str]
    ) -> None:
        payload = {
            "name": name,
            "type": "http",
            "url": url,
            "method": "post",
            "template": template,
            "headers": headers,
        }
        self._upsert(f"/api/{ORG}/alerts/destinations", name, payload)

    def list_alerts(self) -> list[ExistingAlert]:
        entries = self._request("GET", f"/api/v2/{ORG}/alerts").json()["list"]
        return [
            ExistingAlert(entry["alert_id"], entry["name"], tuple(entry.get("tags") or ()))
            for entry in entries
        ]

    def create_alert(self, payload: dict) -> None:
        self._request("POST", f"/api/v2/{ORG}/alerts", payload)

    def update_alert(self, alert_id: str, payload: dict) -> None:
        self._request("PUT", f"/api/v2/{ORG}/alerts/{alert_id}", payload)

    def delete_alert(self, alert_id: str) -> None:
        self._request("DELETE", f"/api/v2/{ORG}/alerts/{alert_id}")


def apply_alerts(admin: OpenObserveAdmin, specs: Sequence[AlertSpec], ntfy_url: str) -> AlertPlan:
    admin.put_template(TEMPLATE_NAME, TEMPLATE_BODY)
    admin.put_destination(DESTINATION_NAME, ntfy_url, TEMPLATE_NAME, NTFY_HEADERS)
    plan = plan_changes(specs, admin.list_alerts())
    for spec in plan.create:
        admin.create_alert(alert_payload(spec))
    for alert_id, spec in plan.update:
        admin.update_alert(alert_id, alert_payload(spec))
    for alert_id in plan.delete:
        admin.delete_alert(alert_id)
    return plan


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openobserve_alerts")
    parser.add_argument("--url", required=True)
    parser.add_argument("--alerts-file", required=True, type=Path)
    parser.add_argument("--openobserve-env", required=True, type=Path)
    parser.add_argument("--deployer-env", required=True, type=Path)
    return parser


def _require(values: Mapping[str, str], key: str, path: Path) -> str:
    value = values.get(key)
    if not value:
        raise AlertsError(f"{key} is missing from {path}")
    return value


def run(argv: Sequence[str] | None = None, *, admin: OpenObserveAdmin | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        specs = parse_alerts(args.alerts_file.read_text())
        observe = parse_env_file(args.openobserve_env)
        deployer = parse_env_file(args.deployer_env)
        email = _require(observe, "ZO_ROOT_USER_EMAIL", args.openobserve_env)
        password = _require(observe, "ZO_ROOT_USER_PASSWORD", args.openobserve_env)
        ntfy_url = _require(deployer, "NTFY_URL", args.deployer_env)
        admin = admin or OpenObserveAdmin(args.url, email, password)
        admin.wait_ready()
        plan = apply_alerts(admin, specs, ntfy_url)
    except AlertsError as exc:
        print(f"openobserve_alerts: {exc}", file=sys.stderr)
        return 1
    print(
        f"openobserve alerts: {len(plan.create)} created, {len(plan.update)} updated, "
        f"{len(plan.delete)} deleted"
    )
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
