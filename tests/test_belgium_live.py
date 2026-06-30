from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import Settings
from connectors.base import ConnectorState
from connectors.belgium_fsma_stori import BelgiumFsmaStoriConnector
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


def test_belgium_stori_live_diagnose_discover_and_download(
    tmp_path: Path,
) -> None:
    settings = Settings(
        db_path=tmp_path / "belgium-live.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=45,
        http_retries=2,
        http_backoff_factor=0.5,
        user_agent="InfoFin Belgium live integration test",
        max_download_bytes=250 * 1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
        belgium_rate_limit_seconds=0.1,
        belgium_lookback_days=900,
    )
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "AB INBEV",
                "BE0974293251",
                "ABI",
                "Euronext Brussels",
            )
        ]
    )
    issuer = database.list_issuers("Euronext Brussels")[0]
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
    )
    connector = BelgiumFsmaStoriConnector(
        session=session,
        base_url=settings.belgium_fsma_stori_base_url,
        rate_limit_seconds=settings.belgium_rate_limit_seconds,
        lookback_days=settings.belgium_lookback_days,
        timeout=settings.http_timeout_seconds,
    )

    try:
        diagnostic = connector.diagnose()
        assert diagnostic.state in {
            ConnectorState.READY,
            ConnectorState.DEGRADED,
        }
        assert diagnostic.detected_count > 0
        discovery = connector.discover("annual financial report")
        assert discovery.notices
        candidates = connector.search_documents(issuer)
        pdf = next(
            candidate
            for candidate in candidates
            if candidate.metadata.get("file_format") == "pdf"
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
    assert "belgium" in first.path.parts
    assert second.status == "duplicate"
