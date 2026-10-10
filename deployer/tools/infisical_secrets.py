import argparse
import os
import re
import sys
from collections.abc import Mapping, Sequence

from deployer.tools.infisical_bootstrap import ENVIRONMENT, BootstrapError, InfisicalAdmin

KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def parse_pairs(pairs: Sequence[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not KEY_PATTERN.match(key):
            raise BootstrapError("invalid pair, expected KEY=VALUE with KEY matching [A-Z][A-Z0-9_]*")
        values[key] = value
    return values


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="infisical_secrets")
    parser.add_argument("--url", required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--folder", required=True)
    parser.add_argument("--environment", default=ENVIRONMENT)
    parser.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    return parser


def _create_folder(admin: InfisicalAdmin, project_id: str, folder: str) -> None:
    try:
        admin.create_folder(project_id, folder)
    except BootstrapError as exc:
        message = str(exc)
        exists = ("HTTP 400" in message or "HTTP 409" in message) and "already exists" in message
        if not exists:
            raise


def run(
    argv: Sequence[str] | None = None,
    *,
    admin: InfisicalAdmin | None = None,
    environ: Mapping[str, str] = os.environ,
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        values = parse_pairs(args.pairs)
        token = environ.get("INFISICAL_TOKEN")
        if not token:
            raise BootstrapError("INFISICAL_TOKEN is required")
        admin = admin or InfisicalAdmin(args.url)
        admin.use_token(token)
        _create_folder(admin, args.project_id, args.folder)
        admin._request(
            "PATCH",
            "/api/v4/secrets/batch",
            {
                "projectId": args.project_id,
                "environment": args.environment,
                "secretPath": f"/{args.folder}",
                "mode": "upsert",
                "secrets": [
                    {"secretKey": key, "secretValue": value} for key, value in values.items()
                ],
            },
        )
    except BootstrapError as exc:
        print(f"infisical_secrets: {exc}", file=sys.stderr)
        return 1
    print(f"infisical_secrets: {len(values)} secret(s) set in /{args.folder}")
    return 0


def main() -> None:
    sys.exit(run())


if __name__ == "__main__":
    main()
