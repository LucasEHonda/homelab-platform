# Failure list (each test below guards one of these):
# - block on a zone with no custom rules yet does nothing or fails
# - block replaces the zone's other WAF custom rules
# - block twice creates a second kill-switch rule
# - unblock leaves the rule enabled
# - status reports open while the rule is enabled
# - a Cloudflare API error is swallowed and reported as success
# - the command runs without a token
import pytest

from deployer.tools.cloudflare_killswitch import (
    PHASE,
    CloudflareError,
    KillSwitch,
    main,
)

ZONE = "meutill.com"
ZONE_ID = "z1"
ENTRYPOINT = f"/zones/{ZONE_ID}/rulesets/phases/{PHASE}/entrypoint"
RATE_LIMIT = {
    "id": "r-other",
    "description": "rate limit auth",
    "expression": 'starts_with(http.request.uri.path, "/api/auth/")',
    "action": "block",
    "enabled": True,
}


class FakeCloudflare:
    """Just enough of the Cloudflare API: one zone and its custom-rules entrypoint."""

    def __init__(self, rules=None, fail=None):
        self.rules = None if rules is None else [dict(r) for r in rules]
        self.fail = fail
        self.calls = []
        self._next = 0

    def _id(self):
        self._next += 1
        return f"r{self._next}"

    def _ruleset(self):
        return {"id": "rs1", "phase": PHASE, "rules": self.rules}

    def send(self, method, path, body):
        self.calls.append((method, path, body))
        if self.fail and self.fail == (method, path.split("?")[0]):
            return 400, {"success": False, "errors": [{"code": 10000, "message": "boom"}], "result": None}
        if method == "GET" and path == f"/zones?name={ZONE}":
            return 200, {"success": True, "errors": [], "result": [{"id": ZONE_ID, "name": ZONE}]}
        if method == "GET" and path == ENTRYPOINT:
            if self.rules is None:
                return 404, {"success": False, "errors": [{"code": 10003, "message": "not found"}], "result": None}
            return 200, {"success": True, "errors": [], "result": self._ruleset()}
        if method == "PUT" and path == ENTRYPOINT:
            assert self.rules is None, "PUT would overwrite an existing ruleset"
            self.rules = [dict(r, id=self._id()) for r in body["rules"]]
            return 200, {"success": True, "errors": [], "result": self._ruleset()}
        if method == "POST" and path == f"/zones/{ZONE_ID}/rulesets/rs1/rules":
            self.rules.append(dict(body, id=self._id()))
            return 200, {"success": True, "errors": [], "result": self._ruleset()}
        if method == "PATCH" and path.startswith(f"/zones/{ZONE_ID}/rulesets/rs1/rules/"):
            rule_id = path.rsplit("/", 1)[1]
            for i, rule in enumerate(self.rules):
                if rule["id"] == rule_id:
                    self.rules[i] = dict(body, id=rule_id)
                    return 200, {"success": True, "errors": [], "result": self._ruleset()}
        return 404, {"success": False, "errors": [{"code": 7003, "message": "no route"}], "result": None}

    def kill_switch_rules(self):
        return [r for r in self.rules or [] if r["description"] == f"kill-switch {ZONE}"]


def switch(fake):
    return KillSwitch(ZONE, fake.send)


def test_block_on_a_zone_without_custom_rules_creates_the_rule_enabled():
    fake = FakeCloudflare()

    switch(fake).set_blocked(True)

    [rule] = fake.kill_switch_rules()
    assert rule["enabled"] is True
    assert rule["action"] == "block"
    assert '"meutill.com"' in rule["expression"]


def test_block_keeps_the_zone_other_rules():
    fake = FakeCloudflare(rules=[RATE_LIMIT])

    switch(fake).set_blocked(True)

    assert [r["id"] for r in fake.rules if r["id"] == "r-other"] == ["r-other"]
    assert fake.rules[0] == RATE_LIMIT
    assert len(fake.kill_switch_rules()) == 1


def test_block_twice_keeps_a_single_rule():
    fake = FakeCloudflare(rules=[RATE_LIMIT])

    switch(fake).set_blocked(True)
    switch(fake).set_blocked(True)

    assert len(fake.kill_switch_rules()) == 1


def test_unblock_disables_the_rule():
    fake = FakeCloudflare(rules=[RATE_LIMIT])
    switch(fake).set_blocked(True)

    switch(fake).set_blocked(False)

    [rule] = fake.kill_switch_rules()
    assert rule["enabled"] is False
    assert fake.rules[0] == RATE_LIMIT


def test_status_follows_the_rule():
    fake = FakeCloudflare()
    ks = switch(fake)

    assert ks.is_blocked() is False
    ks.set_blocked(True)
    assert ks.is_blocked() is True
    ks.set_blocked(False)
    assert ks.is_blocked() is False


def test_status_on_a_zone_without_custom_rules_writes_nothing():
    fake = FakeCloudflare()

    assert switch(fake).is_blocked() is False

    assert [c for c in fake.calls if c[0] != "GET"] == []


def test_api_error_is_raised_with_cloudflare_message():
    fake = FakeCloudflare(rules=[RATE_LIMIT], fail=("POST", f"/zones/{ZONE_ID}/rulesets/rs1/rules"))

    with pytest.raises(CloudflareError, match="boom"):
        switch(fake).set_blocked(True)


def test_main_refuses_to_run_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)

    assert main(["status", "--zone", ZONE]) == 2
    assert "CLOUDFLARE_API_TOKEN" in capsys.readouterr().err


def test_main_block_prints_the_new_state(monkeypatch, capsys):
    fake = FakeCloudflare()
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    assert main(["block", "--zone", ZONE], send=fake.send) == 0

    assert "BLOCKED" in capsys.readouterr().out
    assert fake.kill_switch_rules()[0]["enabled"] is True


def test_main_reports_api_errors_with_exit_code_1(monkeypatch, capsys):
    fake = FakeCloudflare(fail=("GET", "/zones"))
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    assert main(["block", "--zone", ZONE], send=fake.send) == 1
    assert "boom" in capsys.readouterr().err
