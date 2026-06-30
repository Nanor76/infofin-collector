from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import Settings
from connectors.base import ConnectorState
from connectors.italy_emarketstorage import ItalyEmarketStorageConnector
from db import Database
from download import DocumentDownloader
from http_client import build_http_session
from models import Issuer

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_TESTS") != "1",
        reason="RUN_LIVE_TESTS=1 requis",
    ),
]


def live_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "italy-live.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=30,
        http_retries=2,
        http_backoff_factor=0.5,
        user_agent="InfoFin Italy live integration test",
        max_download_bytes=200 * 1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
        italy_rate_limit_seconds=0.2,
        italy_lookback_days=900,
        italy_max_pages=1,
    )


def live_connector(
    settings: Settings,
    session: object,
) -> ItalyEmarketStorageConnector:
    return ItalyEmarketStorageConnector(
        session=session,
        home_url=settings.italy_home_url,
        press_releases_url=settings.italy_press_releases_url,
        documents_url=settings.italy_documents_url,
        oneinfo_url=settings.italy_1info_url,
        borsa_company_base_url=settings.italy_borsa_company_base_url,
        market="Euronext Milan",
        rate_limit_seconds=settings.italy_rate_limit_seconds,
        lookback_days=settings.italy_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.italy_verify_ssl,
        max_pages=settings.italy_max_pages,
    )


def test_italy_source_discovers_and_downloads_real_pdf(
    tmp_path: Path,
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "MONDO TV",
                "IT0001447785",
                "MTV",
                "Euronext Milan",
            )
        ]
    )
    issuer = database.list_issuers("Euronext Milan")[0]
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
    )
    connector = live_connector(settings, session)

    try:
        diagnostic = connector.diagnose()
        assert diagnostic.state in {
            ConnectorState.READY,
            ConnectorState.DEGRADED,
        }
        assert diagnostic.example_document
        discovery = connector.discover("relazione finanziaria annuale")
        assert discovery.candidates
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
        first = downloader.download(issuer, pdf)
        second = downloader.download(issuer, pdf)
    finally:
        session.close()

    assert first.status == "downloaded"
    assert first.path is not None and first.path.is_file()
    assert "italy" in first.path.parts
    assert second.status == "duplicate"
