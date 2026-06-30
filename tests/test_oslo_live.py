from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from config import Settings
from connectors.base import ConnectorState
from connectors.oslo_newsweb import OsloNewsWebConnector
from db import Database
from download import DocumentDownloader
from http_client import build_http_session
from models import Issuer
from watcher import run_watch


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="RUN_LIVE_TESTS=1 requis",
    ),
]


def live_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "oslo-live.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=30,
        http_retries=2,
        http_backoff_factor=0.5,
        user_agent="InfoFin Oslo live integration test",
        max_download_bytes=100 * 1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
        oslo_euronext_news_url=os.getenv(
            "OSLO_EURONEXT_NEWS_URL",
            "https://live.euronext.com/en/markets/oslo/equities/company-news",
        ),
        oslo_newsweb_base_url=os.getenv(
            "OSLO_NEWSWEB_BASE_URL",
            "https://newsweb.oslobors.no",
        ),
        oslo_rate_limit_seconds=0.25,
        oslo_lookback_days=500,
    )


def test_oslo_source_resolves_and_downloads_real_pdf(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
    )
    connector = OsloNewsWebConnector(
        session=session,
        euronext_news_url=settings.oslo_euronext_news_url,
        newsweb_base_url=settings.oslo_newsweb_base_url,
        rate_limit_seconds=settings.oslo_rate_limit_seconds,
        lookback_days=settings.oslo_lookback_days,
        timeout=settings.http_timeout_seconds,
    )
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "2020 BULKERS",
                "BMG9156K1018",
                "2020",
                "Oslo Børs",
            )
        ]
    )
    issuer = database.list_issuers("Oslo Børs")[0]

    try:
        diagnostic = connector.diagnose()
        assert diagnostic.state in {
            ConnectorState.READY,
            ConnectorState.DEGRADED,
        }
        assert diagnostic.detected_count >= 1
        candidates = connector.search_documents(issuer)
        pdf = next(
            candidate
            for candidate in candidates
            if candidate.url.casefold().split("?", 1)[0].endswith(".pdf")
        )
        downloader = DocumentDownloader(
            database=database,
            session=session,
            data_dir=settings.data_dir,
            timeout=settings.http_timeout_seconds,
            max_download_bytes=settings.max_download_bytes,
        )
        result = downloader.download(issuer, pdf)
    finally:
        session.close()

    assert result.status == "downloaded"
    assert result.path is not None and result.path.is_file()
    assert result.sha256 is not None and len(result.sha256) == 64


def test_oslo_watcher_is_idempotent_live(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "2020 BULKERS",
                "BMG9156K1018",
                "2020",
                "Oslo Børs",
            )
        ]
    )
    since = date.today() - timedelta(days=settings.oslo_lookback_days)

    first = run_watch(
        database,
        settings,
        market="Oslo Børs",
        since=since,
        limit=1,
        reports_dir=tmp_path / "reports",
    )
    second = run_watch(
        database,
        settings,
        market="Oslo Børs",
        since=since,
        limit=1,
        reports_dir=tmp_path / "reports",
    )

    assert first.status == "success"
    assert first.stats.downloaded == 1
    assert second.status == "success"
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
