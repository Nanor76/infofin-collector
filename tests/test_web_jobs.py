from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import pytest

from db import Database
from webapp.jobs import JobManager, run_stored_search
from webapp.repositories import WebSearchRepository
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    LinkSearchResultSet,
    MarketSearchSummary,
)


class FakeSearchService:
    def __init__(self, result_set: LinkSearchResultSet) -> None:
        self.result_set = result_set
        self.calls = 0

    def search_links(self, request: LinkSearchRequest) -> LinkSearchResultSet:
        self.calls += 1
        return self.result_set


def _make_manager(
    tmp_path: Path,
    result_set: LinkSearchResultSet,
) -> tuple[JobManager, WebSearchRepository, FakeSearchService]:
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize_web_search_schema()
    repository = WebSearchRepository(database)
    search_service = FakeSearchService(result_set)
    manager = JobManager(
        repository=repository,
        search_service=search_service,  # type: ignore[arg-type]
        max_workers=1,
    )
    return manager, repository, search_service


def _request() -> LinkSearchRequest:
    return LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )


def _document() -> LinkSearchDocument:
    return LinkSearchDocument(
        market="Euronext Paris",
        source="fake-oam",
        source_document_id="doc-1",
        published_at="2026-06-12",
        period_end_date="",
        reporting_year="",
        document_type="annual_financial_report",
        classification="",
        title="Annual report",
        url="https://official.test/report.pdf",
        issuer_name="Issuer A",
        issuer_isin="",
        issuer_lei="",
        category="",
        file_format="pdf",
        date_confidence="high",
        source_publication_date_raw="",
    )


def _wait_for_status(manager: JobManager, job_id: str, expected: str) -> None:
    for _ in range(50):
        status = manager.get_status(job_id)
        assert status is not None
        if status["status"] == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"statut {expected} non atteint")


def test_submit_creates_done_job(tmp_path: Path) -> None:
    result_set = LinkSearchResultSet(
        request=_request(),
        documents=(_document(),),
        market_summaries=(
            MarketSearchSummary(
                market="Euronext Paris",
                source="fake-oam",
                status="ok",
                documents_count=1,
            ),
        ),
    )
    manager, repository, search_service = _make_manager(tmp_path, result_set)
    job_id = manager.submit(_request(), owner_id="alice")
    _wait_for_status(manager, job_id, "done")
    job = repository.get_job(job_id)
    assert job is not None
    assert job["results_count"] == 1
    assert job["owner_id"] == "alice"
    assert search_service.calls == 1
    manager.shutdown()


def test_submit_partial_when_errors_and_results(tmp_path: Path) -> None:
    result_set = LinkSearchResultSet(
        request=_request(),
        documents=(_document(),),
        market_summaries=(),
        errors=("Euronext Paris: boom",),
    )
    manager, _, _ = _make_manager(tmp_path, result_set)
    job_id = manager.submit(_request())
    _wait_for_status(manager, job_id, "partial")
    manager.shutdown()


def test_submit_failed_when_errors_without_results(tmp_path: Path) -> None:
    result_set = LinkSearchResultSet(
        request=_request(),
        documents=(),
        market_summaries=(),
        errors=("Euronext Paris: boom",),
    )
    manager, _, _ = _make_manager(tmp_path, result_set)
    job_id = manager.submit(_request())
    _wait_for_status(manager, job_id, "failed")
    manager.shutdown()


def test_cancel_on_finished_job_does_not_break(tmp_path: Path) -> None:
    result_set = LinkSearchResultSet(
        request=_request(),
        documents=(_document(),),
        market_summaries=(),
    )
    manager, _, _ = _make_manager(tmp_path, result_set)
    job_id = manager.submit(_request())
    _wait_for_status(manager, job_id, "done")
    assert manager.cancel(job_id) is False
    manager.shutdown()


class FailingSearchService:
    def search_links(self, request: LinkSearchRequest) -> LinkSearchResultSet:
        raise RuntimeError("unexpected worker failure")


def test_worker_failure_marks_job_failed(tmp_path: Path) -> None:
    database = Database(tmp_path / "failed-worker.sqlite3")
    database.initialize_web_search_schema()
    repository = WebSearchRepository(database)
    repository.create_job("job-failed", _request())

    with pytest.raises(RuntimeError, match="unexpected worker failure"):
        run_stored_search(
            repository=repository,
            search_service=FailingSearchService(),  # type: ignore[arg-type]
            job_id="job-failed",
            request=_request(),
        )

    job = repository.get_job("job-failed")
    assert job is not None
    assert job["status"] == "failed"
    assert job["errors"] == ["La recherche a échoué pendant son exécution."]
