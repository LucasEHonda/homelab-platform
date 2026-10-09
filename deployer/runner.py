import json
import subprocess
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, timeout: float = 600) -> CommandResult: ...


class SubprocessRunner:
    def run(self, args: Sequence[str], *, timeout: float = 600) -> CommandResult:
        try:
            p = subprocess.run(
                list(args),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", f"timed out after {timeout}s")
        return CommandResult(p.returncode, p.stdout, p.stderr)


class DockerError(Exception):
    pass


@dataclass(frozen=True)
class ComposeTarget:
    project: str
    compose_file: Path
    project_dir: Path
    env_file: Path


class Docker:
    def __init__(self, runner: CommandRunner) -> None:
        self._runner = runner

    def _check(self, args: list[str], timeout: float = 600) -> CommandResult:
        result = self._runner.run(args, timeout=timeout)
        if result.returncode != 0:
            raise DockerError(
                f"{' '.join(args[:4])} failed: {result.stderr.strip()[-500:]}"
            )
        return result

    def _compose(self, target: ComposeTarget) -> list[str]:
        return [
            "docker",
            "compose",
            "-p",
            target.project,
            "-f",
            str(target.compose_file),
            "--project-directory",
            str(target.project_dir),
            "--env-file",
            str(target.env_file),
        ]

    def pull(self, ref: str) -> None:
        self._check(["docker", "pull", "--quiet", ref], timeout=1800)

    def extract_bundle(self, ref: str, dest: Path) -> None:
        if dest.exists():
            raise DockerError(f"{dest} already exists")
        name = f"bundle-{uuid.uuid4().hex[:12]}"
        self._check(["docker", "create", "--name", name, ref, "/bundle"])
        try:
            dest.mkdir(parents=True)
            self._check(["docker", "cp", f"{name}:/bundle/.", str(dest)])
        finally:
            self._runner.run(["docker", "rm", "-f", name])

    def compose_config(self, target: ComposeTarget) -> dict:
        result = self._check(
            self._compose(target)
            + ["config", "--format", "json", "--no-env-resolution"]
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise DockerError("compose config returned invalid JSON")

    def compose_pull(self, target: ComposeTarget) -> None:
        self._check(self._compose(target) + ["pull", "--quiet"], timeout=1800)

    def compose_up(self, target: ComposeTarget) -> None:
        self._check(
            self._compose(target) + ["up", "-d", "--remove-orphans"], timeout=1800
        )

    def exec(self, container: str, command: Sequence[str]) -> CommandResult:
        return self._runner.run(["docker", "exec", container, *command])

    def inspect(self, container: str, template: str) -> str | None:
        r = self._runner.run(["docker", "inspect", "-f", template, container])
        return r.stdout.strip() if r.returncode == 0 else None
