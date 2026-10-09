import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from deployer.config import PlatformConfig
from deployer.manifest import DatabaseSpec, ManifestError, load_manifest
from deployer.notify import Notifier
from deployer.runner import Docker

logger = logging.getLogger(__name__)

_MYSQL = (
    "MYSQL_PWD=\"$MYSQL_ROOT_PASSWORD\" mysql -uroot -e "
    "\"ALTER USER 'breakglass'@'%' ACCOUNT {action}\""
)
_PSQL = 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "{statement}"'


class BreakGlassError(Exception):
    pass


def _timer(seconds: float, fn: Callable[[], None]) -> None:
    t = threading.Timer(seconds, fn)
    t.daemon = True
    t.start()


class BreakGlassService:
    def __init__(
        self,
        config: PlatformConfig,
        docker: Docker,
        notifier: Notifier,
        *,
        duration_seconds: int = 3600,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        schedule: Callable[[float, Callable[[], None]], None] | None = None,
    ) -> None:
        self._config = config
        self._docker = docker
        self._notifier = notifier
        self._duration_seconds = duration_seconds
        self._now = now
        self._schedule = schedule or _timer

    def _target(self, app_name: str) -> tuple[str, DatabaseSpec]:
        app = self._config.apps.get(app_name)
        if app is None:
            raise BreakGlassError(f"unknown app {app_name}")
        try:
            manifest = load_manifest(app.app_dir / "deploy" / "deploy.yml")
        except ManifestError:
            raise BreakGlassError(f"{app_name} has no deployed release")
        if manifest.database is None:
            raise BreakGlassError(f"{app_name} has no database")
        return f"{manifest.project}-{manifest.database.service}-1", manifest.database

    def open(self, app_name: str, user: str) -> datetime:
        container, db = self._target(app_name)
        expires = self._now() + timedelta(seconds=self._duration_seconds)
        if db.engine == "mysql":
            command = ["sh", "-c", _MYSQL.format(action="UNLOCK")]
        else:
            statement = f"ALTER ROLE breakglass LOGIN VALID UNTIL '{expires.isoformat()}'"
            command = ["sh", "-c", _PSQL.format(statement=statement)]
        result = self._docker.exec(container, command)
        if result.returncode != 0:
            raise BreakGlassError(f"could not unlock breakglass on {app_name}")
        self._schedule(
            self._duration_seconds, lambda: self._lock(app_name, notify_success=True)
        )
        self._notifier.send(
            f"{app_name}: break-glass opened until {expires:%H:%M} UTC"
        )
        return expires

    def _lock(self, app_name: str, *, notify_success: bool) -> None:
        try:
            container, db = self._target(app_name)
        except BreakGlassError:
            return
        if db.engine == "mysql":
            command = ["sh", "-c", _MYSQL.format(action="LOCK")]
        else:
            command = ["sh", "-c", _PSQL.format(statement="ALTER ROLE breakglass NOLOGIN")]
        result = self._docker.exec(container, command)
        if result.returncode != 0:
            logger.error("break-glass lock failed for %s", app_name)
            self._notifier.send(f"{app_name}: break-glass lock FAILED")
        elif notify_success:
            self._notifier.send(f"{app_name}: break-glass closed")

    def lock_all(self) -> None:
        for name in self._config.apps:
            self._lock(name, notify_success=False)
