from __future__ import annotations

import os

import pytest

from config import Settings
from connectors.base import ConnectorState
from connectors.portugal_cmvm_sdi import PortugalCmvmSdiConnector
from http_client import build_http_session

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_TESTS") != "1",
    reason="RUN_LIVE_TESTS=1 requis",
)


def test_portugal_cmvm_live_source() -> None:
    settings = Settings.from_env()
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
    )
    try:
        connector = PortugalCmvmSdiConnector(
            session=session,
            base_url=settings.portugal_cmvm_base_url,
            sdi_url=settings.portugal_cmvm_sdi_url,
            rate_limit_seconds=settings.portugal_rate_limit_seconds,
            lookback_days=settings.portugal_lookback_days,
            timeout=settings.http_timeout_seconds,
        )
        diagnostic = connector.diagnose()
        assert diagnostic.state in {
            ConnectorState.READY,
            ConnectorState.DEGRADED,
        }
        assert diagnostic.example_notice is not None
        assert connector.discover("relatório financeiro anual").notices
    finally:
        session.close()
