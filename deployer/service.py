import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from deployer.config import AppConfig, PlatformConfig
from deployer.manifest import (
    ManifestError,
    load_manifest,
    read_template_keys,
    resolve_in_app_dir,
)
from deployer.notify import Notifier
from deployer.policy import PolicyError, check_compose, validate_image_ref
from deployer.runner import ComposeTarget, Docker, DockerError
from deployer.secrets import (
    InfisicalClient,
    MissingSecretsError,
    SecretsError,
    ensure_required,
    render_env_file,
)

logger = logging.getLogger(__name__)

VERSION = re.compile(r"^v\d+\.\d+\.\d+$")


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    LIVE = "live"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class DeployRequest:
    app: str
    version: str
    images: Mapping[str, str]
    bundle: str


@dataclass
class Deployment:
    id: str
    app: str
    version: str
    status: DeploymentStatus
    detail: str
    created_at: str


class RequestError(Exception):
    pass


class ConflictError(Exception):
    pass


class _Blocked(Exception):
    pass


class _Failed(Exception):
    pass


class SecretsSource(Protocol):
    def fetch(self, app: AppConfig, folder: str) -> dict[str, str]: ...


class InfisicalSecretsSource:
    def __init__(self, client: InfisicalClient, environ: Mapping[str, str]) -> None:
        self._client = client
        self._environ = environ

    def fetch(self, app: AppConfig, folder: str) -> dict[str, str]:
        client_id = self._environ.get(app.infisical.client_id_env)
        client_secret = self._environ.get(app.infisical.client_secret_env)
        if not client_id or not client_secret:
            raise SecretsError(f"credentials for {app.name} are not configured")
        token = self._client.login(client_id, client_secret)
        return self._client.list_secrets(
            token, app.infisical.project_id, app.infisical.environment, folder
        )


