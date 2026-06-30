from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path

import pytest

from config import Settings
from connectors.base import ConnectorState
from connectors.france_info_financiere import (
    FranceInfoFinanciereConnector,
    detect_field_role,
)
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


def test_france_source_downloads_and_persists_real_document(
    tmp_path: Path,
) -> None:
    base_url = os.getenv(
        "AMF_ODS_BASE_URL",
        "https://www.info-financiere.gouv.fr",
    )
    dataset = os.getenv("AMF_ODS_DATASET", "flux-amf-new-prod")
    session = build_http_session(
        retries=2,
        backoff_factor=0.5,
        user_agent="InfoFin live integration test",
    )
    connector = FranceInfoFinanciereConnector(
        session=session,
        base_url=base_url,
        fallback_base_urls=("https://www.info-financiere.gouv.fr",),
        dataset=dataset,
        rows=100,
        timeout=30,
    )
    database = Database(tmp_path / "infofin-live.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Air Liquide",
                "FR0000120073",
                "AI",
                "Euronext Paris",
            )
        ]
    )
    issuer = database.list_issuers("Euronext Paris")[0]

    try:
        diagnostic = connector.diagnose()
        assert diagnostic.state == ConnectorState.READY
        assert diagnostic.total_count is not None
        assert diagnostic.total_count >= 1
        assert diagnostic.example_record
        assert diagnostic.fields
        detected_roles = {
            detect_field_role(field) for field in diagnostic.fields
        }
        assert {
            "isin",
            "company",
            "title",
            "information_type",
            "information_subtype",
            "url",
            "date",
        } <= detected_roles

        candidates = connector.search_documents(issuer)
        assert candidates, "Aucun document financier réel trouvé"
        candidate = next(
            (
                item
                for item in candidates
                if item.url.casefold().split("?", 1)[0].endswith(".pdf")
            ),
            None,
        )
        assert candidate is not None, "Aucun PDF officiel réel trouvé"

        downloader = DocumentDownloader(
            database=database,
            session=session,
            data_dir=tmp_path / "raw",
            timeout=30,
            max_download_bytes=50 * 1024 * 1024,
        )
        result = downloader.download(issuer, candidate)
    finally:
        session.close()

    assert result.status == "downloaded"
    assert result.path is not None and result.path.is_file()
    assert result.sha256 is not None and len(result.sha256) == 64
    assert result.file_size >= 1
    with result.path.open("rb") as handle:
        actual_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    assert actual_sha256 == result.sha256
    stored = database.get_document_by_sha256(result.sha256)
    assert stored is not None
    assert stored["sha256"] == actual_sha256
    assert stored["local_path"] == str(result.path)
    assert stored["source_url"] == candidate.url


def test_france_watcher_is_idempotent_live(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "watch-live.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=30,
        http_retries=2,
        http_backoff_factor=0.5,
        user_agent="InfoFin live watcher test",
        max_download_bytes=250 * 1024 * 1024,
        amf_base_url=os.getenv(
            "AMF_ODS_BASE_URL",
            "https://www.info-financiere.gouv.fr",
        ),
        amf_fallback_base_urls=(
            "https://www.info-financiere.gouv.fr",
        ),
        amf_dataset=os.getenv(
            "AMF_ODS_DATASET",
            "flux-amf-new-prod",
        ),
        amf_rows=100,
    )
    database = Database(settings.db_path)
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "Air Liquide",
                "FR0000120073",
                "AI",
                "Euronext Paris",
            )
        ]
    )

    first = run_watch(
        database,
        settings,
        market="Euronext Paris",
        since=date(2026, 1, 1),
        limit=1,
        reports_dir=tmp_path / "reports",
    )
    second = run_watch(
        database,
        settings,
        market="Euronext Paris",
        since=date(2026, 1, 1),
        limit=1,
        reports_dir=tmp_path / "reports",
    )

    assert first.status == "success"
    assert first.stats.downloaded == 1
    assert second.status == "success"
    assert second.stats.downloaded == 0
    assert second.stats.duplicates == 1
    with database.connect() as connection:
        document = connection.execute(
            "SELECT local_path, sha256 FROM documents"
        ).fetchone()
        runs = connection.execute(
            """
            SELECT downloaded, duplicates
            FROM watch_runs ORDER BY id
            """
        ).fetchall()
    assert document is not None
    assert Path(document["local_path"]).is_file()
    assert len(document["sha256"]) == 64
    assert [tuple(row) for row in runs] == [(1, 0), (0, 1)]
