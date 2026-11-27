"""Applies the OpenObserve realtime pipelines in deploy/openobserve/pipelines.yml.

Each pipeline reads a traces stream as it is ingested and moves the spans whose column contains a
text to another traces stream. Every other span, including the ones without that column, goes
back to the source stream. Pipelines are matched by name: missing ones are created, existing ones
are updated. Pipelines made by hand in the UI are never touched, and removing an entry from the
file does not delete its pipeline (delete it in the UI).
"""

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from deployer.tools.infisical_bootstrap import parse_env_file
from deployer.tools.openobserve_alerts import (
    NAME_PATTERN,
    ORG,
    AlertsError,
    OpenObserveAdmin,
)

PIPELINE_FIELDS = ("name", "description", "source", "column", "contains", "destination")


@dataclass(frozen=True)
class PipelineSpec:
    name: str
    description: str
    source: str
    column: str
    contains: str
    destination: str


def parse_pipelines(text: str) -> list[PipelineSpec]:
    data = yaml.safe_load(text)
    items = data.get("pipelines") if isinstance(data, dict) else None
    if not isinstance(items, list) or not items:
        raise AlertsError("pipelines file: 'pipelines' must be a non-empty list")
    specs = [_parse_one(index, item) for index, item in enumerate(items)]
    names = [spec.name for spec in specs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AlertsError(f"pipelines file: duplicate pipeline names {duplicates}")
    return specs


def _parse_one(index: int, item: object) -> PipelineSpec:
    if not isinstance(item, dict):
        raise AlertsError(f"pipelines file: pipeline {index} must be a mapping")
    missing = [field for field in PIPELINE_FIELDS if field not in item]
    if missing:
        raise AlertsError(f"pipelines file: pipeline {index} lacks {missing}")
    unknown = sorted(key for key in item if key not in PIPELINE_FIELDS)
    if unknown:
        raise AlertsError(f"pipelines file: pipeline {index} has unknown keys {unknown}")
    for field in PIPELINE_FIELDS:
        if not isinstance(item[field], str) or not item[field]:
            raise AlertsError(
                f"pipelines file: pipeline {index} field {field} must be a non-empty string"
            )
    if not NAME_PATTERN.fullmatch(item["name"]):
        raise AlertsError(
            f"pipelines file: pipeline {index} name {item['name']!r} must be lowercase words joined by '-'"
        )
    if item["source"] == item["destination"]:
        raise AlertsError(f"pipelines file: pipeline {index} source and destination are the same")
    return PipelineSpec(**{field: item[field] for field in PIPELINE_FIELDS})


def _stream(name: str) -> dict:
    return {"node_type": "stream", "org_id": ORG, "stream_name": name, "stream_type": "traces"}


def _condition(column: str, operator: str, value: str) -> dict:
    return {
        "filterType": "condition",
        "column": column,
        "operator": operator,
        "value": value,
        "logicalOperator": "OR",
    }


def _filter(logical_operator: str, conditions: list[dict]) -> dict:
    # Version 2 is the condition format of OpenObserve v1.x; a span that matches no condition
    # node reaches no stream and is lost.
    return {
        "node_type": "condition",
        "version": 2,
        "conditions": {
            "filterType": "group",
            "logicalOperator": logical_operator,
            "conditions": conditions,
        },
    }


def _node(node_id: str, data: dict, io_type: str, x: int, y: int) -> dict:
    return {"id": node_id, "data": data, "position": {"x": x, "y": y}, "io_type": io_type, "meta": {}}


def pipeline_payload(spec: PipelineSpec) -> dict:
    moved = _filter("AND", [_condition(spec.column, "contains", spec.contains)])
    # "not_contains" is false on a null column, so spans without it need their own condition or
    # they would match neither filter and be dropped.
    kept = _filter(
        "OR",
        [
            _condition(spec.column, "is_null", ""),
            _condition(spec.column, "not_contains", spec.contains),
        ],
    )
    return {
        "name": spec.name,
        "description": spec.description,
        "enabled": True,
        "org": ORG,
        "source": {
            "source_type": "realtime",
            "org_id": ORG,
            "stream_name": spec.source,
            "stream_type": "traces",
        },
        "nodes": [
            _node("source", _stream(spec.source), "input", 0, 100),
            _node("moved", moved, "default", 250, 0),
            _node("kept", kept, "default", 250, 200),
            _node("to-destination", _stream(spec.destination), "output", 500, 0),
            _node("back-to-source", _stream(spec.source), "output", 500, 200),
        ],
        "edges": [
            {"id": "source-moved", "source": "source", "target": "moved"},
            {"id": "moved-destination", "source": "moved", "target": "to-destination"},
            {"id": "source-kept", "source": "source", "target": "kept"},
            {"id": "kept-source", "source": "kept", "target": "back-to-source"},
        ],
    }


def apply_pipelines(admin: OpenObserveAdmin, specs: Sequence[PipelineSpec]) -> tuple[int, int]:
    existing = {
        entry["name"]: entry
        for entry in admin.request_json("GET", f"/api/{ORG}/pipelines")["list"]
    }
    created = updated = 0
    for spec in specs:
        payload = pipeline_payload(spec)
        current = existing.get(spec.name)
        if current is None:
            admin.request_json("POST", f"/api/{ORG}/pipelines", payload)
            created += 1
        else:
            payload["pipeline_id"] = current["pipeline_id"]
            payload["version"] = current.get("version", 0)
            admin.request_json("PUT", f"/api/{ORG}/pipelines", payload)
            updated += 1
    return created, updated


def run(argv: Sequence[str] | None = None, *, admin: OpenObserveAdmin | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openobserve_pipelines")
    parser.add_argument("--url", required=True)
    parser.add_argument("--pipelines-file", required=True, type=Path)
    parser.add_argument("--openobserve-env", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        specs = parse_pipelines(args.pipelines_file.read_text())
        observe = parse_env_file(args.openobserve_env)
        email = observe.get("ZO_ROOT_USER_EMAIL")
        password = observe.get("ZO_ROOT_USER_PASSWORD")
        if not email or not password:
            raise AlertsError(f"root login is missing from {args.openobserve_env}")
        admin = admin or OpenObserveAdmin(args.url, email, password)
        admin.wait_ready()
        created, updated = apply_pipelines(admin, specs)
    except AlertsError as exc:
        print(f"openobserve_pipelines: {exc}", file=sys.stderr)
        return 1
    print(f"openobserve pipelines: {created} created, {updated} updated")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
