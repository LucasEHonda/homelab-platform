import base64
import json

import httpx
import pytest

from deployer.tools.openobserve_alerts import (
    ALERT_FIELDS,
    MANAGED_TAG,
    NTFY_HEADERS,
    AlertsError,
    AlertSpec,
    ExistingAlert,
    OpenObserveAdmin,
    alert_payload,
    apply_alerts,
    parse_alerts,
    plan_changes,
    run,
)

VALID = """
alerts:
  - name: server-errors-5xx
    description: Any 5xx response.
    sql: SELECT 1
    period: 5
    frequency: 1
    operator: ">="
    threshold: 1
    silence: 30
    row_template: "{service_name}: {errors}"
  - name: slow-requests
    description: Slow.
    sql: SELECT 2
    period: 10
    frequency: 2
    operator: ">"
    threshold: 0
    silence: 0
    row_template: "{route}"
"""

BASE = {
    "name": "a",
    "description": "d",
    "sql": "SELECT 1",
    "period": 5,
    "frequency": 1,
    "operator": ">=",
    "threshold": 1,
    "silence": 30,
    "row_template": "r",
}


def spec(name="a", sql="SELECT 1"):
    return AlertSpec(name, "d", sql, 5, 1, ">=", 1, 30, "r")


class Recorder:
    def __init__(self, missing=(), alerts=(), alert_post_error=None):
        self.requests = []
        self.missing = set(missing)
        self.alerts = list(alerts)
        self.alert_post_error = alert_post_error

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        method, path = request.method, request.url.path
        self.requests.append((method, path, body, request.headers.get("authorization")))
        if path == "/healthz":
            return httpx.Response(200)
        if method == "PUT" and path.rsplit("/", 1)[0].endswith(("templates", "destinations")):
            return httpx.Response(404 if path.rsplit("/", 1)[1] in self.missing else 200)
        if method == "GET" and path == "/api/v2/default/alerts":
            return httpx.Response(200, json={"list": self.alerts})
        if method == "POST" and path == "/api/v2/default/alerts" and self.alert_post_error:
            return httpx.Response(400, json=self.alert_post_error)
        return httpx.Response(200, json={})

    def calls(self):
        return [(method, path) for method, path, _, _ in self.requests]


def make_admin(recorder):
    client = httpx.Client(transport=httpx.MockTransport(recorder.handler))
    return OpenObserveAdmin("http://oo", "root@x", "pw", client, sleep=lambda s: None)


def test_parse_alerts_returns_specs_in_order():
    specs = parse_alerts(VALID)
    assert [s.name for s in specs] == ["server-errors-5xx", "slow-requests"]
    assert specs[0] == AlertSpec(
        "server-errors-5xx", "Any 5xx response.", "SELECT 1", 5, 1, ">=", 1, 30,
        "{service_name}: {errors}",
    )


def _without(field):
    return {k: v for k, v in BASE.items() if k != field}


def _doc(*items):
    import yaml

    return yaml.safe_dump({"alerts": list(items)})


@pytest.mark.parametrize(
    "text, message",
    [
        ("alerts: nope", "alerts file: 'alerts' must be a non-empty list"),
        ("- a", "alerts file: 'alerts' must be a non-empty list"),
        ("alerts: []", "alerts file: 'alerts' must be a non-empty list"),
        (_doc("x"), "alerts file: alert 0 must be a mapping"),
        (_doc(_without("sql")), "alerts file: alert 0 lacks ['sql']"),
        (_doc({**BASE, "zzz": 1, "aaa": 2}), "alerts file: alert 0 has unknown keys ['aaa', 'zzz']"),
        (
            _doc({**BASE, "sql": ""}),
            "alerts file: alert 0 field sql must be a non-empty string",
        ),
        (
            _doc({**BASE, "operator": "=="}),
            "alerts file: alert 0 operator '==' is not one of "
            "('=', '!=', '>', '>=', '<', '<=')",
        ),
        (
            _doc({**BASE, "period": 0}),
            "alerts file: alert 0 field period must be an integer >= 1",
        ),
        (
            _doc({**BASE, "threshold": -1}),
            "alerts file: alert 0 field threshold must be an integer >= 0",
        ),
        (
            _doc({**BASE, "silence": True}),
            "alerts file: alert 0 field silence must be an integer >= 0",
        ),
        (_doc({**BASE, "name": "Server errors"}), "alerts file: alert 0 name 'Server errors' must be lowercase words joined by '-'"),
        (_doc({**BASE, "name": "a:b"}), "alerts file: alert 0 name 'a:b' must be lowercase words joined by '-'"),
        (_doc({**BASE, "name": "Trailing-"}), "alerts file: alert 0 name 'Trailing-' must be lowercase words joined by '-'"),
        (_doc(BASE, BASE), "alerts file: duplicate alert names ['a']"),
    ],
)
def test_parse_alerts_validation_errors(text, message):
    with pytest.raises(AlertsError) as error:
        parse_alerts(text)
    assert str(error.value) == message


def test_alert_fields_match_spec():
    assert set(ALERT_FIELDS) == set(BASE)


def test_alert_payload_exact():
    assert alert_payload(spec()) == {
        "name": "a",
        "description": "d",
        "stream_type": "traces",
        "stream_name": "default",
        "is_real_time": False,
        "enabled": True,
        "tags": [MANAGED_TAG],
        "query_condition": {"type": "sql", "sql": "SELECT 1"},
        "trigger_condition": {
            "period": 5,
            "operator": ">=",
            "threshold": 1,
            "frequency": 1,
            "frequency_type": "minutes",
            "silence": 30,
        },
        "row_template": "r",
        "destinations": ["ntfy"],
    }


