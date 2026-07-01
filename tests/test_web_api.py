from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from config import Settings
from db import Database
from webapp.app import create_app
from webapp.jobs import JobManager
from webapp.repositories import WebSearchRepository
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    LinkSearchResultSet,
    MarketSearchSummary,
)


class FakeJobManager:
    def __init__(self, repository: WebSearchRepository) -> None:
        self.repository = repository
        self.job_id = "fakejob1234567890abcdef1234567890ab"
        self.running_job_id = "runningjob1234567890abcdef123456789"
        self.paginated_job_id = "pagejob1234567890abcdef1234567890ab"
        self._seed_job()

    def _seed_job(self) -> None:
        request = LinkSearchRequest(
            markets=("Euronext Paris",),
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 30),
        )
        self.repository.create_job(self.job_id, request)
        self.repository.mark_job_running(self.job_id)
        self.repository.upsert_market_run(
            self.job_id,
            MarketSearchSummary(
                market="Euronext Paris",
                source="fake-oam",
                status="ok",
                documents_count=1,
            ),
        )
        self.repository.replace_results(
            self.job_id,
            (
                LinkSearchDocument(
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
                    issuer_isin="FR0000000001",
                    issuer_lei="",
                    category="",
                    file_format="pdf",
                    date_confidence="high",
                    source_publication_date_raw="",
                ),
            ),
        )
        self.repository.finish_job(
            self.job_id,
            status="done",
            results_count=1,
            warnings=(),
            errors=(),
        )
        self.repository.create_job(
            self.running_job_id,
            LinkSearchRequest(
                markets=("Euronext Paris",),
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
            ),
        )
        self.repository.mark_job_running(self.running_job_id)
        self.repository.create_job(
            self.paginated_job_id,
            LinkSearchRequest(
                markets=("Euronext Paris",),
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
            ),
        )
        paginated_documents = tuple(
            LinkSearchDocument(
                market="Euronext Paris",
                source="fake-oam",
                source_document_id=f"doc-page-{index}",
                published_at=f"2026-06-{(index % 28) + 1:02d}",
                period_end_date="",
                reporting_year="",
                document_type="annual_financial_report",
                classification="",
                title=f"Paginated report {index}",
                url=f"https://official.test/report-{index}.pdf",
                issuer_name=f"Issuer {index}",
                issuer_isin=f"FR{index:010d}",
                issuer_lei="",
                category="",
                file_format="pdf",
                date_confidence="high",
                source_publication_date_raw="",
            )
            for index in range(51)
        )
        self.repository.replace_results(self.paginated_job_id, paginated_documents)
        self.repository.finish_job(
            self.paginated_job_id,
            status="done",
            results_count=len(paginated_documents),
            warnings=(),
            errors=(),
        )

    def submit(self, request: LinkSearchRequest) -> str:
        self.repository.create_job("newjob1234567890abcdef1234567890ab", request)
        return "newjob1234567890abcdef1234567890ab"

    def get_status(self, job_id: str) -> dict[str, object] | None:
        job = self.repository.get_job(job_id)
        if job is None:
            return None
        markets = self.repository.list_market_runs(job_id)
        return {
            "job_id": job_id,
            "status": job["status"],
            "results_count": job["results_count"],
            "warnings": job["warnings"],
            "errors": job["errors"],
            "markets": markets,
        }

    def cancel(self, job_id: str) -> bool:
        return False

    def shutdown(self) -> None:
        return None


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    settings = Settings(
        db_path=tmp_path / "api.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )
    database = Database(settings.db_path)
    database.initialize_web_search_schema()
    repository = WebSearchRepository(database)
    app = create_app(
        settings=settings,
        database=database,
        job_manager=FakeJobManager(repository),  # type: ignore[arg-type]
    )
    return TestClient(app)


def test_get_markets(client: TestClient) -> None:
    response = client.get("/api/markets")
    assert response.status_code == 200
    markets = response.json()["markets"]
    assert "Euronext Paris" in markets


def test_post_search_returns_job_id(client: TestClient) -> None:
    response = client.post(
        "/api/searches",
        json={
            "markets": ["Euronext Paris"],
            "date_from": "2026-06-01",
            "date_to": "2026-06-30",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_id"]
    assert payload["status_url"].endswith(payload["job_id"])
    assert payload["results_url"].endswith("/results")


def test_get_search_status(client: TestClient) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(f"/api/searches/{job_id}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "done"
    assert payload["results_count"] == 1


def test_get_unknown_search_returns_404(client: TestClient) -> None:
    response = client.get("/api/searches/unknown")
    assert response.status_code == 404


def test_get_search_results_paginates(client: TestClient) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(
        f"/api/searches/{job_id}/results",
        params={"page": 1, "page_size": 1},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["page"] == 1
    assert payload["page_size"] == 1
    assert len(payload["results"]) == 1


def test_get_document_types(client: TestClient) -> None:
    response = client.get("/api/document-types")
    assert response.status_code == 200
    values = [item["value"] for item in response.json()["document_types"]]
    assert "annual_financial_report" in values


def test_get_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_get_home_contains_form(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="search-form"' in response.text
    assert "Lancer la recherche" in response.text


def test_get_results_page_with_fake_job(client: TestClient) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(f"/searches/{job_id}")
    assert response.status_code == 200
    assert 'hx-trigger="load, every 2s"' in response.text
    assert "Annual report" in response.text
    partial = client.get(f"/partials/searches/{job_id}/results")
    assert partial.status_code == 200
    assert 'rel="noopener noreferrer"' in partial.text


def test_terminal_results_page_does_not_auto_poll_results(
    client: TestClient,
) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(f"/searches/{job_id}")

    soup = BeautifulSoup(response.text, "html.parser")
    status = soup.select_one("#job-status .job-status")
    results = soup.select_one("#results-table")

    assert status is not None
    assert status["data-terminal"] == "true"
    assert results is not None
    assert not results.has_attr("hx-trigger")
    assert not results.has_attr("hx-get")


def test_running_results_page_polls_with_current_filters(
    client: TestClient,
) -> None:
    job_id = "runningjob1234567890abcdef123456789"
    response = client.get(f"/searches/{job_id}")

    soup = BeautifulSoup(response.text, "html.parser")
    status = soup.select_one("#job-status .job-status")
    results = soup.select_one("#results-table")

    assert status is not None
    assert status["data-terminal"] == "false"
    assert results is not None
    assert results["hx-get"] == f"/partials/searches/{job_id}/results"
    assert "infofinShouldPollResults()" in results["hx-trigger"]
    assert results["hx-include"] == "#results-filters"


def test_results_pagination_uses_htmx_values_and_form_filters(
    client: TestClient,
) -> None:
    job_id = "pagejob1234567890abcdef1234567890ab"
    response = client.get(
        f"/partials/searches/{job_id}/results",
        params={"page": 1, "page_size": 50},
    )

    soup = BeautifulSoup(response.text, "html.parser")
    next_button = soup.select_one(".pagination button")

    assert next_button is not None
    assert next_button["hx-get"] == f"/partials/searches/{job_id}/results"
    assert next_button["hx-include"] == "#results-filters"
    assert '"page": 2' in next_button["hx-vals"]
    assert "?" not in next_button["hx-get"]
