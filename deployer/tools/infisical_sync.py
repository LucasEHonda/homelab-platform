"""Creates the Infisical secrets a release needs that do not exist yet.

Reads a release bundle laid out like the one the deployer extracts (deploy.yml plus
envs/<file>.example) and, for every key in a template that the app's Infisical project lacks,
generates the value or asks for it. An existing secret is never changed.

A template asks for a generated value with a comment right above the key:

    # generate: token_urlsafe 50
    DJANGO_SECRET_KEY=
    # generate: vapid_public_key
    VAPID_PUBLIC_KEY=
    # generate: vapid_private_key
    VAPID_PRIVATE_KEY=

Keys without a directive are asked for; the template value is the default unless it is an
__PLACEHOLDER__.
"""

import argparse
import base64
import getpass
import os
import re
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from deployer.manifest import ManifestError, load_manifest
from deployer.tools.infisical_bootstrap import ENVIRONMENT, BootstrapError, InfisicalAdmin
from deployer.tools.infisical_secrets import _create_folder

DIRECTIVE = re.compile(r"^#\s*generate:\s*(.*)$")
PLACEHOLDER = re.compile(r"__[A-Z0-9_]+__")
# Typed without echo when asked for.
HIDDEN_KEY = re.compile(r"SECRET|PASSWORD|TOKEN|KEY")
TOKEN_KINDS = {"token_urlsafe": secrets.token_urlsafe, "token_hex": secrets.token_hex}
VAPID_KINDS = ("vapid_public_key", "vapid_private_key")
MIN_TOKEN_BYTES = 16
MAX_TOKEN_BYTES = 128


@dataclass(frozen=True)
class TemplateKey:
    key: str
    default: str
    generator: tuple[str, ...] | None


def parse_template(text: str) -> list[TemplateKey]:
    """Keys in template order; a directive belongs to the next key line."""
    keys: list[TemplateKey] = []
    pending: tuple[str, ...] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        directive = DIRECTIVE.match(line)
        if directive:
            pending = tuple(directive.group(1).split())
            _validate_generator(pending)
            continue
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise BootstrapError(f"invalid template line {line!r}")
        value = value.strip()
        default = "" if PLACEHOLDER.search(value) else value
        keys.append(TemplateKey(key.strip(), default, pending))
        pending = None
    if pending is not None:
        raise BootstrapError("generate directive with no key after it")
    return keys


def _validate_generator(generator: tuple[str, ...]) -> None:
    if not generator:
        raise BootstrapError("empty generate directive")
    kind, *params = generator
    if kind in TOKEN_KINDS:
        if len(params) != 1 or not params[0].isdigit():
            raise BootstrapError(f"generate: {kind} needs one byte count")
        if not MIN_TOKEN_BYTES <= int(params[0]) <= MAX_TOKEN_BYTES:
            raise BootstrapError(
                f"generate: {kind} byte count must be {MIN_TOKEN_BYTES}-{MAX_TOKEN_BYTES}"
            )
    elif kind in VAPID_KINDS:
        if params:
            raise BootstrapError(f"generate: {kind} takes no parameter")
    else:
        raise BootstrapError(f"generate: unknown kind {kind}")


def vapid_key_pair() -> dict[str, str]:
    """A P-256 key pair encoded the way browsers and pywebpush read it (raw, base64url)."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return {
        "vapid_public_key": _b64url(public),
        "vapid_private_key": _b64url(private.private_numbers().private_value.to_bytes(32, "big")),
    }


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def resolve_missing(
    folder: str,
    missing: Sequence[TemplateKey],
    *,
    ask: Callable[[str, bool], str],
) -> tuple[dict[str, str], list[str]]:
    """Values for the missing keys of one folder, and which of them were generated."""
    vapid = [item for item in missing if item.generator and item.generator[0] in VAPID_KINDS]
    pair = _vapid_pair_for(folder, vapid)
    values: dict[str, str] = {}
    generated: list[str] = []
    for item in missing:
        if item.generator is None:
            values[item.key] = _ask(folder, item, ask)
            continue
        kind, *params = item.generator
        values[item.key] = pair[kind] if kind in VAPID_KINDS else TOKEN_KINDS[kind](int(params[0]))
        generated.append(item.key)
    return values, generated


def _vapid_pair_for(folder: str, vapid: Sequence[TemplateKey]) -> dict[str, str]:
    if not vapid:
        return {}
    kinds = sorted(item.generator[0] for item in vapid)
    if kinds != sorted(VAPID_KINDS):
        # One half already exists; a new half would not match it.
        raise BootstrapError(
            f"{folder}: only part of the VAPID pair is missing; set it by hand or delete both"
        )
    return vapid_key_pair()


def _ask(folder: str, item: TemplateKey, ask: Callable[[str, bool], str]) -> str:
    suffix = f" [{item.default}]" if item.default else " (Enter = empty)"
    answer = ask(f"{folder}/{item.key}{suffix}: ", bool(HIDDEN_KEY.search(item.key)))
    return answer or item.default


def _terminal_ask(prompt: str, hidden: bool) -> str:
    return getpass.getpass(prompt) if hidden else input(prompt)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infisical_sync")
    parser.add_argument("--url", required=True)
    parser.add_argument("--project", required=True, help="Infisical project name (the app name)")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--environment", default=ENVIRONMENT)
    parser.add_argument("--check", action="store_true", help="list missing keys, write nothing")
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    admin: InfisicalAdmin | None = None,
    environ: Mapping[str, str] = os.environ,
    ask: Callable[[str, bool], str] = _terminal_ask,
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = load_manifest(args.bundle / "deploy.yml")
        templates = _read_templates(args.bundle, manifest.env_files)
        token = environ.get("INFISICAL_TOKEN") or ask("Infisical admin token: ", True)
        if not token:
            raise BootstrapError("an Infisical admin token is required")
        admin = admin or InfisicalAdmin(args.url)
        admin.use_token(token)
        project_id = admin.find_project(args.project)
        missing: dict[str, list[TemplateKey]] = {}
        for folder, keys in templates.items():
            existing = admin.list_secret_keys(project_id, args.environment, folder)
            items = [item for item in keys if item.key not in existing]
            if items:
                missing[folder] = items
        if not missing:
            print("infisical_sync: nothing missing")
            return 0
        if args.check:
            for folder, items in missing.items():
                print(f"missing in {folder}: {', '.join(item.key for item in items)}")
            return 1
        # Everything is resolved before the first write, so a failure or Ctrl-C writes nothing.
        resolved = {
            folder: resolve_missing(folder, items, ask=ask) for folder, items in missing.items()
        }
        for folder, (values, generated) in resolved.items():
            _create_folder(admin, project_id, folder, environment=args.environment)
            admin.create_secrets(project_id, folder, values, environment=args.environment)
            asked = [key for key in values if key not in generated]
            print(
                f"infisical_sync: {folder}: generated {', '.join(generated) or 'none'};"
                f" asked {', '.join(asked) or 'none'}"
            )
    except (BootstrapError, ManifestError) as exc:
        print(f"infisical_sync: {exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\ninfisical_sync: cancelled, nothing written", file=sys.stderr)
        return 1
    return 0


def _read_templates(bundle: Path, env_files: Mapping[str, str]) -> dict[str, list[TemplateKey]]:
    """Template keys per folder name ("django"), for the env files that have a template."""
    templates: dict[str, list[TemplateKey]] = {}
    for relative, folder in env_files.items():
        template = bundle / "envs" / f"{Path(relative).name}.example"
        if template.exists():
            templates[folder.strip("/")] = parse_template(template.read_text())
    return templates


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
