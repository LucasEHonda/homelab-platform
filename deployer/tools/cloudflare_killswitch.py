"""Kill switch for a site published through Cloudflare: one WAF custom rule that blocks every host of the zone.

The rule is identified by its description (`kill-switch <zone>`), created disabled the first time it is
needed and then only toggled, so the zone's other custom rules are never touched. Blocking happens at
Cloudflare's edge, so no deploy on the NAS can undo it and the tailnet path to the apps is unaffected.

    python3 -m deployer.tools.cloudflare_killswitch block|unblock|status --zone meutill.com

Reads the API token from CLOUDFLARE_API_TOKEN. The token needs only `Zone > Zone > Read` and
`Zone > Zone WAF > Edit` on that zone.

Runs on the host's own python3 (stdlib only, Python 3.9+) on purpose: `make panic` must work even when
Docker Desktop is not running.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

API = "https://api.cloudflare.com/client/v4"
PHASE = "http_request_firewall_custom"
TIMEOUT_SECONDS = 20

# (method, path relative to API, json body or None) -> (http status, parsed envelope)
Send = Callable[[str, str, Optional[Dict[str, Any]]], Tuple[int, Dict[str, Any]]]


class CloudflareError(Exception):
    pass


def http_sender(token: str) -> Send:
    def send(method: str, path: str, body: Optional[Dict[str, Any]]) -> Tuple[int, Dict[str, Any]]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(API + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {token}")
        request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            except ValueError:
                return error.code, {"success": False, "errors": [{"message": error.reason}]}

    return send


class KillSwitch:
    def __init__(self, zone: str, send: Send) -> None:
        self.zone = zone
        self.description = f"kill-switch {zone}"
        self.expression = f'(http.host eq "{zone}" or ends_with(http.host, ".{zone}"))'
        self._send = send
        self._zone_id: Optional[str] = None

    def is_blocked(self) -> bool:
        _, rule = self._find()
        return bool(rule and rule.get("enabled"))

    def set_blocked(self, blocked: bool) -> None:
        ruleset_id, rule = self._find()
        desired = self._rule(blocked)
        if rule is None:
            if ruleset_id is None:
                self._call("PUT", self._entrypoint(), {"rules": [desired]})
            else:
                self._call("POST", f"/zones/{self._zone()}/rulesets/{ruleset_id}/rules", desired)
            return
        if bool(rule.get("enabled")) == blocked:
            return
        self._call("PATCH", f"/zones/{self._zone()}/rulesets/{ruleset_id}/rules/{rule['id']}", desired)

    def _rule(self, enabled: bool) -> Dict[str, Any]:
        return {
            "description": self.description,
            "expression": self.expression,
            "action": "block",
            "enabled": enabled,
        }

    def _find(self) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """(ruleset id, kill-switch rule); the ruleset id is None while the zone has no custom rules."""
        status, envelope = self._send("GET", self._entrypoint(), None)
        if status == 404:
            return None, None
        ruleset = self._result(status, envelope)
        for rule in ruleset.get("rules") or []:
            if rule.get("description") == self.description:
                return ruleset["id"], rule
        return ruleset["id"], None

    def _zone(self) -> str:
        if self._zone_id is None:
            query = urllib.parse.urlencode({"name": self.zone})
            zones = self._call("GET", f"/zones?{query}", None)
            if not zones:
                raise CloudflareError(f"zone {self.zone} not found (or the token cannot read it)")
            self._zone_id = zones[0]["id"]
        return self._zone_id

    def _entrypoint(self) -> str:
        return f"/zones/{self._zone()}/rulesets/phases/{PHASE}/entrypoint"

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> Any:
        return self._result(*self._send(method, path, body))

    @staticmethod
    def _result(status: int, envelope: Dict[str, Any]) -> Any:
        if 200 <= status < 300 and envelope.get("success"):
            return envelope.get("result")
        messages = "; ".join(e.get("message", "?") for e in envelope.get("errors") or []) or "no details"
        raise CloudflareError(f"Cloudflare API answered {status}: {messages}")


def main(argv: Optional[Sequence[str]] = None, send: Optional[Send] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=["block", "unblock", "status"])
    parser.add_argument("--zone", required=True)
    args = parser.parse_args(argv)

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        print("CLOUDFLARE_API_TOKEN is not set", file=sys.stderr)
        return 2

    switch = KillSwitch(args.zone, send or http_sender(token))
    try:
        if args.action != "status":
            switch.set_blocked(args.action == "block")
        blocked = switch.is_blocked()
    except (CloudflareError, OSError) as error:
        print(f"{args.zone}: {error}", file=sys.stderr)
        return 1
    state = "BLOCKED at Cloudflare" if blocked else "OPEN to the internet"
    print(f"{args.zone}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
