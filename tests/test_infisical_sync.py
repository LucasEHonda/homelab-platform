# Failure list (each test below guards one of these):
# - an existing secret is sent again and overwritten
# - a directive is applied to a key other than the one right below it
# - an __PLACEHOLDER__ template value is stored as the default
# - the two VAPID halves do not belong to the same key pair
# - only one VAPID half is missing and a new, mismatching half is written
# - a bad directive is found only after some secrets were written
# - generated or typed values are printed
# - Ctrl-C while asking writes part of the secrets
# - --check writes anything
# - an env file without a template is treated as an error
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from deployer.tools.infisical_bootstrap import BootstrapError, InfisicalAdmin
from deployer.tools.infisical_sync import parse_template, resolve_missing, run

MANIFEST = """project: ix-demo
compose: compose.yml
image_vars: [API_IMAGE_REF]
env_files:
  ../.envs/.production/.django: /django
  ../.envs/.production/.tailscale: /tailscale
migrate_service: migrate
health_service: api
"""

DJANGO = """# Production settings.
DJANGO_DEBUG=False
# generate: token_urlsafe 50
DJANGO_SECRET_KEY=__DJANGO_SECRET_KEY__
DJANGO_ALLOWED_HOSTS=demo.__TAILNET__
# generate: vapid_public_key
VAPID_PUBLIC_KEY=
# generate: vapid_private_key
VAPID_PRIVATE_KEY=
"""


class FakeInfisical:
    def __init__(self, existing):
        self.existing = existing
        self.requests = []

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == "/api/v1/projects":
            return httpx.Response(200, json={"projects": [{"id": "p1", "name": "demo"}]})
        if request.method == "GET" and request.url.path == "/api/v4/secrets":
            folder = request.url.params["secretPath"].strip("/")
            keys = self.existing.get(folder, [])
            return httpx.Response(
                200, json={"secrets": [{"secretKey": k, "secretValue": "old"} for k in keys]}
            )
        return httpx.Response(200, json={})

    def written(self):
        return {
            body["secretPath"]: {s["secretKey"]: s["secretValue"] for s in body["secrets"]}
            for method, path, body in self.requests
            if method == "POST" and path == "/api/v4/secrets/batch"
        }


@pytest.fixture
def bundle(tmp_path):
    (tmp_path / "envs").mkdir()
    (tmp_path / "deploy.yml").write_text(MANIFEST)
    (tmp_path / "envs" / ".django.example").write_text(DJANGO)
    return tmp_path


def sync(bundle, fake, *, answers=None, check=False):
    answers = dict(answers or {})
    prompts = []

    def ask(prompt, hidden):
        prompts.append((prompt, hidden))
        key = prompt.split("/", 1)[1].split(" ", 1)[0].rstrip(":")
        if answers.get(key) is KeyboardInterrupt:
            raise KeyboardInterrupt
        return answers.get(key, "")

    admin = InfisicalAdmin(
        "http://infisical:8080", httpx.Client(transport=httpx.MockTransport(fake.handler))
    )
    argv = ["--url", "http://infisical:8080", "--project", "demo", "--bundle", str(bundle)]
    code = run([*argv, "--check"] if check else argv, admin=admin, environ={"INFISICAL_TOKEN": "t"}, ask=ask)
    return code, prompts


def test_only_missing_keys_are_written(bundle):
    fake = FakeInfisical({"django": ["DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"]})

    code, prompts = sync(bundle, fake)

    assert code == 0
    assert prompts == []
    assert set(fake.written()["/django"]) == {"VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"}


def test_directive_applies_only_to_the_next_key():
    keys = {item.key: item for item in parse_template(DJANGO)}

    assert keys["DJANGO_SECRET_KEY"].generator == ("token_urlsafe", "50")
    assert keys["DJANGO_DEBUG"].generator is None
    assert keys["DJANGO_ALLOWED_HOSTS"].generator is None


