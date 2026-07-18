from __future__ import annotations

from dataclasses import replace
from datetime import date
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from config import Settings
from db import Database
from webapp.app import create_app
from webapp.beta_access import hash_password
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
        self.queued_job_id = "queuedjob1234567890abcdef1234567890a"
        self.running_job_id = "runningjob1234567890abcdef123456789"
        self.alert_job_id = "alertjob1234567890abcdef12345678901"
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
            self.queued_job_id,
            LinkSearchRequest(
                markets=("Euronext Paris",),
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
            ),
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
            self.alert_job_id,
            LinkSearchRequest(
                markets=("Euronext Paris",),
                date_from=date(2026, 6, 1),
                date_to=date(2026, 6, 30),
            ),
        )
        self.repository.mark_job_running(self.alert_job_id)
        self.repository.upsert_market_run(
            self.alert_job_id,
            MarketSearchSummary(
                market="Euronext Paris",
                source="fake-oam",
                status="failed",
                warning="Market warning",
                error="Market error",
            ),
        )
        self.repository.finish_job(
            self.alert_job_id,
            status="partial",
            results_count=0,
            warnings=("Job warning",),
            errors=("Job error",),
        )
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
    assert payload["markets"] == [
        {
            "market": "Euronext Paris",
            "status": "ok",
            "results_count": 1,
            "warning": None,
            "error": None,
        }
    ]
    assert "fake-oam" not in response.text


def test_queued_search_is_publicly_reported_as_running(
    client: TestClient,
) -> None:
    job_id = "queuedjob1234567890abcdef1234567890a"

    api_response = client.get(f"/api/searches/{job_id}")
    page_response = client.get(f"/searches/{job_id}")

    assert api_response.status_code == 200
    assert api_response.json()["status"] == "running"
    assert page_response.status_code == 200
    soup = BeautifulSoup(page_response.text, "html.parser")
    status = soup.select_one('[data-testid="results-job-status"]')
    state = soup.select_one('[data-testid="results-job-state"]')
    assert status is not None
    assert status["data-status"] == "running"
    assert state is not None
    assert state.get_text(" ", strip=True) == "En cours"
    assert "queued" not in str(status)


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
    result = payload["results"][0]
    assert set(result) == {
        "market",
        "published_at",
        "period_end_date",
        "reporting_year",
        "document_type",
        "title",
        "issuer_name",
        "issuer_isin",
        "issuer_lei",
        "file_format",
        "document_url",
    }
    assert result["document_url"] == "https://official.test/report.pdf"
    assert "fake-oam" not in response.text
    assert "doc-1" not in response.text


def test_private_search_controls_are_not_exposed(client: TestClient) -> None:
    response = client.post(
        "/api/searches",
        json={
            "markets": ["Euronext Paris"],
            "date_from": "2026-06-01",
            "date_to": "2026-06-30",
            "sources": ["fake-oam"],
            "max_candidates": 1,
            "dedupe_url": False,
        },
    )

    assert response.status_code == 422
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404


def test_get_document_types(client: TestClient) -> None:
    response = client.get("/api/document-types")
    assert response.status_code == 200
    values = [item["value"] for item in response.json()["document_types"]]
    assert "annual_financial_report" in values


def test_get_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "storage_backend": "sqlite",
        "job_backend": "local",
    }


