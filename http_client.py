from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
import logging
from typing import Any, Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("http_client")


class RequestLimitExceeded(RuntimeError):
    pass


class InfoFinSession(requests.Session):
    def __init__(self, verify: bool = True) -> None:
        super().__init__()
        self.verify = verify
        self.ssl_disabled_sources: set[str] = set()
        self.current_source: str = "unassigned"

    @contextmanager
    def source(self, source: str) -> Iterator[None]:
        previous = self.current_source
        self.current_source = source
        try:
            yield
        finally:
            self.current_source = previous

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any) -> Any:
        verify = kwargs.get("verify", self.verify)
        if verify is False:
            source = self.current_source
            if source not in self.ssl_disabled_sources:
                self.ssl_disabled_sources.add(source)
                logger.warning(
                    f"AVERTISSEMENT: La vérification SSL/TLS est désactivée pour la source '{source}'. "
                    "Ceci présente un risque de sécurité. Veuillez corriger la configuration locale (certificats/proxy)."
                )
        return super().request(method, url, *args, **kwargs)


class RequestCountingSession:
    def __init__(
        self,
        session: Any,
        *,
        max_requests: int = 500,
        allow_large_run: bool = False,
    ) -> None:
        self._session = session
        self.max_requests = max_requests
        self.allow_large_run = allow_large_run
        self.total_requests = 0
        self.requests_by_source: dict[str, int] = defaultdict(int)
        self.limit_exceeded = False

        if not hasattr(session, "current_source"):
            session.current_source = "unassigned"
        if not hasattr(session, "ssl_disabled_sources"):
            session.ssl_disabled_sources = set()

    @property
    def current_source(self) -> str:
        return getattr(self._session, "current_source", "unassigned")

    @current_source.setter
    def current_source(self, value: str) -> None:
        if hasattr(self._session, "current_source"):
            self._session.current_source = value

    @property
    def ssl_disabled_sources(self) -> set[str]:
        return getattr(self._session, "ssl_disabled_sources", set())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    @contextmanager
    def source(self, source: str) -> Iterator[None]:
        previous = self.current_source
        self.current_source = source
        try:
            yield
        finally:
            self.current_source = previous

    def _count(self) -> None:
        if (
            not self.allow_large_run
            and self.total_requests >= self.max_requests
        ):
            self.limit_exceeded = True
            raise RequestLimitExceeded(
                f"Le run dépasserait la limite de {self.max_requests} "
                "appels HTTP. Relancer avec --confirm-large-run uniquement "
                "pour un backfill contrôlé."
            )
        self.total_requests += 1
        self.requests_by_source[self.current_source] += 1

    def get(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return self._session.get(*args, **kwargs)

    def post(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return self._session.post(*args, **kwargs)

    def head(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return self._session.head(*args, **kwargs)

    def request(self, *args: Any, **kwargs: Any) -> Any:
        self._count()
        return self._session.request(*args, **kwargs)

    def raise_if_exceeded(self) -> None:
        if self.limit_exceeded:
            raise RequestLimitExceeded(
                f"Le run a atteint la limite de {self.max_requests} appels "
                "HTTP et a été interrompu. Utiliser --confirm-large-run "
                "uniquement si cette charge est intentionnelle."
            )


def build_http_session(
    *,
    retries: int,
    backoff_factor: float,
    user_agent: str,
    verify: bool = True,
) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = InfoFinSession(verify=verify)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json, application/pdf, application/xhtml+xml, "
            "application/zip, */*;q=0.5",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