class DeploymentService:
    def __init__(
        self,
        config: PlatformConfig,
        docker: Docker,
        secrets: SecretsSource,
        notifier: Notifier,
        history_path: Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = 5.0,
        keep_releases: int = 5,
        run_async: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._config = config
        self._docker = docker
        self._secrets = secrets
        self._notifier = notifier
        self._history_path = history_path
        self._sleep = sleep
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._keep_releases = keep_releases
        self._run_async = run_async or (
            lambda fn: threading.Thread(target=fn, daemon=True).start()
        )
        self._deployments: dict[str, Deployment] = {}
        self._locks: dict[str, threading.Lock] = {
            name: threading.Lock() for name in config.apps
        }

    def submit(self, request: DeployRequest) -> Deployment:
        if request.app not in self._config.apps:
            raise RequestError(f"unknown app {request.app}")
        app = self._config.apps[request.app]
        if VERSION.fullmatch(request.version) is None:
            raise RequestError("version must look like vX.Y.Z")
        if not request.images:
            raise RequestError("images are required")
        try:
            for ref in (*request.images.values(), request.bundle):
                validate_image_ref(ref, app.image_prefixes)
        except PolicyError as exc:
            raise RequestError(str(exc)) from exc
        if not self._locks[app.name].acquire(blocking=False):
            raise ConflictError(f"a deployment of {app.name} is already running")
        deployment = Deployment(
            id=uuid.uuid4().hex,
            app=app.name,
            version=request.version,
            status=DeploymentStatus.QUEUED,
            detail="",
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        self._deployments[deployment.id] = deployment
        self._run_async(lambda: self._run(deployment, request))
        return deployment

    def get(self, deployment_id: str) -> Deployment | None:
        return self._deployments.get(deployment_id)

    def _point(self, app_dir: Path, link: Path, target: str) -> None:
        tmp = app_dir / ".deploy.tmp"
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, link)

    def _prune(self, releases: Path, current: str, previous: str | None) -> None:
        try:
            children = [child for child in releases.iterdir() if child.is_dir()]
            children.sort(key=lambda child: child.stat().st_mtime, reverse=True)
            keep = {child.name for child in children[: self._keep_releases]}
            keep.add(current)
            if previous is not None:
                keep.add(previous)
            for child in children:
                if child.name not in keep:
                    shutil.rmtree(child)
        except OSError:
            pass

    def _write_env_file(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(tmp, path)

    def _collect_env_files(
        self, app: AppConfig, manifest, release_dir: Path
    ) -> dict[Path, str]:
        files: dict[Path, str] = {}
        for relative, folder in manifest.env_files.items():
            path = resolve_in_app_dir(app.app_dir, relative)
            template = release_dir / "envs" / f"{path.name}.example"
            required = read_template_keys(template) if template.exists() else []
            values = self._secrets.fetch(app, folder)
            ensure_required(values, required, folder)
            files[path] = render_env_file(values)
        return files

    def _run(self, deployment: Deployment, request: DeployRequest) -> None:
        app = self._config.apps[request.app]
        app_dir = app.app_dir
        releases = app_dir / ".releases"
        release_dir = releases / f"{request.version}-{deployment.id[:8]}"
        deploy_link = app_dir / "deploy"
        switched = False
        started = False
        previous: str | None = None
        notify_detail = ""
        deployment.status = DeploymentStatus.RUNNING
        try:
            releases.mkdir(parents=True, exist_ok=True)
            self._docker.pull(request.bundle)
            self._docker.extract_bundle(request.bundle, release_dir)

            manifest = load_manifest(release_dir / "deploy.yml")
            if set(request.images) != set(manifest.image_vars):
                raise _Blocked(f"images must be exactly {sorted(manifest.image_vars)}")

            (release_dir / ".env").write_text(
                render_env_file(
                    {
                        "APP_VERSION": request.version,
                        "RELEASE_DIR": str(release_dir),
                        **request.images,
                    }
                )
            )

            # compose config needs every env_file to exist, so secrets are written first.
            try:
                files = self._collect_env_files(app, manifest, release_dir)
            except MissingSecretsError as exc:
                notify_detail = str(exc)
                raise _Blocked(str(exc)) from exc

            for path, content in files.items():
                self._write_env_file(path, content)

            if not deploy_link.exists() and not deploy_link.is_symlink():
                deploy_link.mkdir()
            project_dir = deploy_link
            staged = ComposeTarget(
                manifest.project,
                release_dir / manifest.compose,
                project_dir,
                release_dir / ".env",
            )
            rendered = self._docker.compose_config(staged)
            violations = check_compose(
                rendered, app_dir, app.image_prefixes, self._config.shared_networks
            )
            if violations:
                raise _Blocked("; ".join(violations))

            services = rendered.get("services") or {}
            for service_name in (manifest.migrate_service, manifest.health_service):
                if service_name not in services:
                    raise _Blocked(f"service {service_name} is not in {manifest.compose}")

            if manifest.backup is not None and manifest.backup.shared:
                result = self._docker.exec(
                    "platform-data-backup-1",
                    ["sh", "/usr/local/bin/pg-backup.sh", "now", request.app],
                )
                if result.returncode != 0:
                    raise _Failed("database backup failed")
            elif manifest.backup is not None:
                container = f"{manifest.project}-{manifest.backup.service}-1"
                if self._docker.inspect(container, "{{.State.Running}}") == "true":
                    result = self._docker.exec(container, manifest.backup.command)
                    if result.returncode != 0:
                        raise _Failed("database backup failed")

            if deploy_link.is_symlink():
                previous = os.readlink(deploy_link)
            elif deploy_link.is_dir():
                legacy = releases / f"legacy-{deployment.id[:8]}"
                os.rename(deploy_link, legacy)
                previous = os.path.relpath(legacy, app_dir)
            self._point(app_dir, deploy_link, os.path.relpath(release_dir, app_dir))
            switched = True

            live = ComposeTarget(
                manifest.project,
                deploy_link / manifest.compose,
                deploy_link,
                deploy_link / ".env",
            )
            self._docker.compose_pull(live)
            started = True
            try:
                self._docker.compose_up(live)
            except DockerError as exc:
                up_error: str | None = str(exc)
            else:
                up_error = None

            migrate_image = rendered["services"][manifest.migrate_service]["image"]
            health_image = rendered["services"][manifest.health_service]["image"]
            deadline = self._clock() + manifest.health_timeout_seconds
            migrate_failed = False
            up_failed = False
            while True:
                m = self._docker.inspect(
                    f"{manifest.project}-{manifest.migrate_service}-1",
                    "{{.State.Status}} {{.State.ExitCode}} {{.Config.Image}}",
                )
                if m:
                    parts = m.split()
                    if (
                        len(parts) == 3
                        and parts[0] == "exited"
                        and parts[1] != "0"
                        and parts[2] == migrate_image
                    ):
                        migrate_failed = True
                        break
                h = self._docker.inspect(
                    f"{manifest.project}-{manifest.health_service}-1",
                    "{{.Config.Image}} {{.State.Health.Status}}",
                )
                if h == f"{health_image} healthy":
                    break
                if up_error is not None:
                    up_failed = True
                    break
                if self._clock() >= deadline:
                    raise _Failed(
                        f"{manifest.health_service} did not become healthy "
                        f"in {manifest.health_timeout_seconds}s"
                    )
                self._sleep(self._poll_seconds)

            if migrate_failed or up_failed:
                if previous is None:
                    raise _Failed(up_error if up_failed else "migrations failed")
                reason = "compose up failed" if up_failed else "migrations failed"
                self._point(app_dir, deploy_link, previous)
                try:
                    self._docker.compose_up(live)
                    detail = f"{reason}; back on {Path(previous).name}"
                except DockerError:
                    detail = f"{reason}; rollback to {Path(previous).name} failed"
                deployment.status = (
                    DeploymentStatus.FAILED
                    if up_failed
                    else DeploymentStatus.ROLLED_BACK
                )
                deployment.detail = detail
            else:
                deployment.status = DeploymentStatus.LIVE
                deployment.detail = ""
                self._prune(
                    releases,
                    current=release_dir.name,
                    previous=Path(previous).name if previous else None,
                )
        except _Blocked as exc:
            deployment.status = DeploymentStatus.BLOCKED
            deployment.detail = str(exc)
            if not switched and release_dir.exists():
                shutil.rmtree(release_dir, ignore_errors=True)
        except (
            _Failed,
            DockerError,
            ManifestError,
            SecretsError,
            PolicyError,
            OSError,
        ) as exc:
            deployment.status = DeploymentStatus.FAILED
            deployment.detail = str(exc)
            if switched and not started and previous is not None:
                try:
                    self._point(app_dir, deploy_link, previous)
                except OSError:
                    pass
            if not switched and release_dir.exists():
                shutil.rmtree(release_dir, ignore_errors=True)
        except Exception:
            logger.exception("deployment %s of %s crashed", deployment.id, deployment.app)
            deployment.status = DeploymentStatus.FAILED
            deployment.detail = "internal error"
            if switched and not started and previous is not None:
                try:
                    self._point(app_dir, deploy_link, previous)
                except OSError:
                    pass
            if not switched and release_dir.exists():
                shutil.rmtree(release_dir, ignore_errors=True)
        finally:
            try:
                self._history_path.parent.mkdir(parents=True, exist_ok=True)
                entry = {
                    "id": deployment.id,
                    "app": deployment.app,
                    "version": deployment.version,
                    "status": deployment.status.value,
                    "detail": deployment.detail,
                    "created_at": deployment.created_at,
                    "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                }
                with self._history_path.open("a") as handle:
                    handle.write(json.dumps(entry) + "\n")
            except OSError:
                pass
            message = f"{deployment.app} {deployment.version}: {deployment.status.value}"
            if deployment.status == DeploymentStatus.BLOCKED:
                if notify_detail:
                    message += f" ({notify_detail})"
                else:
                    message += " (see the Deploy job)"
            self._notifier.send(message)
            self._locks[app.name].release()
