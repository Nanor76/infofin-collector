from __future__ import annotations

from datetime import date
from pathlib import Path

from db import Database
from webapp.repositories import WebSearchRepository
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    MarketSearchSummary,
)


def _make_repo(tmp_path: Path) -> WebSearchRepository:
    database = Database(tmp_path / "web.sqlite3")
    database.initialize_web_search_schema()
    return WebSearchRepository(database)


def _sample_document(**overrides) -> LinkSearchDocument:
    defaults = {
        "market": "Euronext Paris",
        "source": "fake-oam",
        "source_document_id": "doc-1",
        "published_at": "2026-06-12",
        "period_end_date": "",
        "reporting_year": 2025,
        "document_type": "annual_financial_report",
        "classification": "",
        "title": "Annual report",
        "url": "https://official.test/report.pdf",
        "issuer_name": "Issuer A",
        "issuer_isin": "FR0000000001",
        "issuer_lei": "",
        "category": "annual",
        "file_format": "pdf",
        "date_confidence": "high",
        "source_publication_date_raw": "",
        "metadata": {"issuer_name": "Issuer A"},
    }
    defaults.update(overrides)
    return LinkSearchDocument(**defaults)


def test_initialize_web_search_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "web.sqlite3")
    database.initialize_web_search_schema()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "web_search_jobs" in tables
    assert "web_search_market_runs" in tables
    assert "web_search_results" in tables


def test_create_and_get_job(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    repo.finish_job(
        "job-1",
        status="done",
        results_count=1,
        warnings=("warn",),
        errors=("err",),
    )
    job = repo.get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["warnings"] == ["warn"]
    assert job["errors"] == ["err"]
    assert job["request"].markets == ("Euronext Paris",)


def test_replace_results_and_list_paginated(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    documents = tuple(_sample_document() for _ in range(3))
    repo.replace_results("job-1", documents)
    page1, total = repo.list_results("job-1", page=1, page_size=2)
    assert total == 3
    assert len(page1) == 2
    page2, _ = repo.list_results("job-1", page=2, page_size=2)
    assert len(page2) == 1


def test_replace_results_overwrites_previous_rows(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    repo.replace_results("job-1", (_sample_document(),))
    repo.replace_results("job-1", ())
    results, total = repo.list_results("job-1")
    assert total == 0
    assert results == []


def test_list_results_filters(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris", "Oslo Børs"),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    repo.replace_results(
        "job-1",
        (
            _sample_document(),
            _sample_document(
                market="Oslo Børs",
                source="oslo",
                document_type="half_year_financial_report",
                title="Half year",
                issuer_name="Issuer B",
            ),
        ),
    )
    by_type, _ = repo.list_results(
        "job-1",
        document_type="half_year_financial_report",
    )
    assert len(by_type) == 1
    by_market, _ = repo.list_results("job-1", market="Oslo Børs")
    assert len(by_market) == 1
    by_source, _ = repo.list_results("job-1", source="fake-oam")
    assert len(by_source) == 1
    by_query, _ = repo.list_results("job-1", q="issuer b")
    assert len(by_query) == 1


def test_list_results_sort_whitelist(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    repo.replace_results(
        "job-1",
        (
            _sample_document(title="B report", published_at="2026-06-10"),
            _sample_document(title="A report", published_at="2026-06-12"),
        ),
    )
    results, _ = repo.list_results("job-1", sort="title")
    assert results[0]["title"] == "A report"
    results_desc, _ = repo.list_results("job-1", sort="-published_at")
    assert results_desc[0]["published_at"] == "2026-06-12"


def test_upsert_market_run_and_purge(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    request = LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )
    repo.create_job("job-1", request)
    repo.upsert_market_run(
        "job-1",
        MarketSearchSummary(
            market="Euronext Paris",
            source="fake-oam",
            status="ok",
            candidates_returned=2,
            documents_count=1,
        ),
    )
    runs = repo.list_market_runs("job-1")
    assert len(runs) == 1
    assert runs[0]["market"] == "Euronext Paris"
    deleted = repo.purge_jobs_older_than("9999-12-31T00:00:00+00:00")
    assert deleted == 1