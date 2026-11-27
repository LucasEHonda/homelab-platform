import json
from pathlib import Path

import httpx
import pytest

from deployer.tools.openobserve_alerts import AlertsError, OpenObserveAdmin
from deployer.tools.openobserve_pipelines import (
    PipelineSpec,
    apply_pipelines,
    parse_pipelines,
    pipeline_payload,
    run,
)

VALID = """
pipelines:
  - name: health-checks-to-own-stream
    description: Health spans.
    source: default
    column: code_file_path
    contains: /health.py
    destination: health
"""

SPEC = PipelineSpec(
    "health-checks-to-own-stream", "Health spans.", "default", "code_file_path", "/health.py", "health"
)


class Recorder:
    def __init__(self, pipelines=()):
        self.pipelines = list(pipelines)
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/healthz":
            return httpx.Response(200)
        if request.method == "GET" and request.url.path == "/api/default/pipelines":
            return httpx.Response(200, json={"list": self.pipelines})
        return httpx.Response(200, json={"code": 200})

    def writes(self):
        return [(m, p, b) for m, p, b in self.requests if m in ("POST", "PUT", "DELETE")]


def make_admin(recorder):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return OpenObserveAdmin("http://oo", "root@x", "pw", client, sleep=lambda s: None)


def _routes(payload):
    """Maps each condition node to the stream its edge leads to, with its conditions."""
    nodes = {node["id"]: node["data"] for node in payload["nodes"]}
    edges = {edge["source"]: [] for edge in payload["edges"]}
    for edge in payload["edges"]:
        edges[edge["source"]].append(edge["target"])
    source_id = payload["nodes"][0]["id"]
    routes = []
    for filter_id in edges[source_id]:
        (target,) = edges[filter_id]
        routes.append((nodes[filter_id]["conditions"], nodes[target]["stream_name"]))
    return routes


def _matches(group, value):
    """Evaluates a v2 condition group the way OpenObserve does for one column value."""
    results = []
    for condition in group["conditions"]:
        operator, text = condition["operator"], condition["value"]
        if operator == "is_null":
            results.append(value is None)
        elif operator == "contains":
            results.append(value is not None and text in value)
        elif operator == "not_contains":
            results.append(value is not None and text not in value)
        else:
            raise AssertionError(f"unexpected operator {operator}")
    return any(results) if group["logicalOperator"] == "OR" else all(results)


@pytest.mark.parametrize(
    "file_path, streams",
    [
        ("support/views/health.py", ["health"]),
        ("accounting_admin/core/api/internal/health.py", ["health"]),
        # Real queries of the financial health score must stay in the default stream.
        ("accounting_admin/core/health/queries.py", ["default"]),
        ("accounting_admin/domain/financial_health.py", ["default"]),
        # Request, Celery and other spans have no code_file_path and must not be dropped.
        (None, ["default"]),
    ],
)
def test_every_span_reaches_exactly_one_stream(file_path, streams):
    routes = _routes(pipeline_payload(SPEC))
    reached = [stream for group, stream in routes if _matches(group, file_path)]
    assert reached == streams


def test_payload_reads_the_source_traces_stream():
    payload = pipeline_payload(SPEC)
    assert payload["source"] == {
        "source_type": "realtime",
        "org_id": "default",
        "stream_name": "default",
        "stream_type": "traces",
    }
    assert payload["nodes"][0]["data"]["stream_name"] == "default"
    assert payload["nodes"][0]["io_type"] == "input"


@pytest.mark.parametrize(
    "text, message",
    [
        ("pipelines: []", "pipelines file: 'pipelines' must be a non-empty list"),
        (VALID.replace("    contains: /health.py\n", ""), "pipelines file: pipeline 0 lacks ['contains']"),
        (VALID + "    extra: 1\n", "pipelines file: pipeline 0 has unknown keys ['extra']"),
        (VALID.replace("/health.py", '""'), "pipelines file: pipeline 0 field contains must be a non-empty string"),
        (VALID.replace("destination: health", "destination: default"), "pipelines file: pipeline 0 source and destination are the same"),
        (VALID.replace("health-checks-to-own-stream", "Health checks"), "pipelines file: pipeline 0 name 'Health checks' must be lowercase words joined by '-'"),
        (VALID + VALID.replace("pipelines:\n", ""), "pipelines file: duplicate pipeline names ['health-checks-to-own-stream']"),
    ],
)
def test_parse_pipelines_rejects(text, message):
    with pytest.raises(AlertsError) as error:
        parse_pipelines(text)
    assert str(error.value) == message


def test_repo_pipelines_file_is_valid():
    specs = parse_pipelines(Path("deploy/openobserve/pipelines.yml").read_text())
    assert [s.destination for s in specs] == ["health"]


def test_apply_creates_missing_pipeline():
    recorder = Recorder(pipelines=[{"pipeline_id": "hand", "name": "made-in-ui", "version": 3}])
    assert apply_pipelines(make_admin(recorder), [SPEC]) == (1, 0)
    ((method, path, body),) = recorder.writes()
    assert (method, path) == ("POST", "/api/default/pipelines")
    assert "pipeline_id" not in body


def test_apply_updates_existing_pipeline_by_name_keeping_its_id():
    existing = {"pipeline_id": "p-1", "name": SPEC.name, "version": 2}
    recorder = Recorder(pipelines=[existing, {"pipeline_id": "hand", "name": "made-in-ui"}])
    assert apply_pipelines(make_admin(recorder), [SPEC]) == (0, 1)
    ((method, path, body),) = recorder.writes()
    assert (method, path) == ("PUT", "/api/default/pipelines")
    assert (body["pipeline_id"], body["version"]) == ("p-1", 2)


def test_run_end_to_end(tmp_path, capsys):
    pipelines = tmp_path / "pipelines.yml"
    pipelines.write_text(VALID)
    env = tmp_path / ".openobserve"
    env.write_text("ZO_ROOT_USER_EMAIL=root@x\nZO_ROOT_USER_PASSWORD=s3cret-pw\n")
    argv = ["--url", "http://oo", "--pipelines-file", str(pipelines), "--openobserve-env", str(env)]
    assert run(argv, admin=make_admin(Recorder())) == 0
    out = capsys.readouterr()
    assert "openobserve pipelines: 1 created, 0 updated" in out.out
    assert "s3cret-pw" not in out.out + out.err


def test_run_without_root_login(tmp_path, capsys):
    pipelines = tmp_path / "pipelines.yml"
    pipelines.write_text(VALID)
    env = tmp_path / ".openobserve"
    env.write_text("OTHER=1\n")
    argv = ["--url", "http://oo", "--pipelines-file", str(pipelines), "--openobserve-env", str(env)]
    assert run(argv, admin=make_admin(Recorder())) == 1
    assert "root login is missing from" in capsys.readouterr().err