def test_password_protects_every_web_route(client: TestClient) -> None:
    settings = replace(
        client.app.state.settings,
        web_access_username="mobile-user",
        web_access_password="mobile-password",
    )
    app = create_app(
        settings=settings,
        database=client.app.state.database,
        repository=client.app.state.repository,
        job_manager=client.app.state.job_manager,
    )

    with TestClient(app) as protected_client:
        unauthenticated = protected_client.get("/api/health")
        wrong_password = protected_client.get(
            "/api/health",
            auth=("mobile-user", "wrong-password"),
        )
        authenticated = protected_client.get(
            "/api/health",
            auth=("mobile-user", "mobile-password"),
        )
        static_asset = protected_client.get(
            "/static/app.css",
            auth=("mobile-user", "mobile-password"),
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == (
        'Basic realm="InfoFin", charset="UTF-8"'
    )
    assert wrong_password.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.json()["status"] == "ok"
    assert static_asset.status_code == 200


def test_beta_pages_keep_stable_interactive_test_ids(
    client: TestClient,
) -> None:
    password = "alice secure password"
    settings = replace(
        client.app.state.settings,
        web_beta_users_json=json.dumps(
            {
                "alice": {
                    "display_name": "Alice",
                    "password_hash": hash_password(
                        password,
                        salt=b"0123456789abcdef",
                        iterations=1_000,
                    ),
                }
            }
        ),
        web_beta_session_secret="s" * 32,
        web_contact_email="beta@example.test",
    )
    owned_job_id = "ownedjob1234567890abcdef1234567890a"
    client.app.state.repository.create_job(
        owned_job_id,
        LinkSearchRequest(
            markets=("Euronext Paris",),
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 30),
        ),
        owner_id="alice",
    )
    client.app.state.repository.finish_job(
        owned_job_id,
        status="done",
        results_count=0,
        warnings=(),
        errors=(),
    )
    app = create_app(
        settings=settings,
        database=client.app.state.database,
        repository=client.app.state.repository,
        job_manager=client.app.state.job_manager,
    )

    with TestClient(app) as beta_client:
        login = beta_client.get("/login")
        beta_client.post(
            "/login",
            data={
                "username": "alice",
                "password": password,
                "next": "/",
            },
        )
        home = beta_client.get("/")
        results = beta_client.get(f"/searches/{owned_job_id}")
        mentions = beta_client.get("/legal/mentions")
        privacy = beta_client.get("/legal/privacy")

    for response in (login, home, results, mentions, privacy):
        soup = BeautifulSoup(response.text, "html.parser")
        assert not _interactive_elements_without_test_id(soup)

    _assert_test_ids(
        BeautifulSoup(login.text, "html.parser"),
        {
            "login-page",
            "login-heading",
            "login-form",
            "login-username-input",
            "login-password-input",
            "login-submit-button",
            "layout-legal-mentions-link",
            "layout-legal-privacy-link",
        },
    )
    _assert_test_ids(
        BeautifulSoup(home.text, "html.parser"),
        {
            "layout-beta-account",
            "layout-beta-user",
            "layout-logout-form",
            "layout-logout-button",
            "search-beta-quota-state",
            "search-beta-quota-used-value",
            "search-beta-quota-limit-value",
        },
    )
    _assert_test_ids(
        BeautifulSoup(results.text, "html.parser"),
        {
            "results-feedback-section",
            "results-feedback-form",
            "results-feedback-category-select",
            "results-feedback-message-input",
            "results-feedback-submit-button",
            "results-feedback-response-state",
        },
    )
    assert 'fetch("/api/feedback"' in (
        Path(__file__).parents[1] / "webapp" / "static" / "app.js"
    ).read_text(encoding="utf-8")


