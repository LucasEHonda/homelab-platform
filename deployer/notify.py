import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, message: str) -> None: ...


class NtfyNotifier:
    def __init__(self, url: str | None, client: httpx.Client | None = None) -> None:
        self._url = url
        self._client = (client or httpx.Client(timeout=10)) if url else None

    def send(self, message: str) -> None:
        if not self._url:
            return
        try:
            self._client.post(self._url, content=message.encode()).raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("ntfy notification failed: %s", type(exc).__name__)
