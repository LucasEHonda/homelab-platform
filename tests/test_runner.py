import re
from pathlib import Path

import pytest

from deployer.runner import (
    CommandResult,
    ComposeTarget,
    Docker,
    DockerError,
    SubprocessRunner,
)


class FakeRunner:
    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self._results = list(results or [])

    def run(self, args, *, timeout=600):
        self.calls.append(list(args))
        self.timeouts.append(timeout)
        if self._results:
            return self._results.pop(0)
        return CommandResult(0, "", "")


TARGET = ComposeTarget(
    project="games",
    compose_file=Path("/srv/games/compose.yml"),
    project_dir=Path("/srv/games"),
    env_file=Path("/srv/games/.env"),
)

COMPOSE = [
    "docker",
    "compose",
    "-p",
    "games",
    "-f",
    "/srv/games/compose.yml",
    "--project-directory",
    "/srv/games",
    "--env-file",
    "/srv/games/.env",
]


def test_pull_sends_args_and_raises_on_failure():
    runner = FakeRunner()
    Docker(runner).pull("ghcr.io/x")
    assert runner.calls == [["docker", "pull", "--quiet", "ghcr.io/x"]]
    assert runner.timeouts == [1800]
    failing = FakeRunner([CommandResult(1, "", "boom")])
    with pytest.raises(DockerError) as exc:
        Docker(failing).pull("ghcr.io/x")
    assert str(exc.value).startswith("docker pull --quiet ghcr.io/x failed:")


def test_extract_bundle_calls_in_order(tmp_path):
    runner = FakeRunner()
    dest = tmp_path / "rel"
    Docker(runner).extract_bundle("ghcr.io/x", dest)
    create, cp, rm = runner.calls
    match = re.fullmatch(r"bundle-[0-9a-f]{12}", create[3])
    assert match
    name = create[3]
    assert create == ["docker", "create", "--name", name, "ghcr.io/x", "/bundle"]
    assert cp == ["docker", "cp", f"{name}:/bundle/.", str(dest)]
    assert rm == ["docker", "rm", "-f", name]
    assert dest.is_dir()


def test_extract_bundle_cp_failure_still_removes(tmp_path):
    runner = FakeRunner([CommandResult(0, "", ""), CommandResult(1, "", "cp failed")])
    with pytest.raises(DockerError):
        Docker(runner).extract_bundle("ghcr.io/x", tmp_path / "rel")
    assert runner.calls[-1][:3] == ["docker", "rm", "-f"]


def test_extract_bundle_dest_exists(tmp_path):
    runner = FakeRunner()
    with pytest.raises(DockerError):
        Docker(runner).extract_bundle("ghcr.io/x", tmp_path)
    assert runner.calls == []


def test_compose_config_parses_json_and_rejects_invalid():
    runner = FakeRunner([CommandResult(0, '{"services": {}}', "")])
    assert Docker(runner).compose_config(TARGET) == {"services": {}}
    assert runner.calls == [COMPOSE + ["config", "--format", "json", "--no-env-resolution"]]
    bad = FakeRunner([CommandResult(0, "not json", "")])
    with pytest.raises(DockerError):
        Docker(bad).compose_config(TARGET)


def test_compose_up_args_and_timeout():
    runner = FakeRunner()
    Docker(runner).compose_up(TARGET)
    assert runner.calls == [COMPOSE + ["up", "-d", "--remove-orphans"]]
    assert runner.timeouts == [1800]


def test_exec_returns_result_on_failure():
    result = CommandResult(1, "out", "err")
    runner = FakeRunner([result])
    assert Docker(runner).exec("c1", ["ls", "-l"]) == result
    assert runner.calls == [["docker", "exec", "c1", "ls", "-l"]]


def test_inspect_returns_stripped_or_none():
    runner = FakeRunner([CommandResult(0, " healthy\n", ""), CommandResult(1, "", "no")])
    docker = Docker(runner)
    assert docker.inspect("c1", "{{.State.Health.Status}}") == "healthy"
    assert docker.inspect("c1", "{{.State.Health.Status}}") is None


def test_subprocess_runner_captures_and_times_out():
    result = SubprocessRunner().run(["sh", "-c", "echo hi; echo err >&2; exit 3"])
    assert (result.returncode, result.stdout, result.stderr) == (3, "hi\n", "err\n")
    assert SubprocessRunner().run(["sleep", "5"], timeout=0.2).returncode == 124