def test_get_home_contains_form(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="search-form"' in response.text
    assert "Lancer la recherche" in response.text
    assert "OAM" not in response.text


def test_home_interactive_elements_have_test_ids(client: TestClient) -> None:
    response = client.get("/")
    soup = BeautifulSoup(response.text, "html.parser")

    assert not _interactive_elements_without_test_id(soup)
    _assert_test_ids(
        soup,
        {
            "layout-header",
            "layout-home-link",
            "layout-main",
            "layout-footer",
            "search-page",
            "search-heading",
            "search-form",
            "search-market-section",
            "search-market-list",
            "search-market-option-oslo-bors",
            "search-market-checkbox-oslo-bors",
            "search-market-filter-input",
            "search-market-select-all-button",
            "search-market-select-none-button",
            "search-map",
            "search-map-loading",
            "search-map-tooltip",
            "search-map-zoom-in-button",
            "search-map-zoom-out-button",
            "search-map-reset-button",
            "search-date-section",
            "search-date-presets",
            "search-date-preset-7-button",
            "search-date-preset-30-button",
            "search-date-preset-90-button",
            "search-date-preset-365-button",
            "search-date-preset-custom-button",
            "search-date-from-input",
            "search-date-to-input",
            "search-document-type-section",
            "search-document-type-option-annual-financial-report",
            "search-document-type-checkbox-annual-financial-report",
            "search-submit-button",
        },
    )


def test_get_results_page_with_fake_job(client: TestClient) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(f"/searches/{job_id}")
    assert response.status_code == 200
    assert 'data-testid="results-status-region"' in response.text
    assert "Annual report" in response.text
    partial = client.get(f"/partials/searches/{job_id}/results")
    assert partial.status_code == 200
    assert 'rel="noopener noreferrer"' in partial.text
    partial_soup = BeautifulSoup(partial.text, "html.parser")
    open_link = partial_soup.select_one(
        '[data-testid="results-document-open-link"]'
    )
    copy_buttons = partial_soup.select(
        '[data-testid="results-document-copy-link-button"]'
    )
    assert open_link is not None
    assert str(open_link.get("href", "")) == "https://official.test/report.pdf"
    assert copy_buttons == []
    assert "fake-oam" not in response.text
    assert "doc-1" not in response.text
    assert "fake-oam" not in partial.text
    assert "doc-1" not in partial.text


def test_results_interactive_elements_have_test_ids(client: TestClient) -> None:
    job_id = "pagejob1234567890abcdef1234567890ab"
    responses = (
        client.get(f"/searches/{job_id}"),
        client.get(
            f"/partials/searches/{job_id}/results",
            params={"page": 1, "page_size": 50},
        ),
    )

    for response in responses:
        soup = BeautifulSoup(response.text, "html.parser")
        assert not _interactive_elements_without_test_id(soup)

    page_soup = BeautifulSoup(responses[0].text, "html.parser")
    assert page_soup.select_one('[name="issuer_isin"]') is None
    assert "Société (ISIN)" not in page_soup.get_text(" ", strip=True)
    _assert_test_ids(
        page_soup,
        {
            "layout-header",
            "layout-main",
            "layout-footer",
            "results-page",
            "results-toolbar",
            "results-new-search-link",
            "results-export-actions",
            "results-export-csv-link",
            "results-export-json-link",
            "results-status-section",
            "results-status-region",
            "results-job-status",
            "results-job-state",
            "results-job-indexed-count",
            "results-panel",
            "results-filter-form",
            "results-filter-document-type-select",
            "results-filter-market-input",
            "results-filter-query-input",
            "results-table-region",
            "results-table-scroll-container",
            "results-summary",
            "results-total-count",
            "results-current-page",
            "results-total-pages",
            "results-table",
            "results-sort-published-at",
            "results-sort-market",
            "results-sort-issuer-name",
            "results-sort-issuer-lei",
            "results-sort-document-type",
            "results-sort-title",
            "results-document-row",
            "results-document-published-at",
            "results-document-market",
            "results-document-issuer-name",
            "results-document-issuer-lei",
            "results-document-type",
            "results-document-title",
            "results-document-actions",
            "results-document-open-link",
            "results-pagination",
            "results-pagination-previous-disabled",
            "results-pagination-summary",
            "results-pagination-next-button",
        },
    )


def test_results_conditional_states_have_test_ids(client: TestClient) -> None:
    alert_response = client.get(
        "/searches/alertjob1234567890abcdef12345678901"
    )
    alert_soup = BeautifulSoup(alert_response.text, "html.parser")
    assert "fake-oam" not in alert_response.text
    assert "Market warning" not in alert_response.text
    assert "Market error" not in alert_response.text
    assert "Job warning" not in alert_response.text
    assert "Job error" not in alert_response.text
    alert_text = alert_soup.get_text(" ", strip=True)
    assert "Les résultats peuvent être incomplets" in alert_text
    assert "La recherche n'a pas abouti" in alert_text
    _assert_test_ids(
        alert_soup,
        {
            "results-job-warnings",
            "results-job-warning-message",
            "results-job-errors",
            "results-job-error-message",
            "results-market-runs",
            "results-market-run",
            "results-market-run-name",
            "results-market-run-status",
            "results-market-run-count",
            "results-market-run-warning",
            "results-market-run-error",
            "results-empty-state",
        },
    )

    running_response = client.get(
        "/searches/runningjob1234567890abcdef123456789"
    )
    running_soup = BeautifulSoup(running_response.text, "html.parser")
    _assert_test_ids(running_soup, {"results-job-progress"})


@pytest.mark.parametrize("output_format", ["csv", "json"])
def test_exports_hide_technical_provenance_but_keep_document_links(
    client: TestClient,
    output_format: str,
) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(
        f"/api/searches/{job_id}/export",
        params={"format": output_format},
    )

    assert response.status_code == 200
    assert "fake-oam" not in response.text
    assert "doc-1" not in response.text
    assert "source_document_id" not in response.text
    assert "https://official.test/report.pdf" in response.text


def test_dynamic_map_interactions_define_test_ids() -> None:
    app_script = (
        Path(__file__).parents[1] / "webapp" / "static" / "app.js"
    ).read_text(encoding="utf-8")

    assert "'search-map-canvas'" in app_script
    assert "`search-map-country-${code.toLowerCase()}`" in app_script
    assert "navigator.clipboard" not in app_script
    assert 'document.execCommand("copy")' not in app_script


def test_terminal_results_page_does_not_auto_poll_results(
    client: TestClient,
) -> None:
    job_id = "fakejob1234567890abcdef1234567890ab"
    response = client.get(f"/searches/{job_id}")

    soup = BeautifulSoup(response.text, "html.parser")
    status_region = soup.select_one("#job-status")
    status = soup.select_one("#job-status .job-status")
    filters = soup.select_one("#results-filters")
    results = soup.select_one("#results-table")

    assert status_region is not None
    assert not status_region.has_attr("hx-trigger")
    assert not status_region.has_attr("hx-get")
    assert status is not None
    assert status["data-terminal"] == "true"
    assert filters is not None
    assert filters["hx-trigger"] == (
        "change, input changed delay:200ms, submit"
    )
    assert filters["hx-sync"] == "this:replace"
    assert results is not None
    assert not results.has_attr("hx-trigger")
    assert not results.has_attr("hx-get")


def test_running_results_page_polls_with_current_filters(
    client: TestClient,
) -> None:
    job_id = "runningjob1234567890abcdef123456789"
    response = client.get(f"/searches/{job_id}")

    soup = BeautifulSoup(response.text, "html.parser")
    status_region = soup.select_one("#job-status")
    status = soup.select_one("#job-status .job-status")
    results = soup.select_one("#results-table")

    assert status_region is not None
    assert status_region["hx-trigger"] == (
        "load, every 2s [infofinShouldPollResults()]"
    )
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

    first_page_soup = soup
    _assert_test_ids(
        first_page_soup,
        {
            "results-pagination-previous-disabled",
            "results-pagination-next-button",
        },
    )

    last_page_response = client.get(
        f"/partials/searches/{job_id}/results",
        params={"page": 2, "page_size": 50},
    )
    last_page_soup = BeautifulSoup(last_page_response.text, "html.parser")
    _assert_test_ids(
        last_page_soup,
        {
            "results-pagination-previous-button",
            "results-pagination-next-disabled",
        },
    )


def _interactive_elements_without_test_id(
    soup: BeautifulSoup,
) -> list[object]:
    selectors = (
        "a",
        "button",
        'input:not([type="hidden"])',
        "select",
        "textarea",
        "summary",
        "[onclick]",
        '[role="button"]',
        '[contenteditable="true"]',
    )
    return [
        element
        for element in soup.select(", ".join(selectors))
        if not element.has_attr("data-testid")
    ]


def _assert_test_ids(soup: BeautifulSoup, expected: set[str]) -> None:
    actual = {
        str(element["data-testid"])
        for element in soup.select("[data-testid]")
    }
    assert expected <= actual, f"Missing data-testid values: {sorted(expected - actual)}"