def test_plan_changes():
    existing = [
        ExistingAlert("id-old", "stale-alert", (MANAGED_TAG,)),
        ExistingAlert("id-hand", "hand-made", ()),
        ExistingAlert("id-a", "a", ()),
    ]
    plan = plan_changes([spec("a"), spec("new-alert")], existing)
    assert [s.name for s in plan.create] == ["new-alert"]
    assert [(i, s.name) for i, s in plan.update] == [("id-a", "a")]
    assert plan.delete == ("id-old",)


def test_put_template_missing_puts_then_posts():
    recorder = Recorder(missing={"ntfy"})
    make_admin(recorder).put_template("ntfy", "body")
    assert recorder.calls() == [
        ("PUT", "/api/default/alerts/templates/ntfy"),
        ("POST", "/api/default/alerts/templates"),
    ]
    assert recorder.requests[0][2] == recorder.requests[1][2]
    assert recorder.requests[0][2] == {"name": "ntfy", "type": "http", "title": "", "body": "body"}


def test_put_template_present_only_puts():
    recorder = Recorder()
    make_admin(recorder).put_template("ntfy", "body")
    assert recorder.calls() == [("PUT", "/api/default/alerts/templates/ntfy")]


def test_put_destination_present():
    recorder = Recorder()
    make_admin(recorder).put_destination("ntfy", "https://ntfy.sh/t", "ntfy", NTFY_HEADERS)
    assert recorder.calls() == [("PUT", "/api/default/alerts/destinations/ntfy")]
    assert recorder.requests[0][2] == {
        "name": "ntfy",
        "type": "http",
        "url": "https://ntfy.sh/t",
        "method": "post",
        "template": "ntfy",
        "headers": NTFY_HEADERS,
    }


def test_every_request_carries_basic_auth():
    recorder = Recorder()
    admin = make_admin(recorder)
    admin.wait_ready()
    admin.put_template("ntfy", "b")
    admin.list_alerts()
    admin.create_alert({})
    expected = "Basic " + base64.b64encode(b"root@x:pw").decode()
    assert len(recorder.requests) == 4
    assert {auth for _, _, _, auth in recorder.requests} == {expected}


def test_error_response_hides_payload():
    recorder = Recorder(alert_post_error={"code": 400, "message": "bad sql"})
    payload = alert_payload(spec(sql="SELECT secret_column"))
    with pytest.raises(AlertsError) as error:
        make_admin(recorder).create_alert(payload)
    assert "HTTP 400 bad sql" in str(error.value)
    assert "secret_column" not in str(error.value)


def test_apply_alerts_call_order():
    existing = [
        {"alert_id": "id-a", "name": "a", "tags": []},
        {"alert_id": "id-old", "name": "stale-alert", "tags": [MANAGED_TAG]},
        {"alert_id": "id-hand", "name": "hand"},
    ]
    recorder = Recorder(alerts=existing)
    plan = apply_alerts(make_admin(recorder), [spec("a"), spec("new-alert")], "https://ntfy.sh/t")
    assert recorder.calls() == [
        ("PUT", "/api/default/alerts/templates/ntfy"),
        ("PUT", "/api/default/alerts/destinations/ntfy"),
        ("GET", "/api/v2/default/alerts"),
        ("POST", "/api/v2/default/alerts"),
        ("PUT", "/api/v2/default/alerts/id-a"),
        ("DELETE", "/api/v2/default/alerts/id-old"),
    ]
    assert (len(plan.create), len(plan.update), len(plan.delete)) == (1, 1, 1)


def write_inputs(tmp_path, deployer_text):
    alerts = tmp_path / "alerts.yml"
    alerts.write_text(_doc(BASE))
    observe = tmp_path / ".openobserve"
    observe.write_text("ZO_ROOT_USER_EMAIL=root@x\nZO_ROOT_USER_PASSWORD=s3cret-pw\n")
    deployer = tmp_path / ".deployer"
    deployer.write_text(deployer_text)
    return [
        "--url", "http://oo",
        "--alerts-file", str(alerts),
        "--openobserve-env", str(observe),
        "--deployer-env", str(deployer),
    ]


def test_run_end_to_end(tmp_path, capsys):
    argv = write_inputs(tmp_path, "NTFY_URL=https://ntfy.sh/homelab-abc\n")
    code = run(argv, admin=make_admin(Recorder()))
    captured = capsys.readouterr()
    assert code == 0
    assert "openobserve alerts: 1 created, 0 updated, 0 deleted" in captured.out
    output = captured.out + captured.err
    assert "s3cret-pw" not in output
    assert "homelab-abc" not in output


def test_run_without_ntfy_url(tmp_path, capsys):
    argv = write_inputs(tmp_path, "OTHER=1\n")
    assert run(argv, admin=make_admin(Recorder())) == 1
    assert "NTFY_URL is missing from" in capsys.readouterr().err


def test_wait_ready_times_out():
    always = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    admin = OpenObserveAdmin("http://oo", "root@x", "pw", always, sleep=lambda s: None)
    with pytest.raises(AlertsError, match="OpenObserve did not become ready"):
        admin.wait_ready(timeout_seconds=10)