def test_placeholder_is_not_a_default_but_a_plain_value_is():
    keys = {item.key: item for item in parse_template(DJANGO)}

    assert keys["DJANGO_SECRET_KEY"].default == ""
    assert keys["DJANGO_ALLOWED_HOSTS"].default == ""
    assert keys["DJANGO_DEBUG"].default == "False"


def test_asked_key_falls_back_to_the_template_default_and_hides_secrets(bundle):
    fake = FakeInfisical(
        {"django": ["DJANGO_SECRET_KEY", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]}
    )

    code, prompts = sync(bundle, fake, answers={"DJANGO_ALLOWED_HOSTS": "demo.tail.ts.net"})

    assert code == 0
    assert fake.written()["/django"] == {
        "DJANGO_DEBUG": "False",
        "DJANGO_ALLOWED_HOSTS": "demo.tail.ts.net",
    }
    assert [hidden for _, hidden in prompts] == [False, False]


def test_generated_vapid_halves_form_one_key_pair(bundle):
    fake = FakeInfisical({"django": ["DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS"]})

    sync(bundle, fake)

    written = fake.written()["/django"]
    private_value = int.from_bytes(_b64decode(written["VAPID_PRIVATE_KEY"]), "big")
    derived = ec.derive_private_key(private_value, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    assert _b64decode(written["VAPID_PUBLIC_KEY"]) == derived


def test_generated_token_has_the_requested_size(bundle):
    fake = FakeInfisical({"django": ["DJANGO_DEBUG", "DJANGO_ALLOWED_HOSTS", "VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]})

    sync(bundle, fake)

    assert len(_b64decode(fake.written()["/django"]["DJANGO_SECRET_KEY"])) == 50


def test_half_a_vapid_pair_is_refused_and_nothing_is_written(bundle, capsys):
    fake = FakeInfisical({"django": ["DJANGO_DEBUG", "DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "VAPID_PUBLIC_KEY"]})

    code, _ = sync(bundle, fake)

    assert code == 1
    assert fake.written() == {}
    assert "part of the VAPID pair" in capsys.readouterr().err


@pytest.mark.parametrize(
    "directive",
    ["# generate: magic", "# generate: token_hex", "# generate: token_hex 4", "# generate: vapid_public_key 1"],
)
def test_bad_directive_fails_before_any_request(bundle, directive):
    (bundle / "envs" / ".django.example").write_text(f"{directive}\nA=\n")
    fake = FakeInfisical({})

    code, _ = sync(bundle, fake)

    assert code == 1
    assert fake.requests == []


def test_values_are_never_printed(bundle, capsys):
    fake = FakeInfisical({})

    sync(bundle, fake, answers={"DJANGO_ALLOWED_HOSTS": "typed-value"})

    output = "".join(capsys.readouterr())
    assert "typed-value" not in output
    for value in fake.written()["/django"].values():
        if value not in ("", "False"):
            assert value not in output


def test_ctrl_c_while_asking_writes_nothing(bundle):
    fake = FakeInfisical({})

    code, _ = sync(bundle, fake, answers={"DJANGO_ALLOWED_HOSTS": KeyboardInterrupt})

    assert code == 1
    assert fake.written() == {}


def test_check_lists_missing_keys_and_writes_nothing(bundle, capsys):
    fake = FakeInfisical({})

    code, prompts = sync(bundle, fake, check=True)

    assert code == 1
    assert prompts == []
    assert fake.written() == {}
    assert "missing in django: DJANGO_DEBUG" in capsys.readouterr().out


def test_env_file_without_template_is_skipped(bundle):
    fake = FakeInfisical({"django": [item.key for item in parse_template(DJANGO)]})

    code, _ = sync(bundle, fake)

    assert code == 0
    assert not any("tailscale" in str(body) for _, _, body in fake.requests)


def test_partial_pair_check_is_per_folder():
    items = [item for item in parse_template(DJANGO) if item.key == "VAPID_PRIVATE_KEY"]

    with pytest.raises(BootstrapError):
        resolve_missing("django", items, ask=lambda prompt, hidden: "")


def _b64decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
