from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import date
from pathlib import Path
from uuid import uuid4

import uvicorn

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import Settings  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.firestore_repository import (  # noqa: E402
    FirestoreWebSearchRepository,
    InMemoryDocumentStore,
)
from webapp.services.document_search import (  # noqa: E402
    LinkSearchDocument,
    LinkSearchRequest,
    LinkSearchResultSet,
    MarketSearchSummary,
)


class DeterministicJobManager:
    """Synchronous, network-free job manager used only by Playwright."""

    def __init__(self, repository: FirestoreWebSearchRepository) -> None:
        self.repository = repository

    def submit(self, request: LinkSearchRequest) -> str:
        job_id = f"e2e-{uuid4().hex}"
        self.repository.create_job(job_id, request)
        if request.query == "queued-fixture":
            return job_id
        documents = self._documents_for(request)
        self.repository.mark_job_running(job_id)
        for market in request.markets:
            count = sum(document.market == market for document in documents)
            self.repository.upsert_market_run(
                job_id,
                MarketSearchSummary(
                    market=market,
                    source="e2e-fixture",
                    status="ok",
                    candidates_returned=count,
                    documents_count=count,
                ),
            )
        self.repository.replace_results(job_id, documents)
        self.repository.finish_job(
            job_id,
            status="done",
            results_count=len(documents),
            warnings=(),
            errors=(),
        )
        return job_id

    def get_status(self, job_id: str) -> dict[str, object] | None:
        job = self.repository.get_job(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id,
            "status": job["status"],
            "results_count": job["results_count"],
            "warnings": job["warnings"],
            "errors": job["errors"],
            "markets": self.repository.list_market_runs(job_id),
        }

    def cancel(self, job_id: str) -> bool:
        return False

    def shutdown(self) -> None:
        return None

    @staticmethod
    def _documents_for(
        request: LinkSearchRequest,
    ) -> tuple[LinkSearchDocument, ...]:
        markets = request.markets or ("Euronext Paris",)
        requested_types = request.document_types
        documents: list[LinkSearchDocument] = []
        for index in range(51):
            is_unique_half_year = index == 1 and not requested_types
            document_type = (
                requested_types[index % len(requested_types)]
                if requested_types
                else (
                    "half_year_financial_report"
                    if is_unique_half_year
                    else "annual_financial_report"
                )
            )
            market = markets[1] if index == 1 and len(markets) > 1 else markets[0]
            issuer_name = "Beta ASA" if is_unique_half_year else f"Alpha {index:02d} SA"
            title = (
                "Rapport semestriel Beta unique"
                if is_unique_half_year
                else f"Rapport annuel Alpha {index:02d}"
            )
            documents.append(
                LinkSearchDocument(
                    market=market,
                    source="e2e-fixture",
                    source_document_id=f"e2e-document-{index:02d}",
                    published_at=f"2026-06-{30 - (index % 30):02d}",
                    period_end_date="2025-12-31",
                    reporting_year=2025,
                    document_type=document_type,
                    classification="regulated_information",
                    title=title,
                    url=f"https://documents.example.test/report-{index:02d}.pdf",
                    issuer_name=issuer_name,
                    issuer_isin=f"FR{index:010d}",
                    issuer_lei=f"969500E2E{index:010d}",
                    category="financial-report",
                    file_format="pdf",
                    date_confidence="high",
                    source_publication_date_raw="",
                )
            )
        return tuple(documents)


class DeterministicSearchService:
    """Network-free worker used by the internal Cloud Tasks endpoint."""

    def search_links(self, request: LinkSearchRequest) -> LinkSearchResultSet:
        documents = DeterministicJobManager._documents_for(request)
        summaries = tuple(
            MarketSearchSummary(
                market=market,
                source="e2e-fixture",
                status="ok",
                candidates_returned=sum(
                    document.market == market for document in documents
                ),
                documents_count=sum(
                    document.market == market for document in documents
                ),
            )
            for market in request.markets
        )
        return LinkSearchResultSet(
            request=request,
            documents=documents,
            market_summaries=summaries,
        )


def build_app():
    temporary_directory = tempfile.TemporaryDirectory(prefix="infofin-e2e-")
    database_path = Path(temporary_directory.name) / "infofin-e2e.sqlite3"
    settings = Settings(
        db_path=database_path,
        data_dir=Path(temporary_directory.name) / "raw",
        http_timeout_seconds=1,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="infofin-playwright",
        max_download_bytes=1024,
        amf_base_url="https://unused.example.test",
        amf_fallback_base_urls=(),
        amf_dataset="unused",
        amf_rows=1,
        web_workers=1,
        web_storage_backend="firestore",
        web_job_backend="cloud-tasks",
        google_cloud_project="infofin-e2e",
        cloud_tasks_queue="infofin-search-queue",
        web_service_url="http://127.0.0.1:8766",
        web_access_username="e2e-user",
        web_access_password="e2e-password",
    )
    repository = FirestoreWebSearchRepository(
        store=InMemoryDocumentStore(),
        prefix="infofin_e2e",
    )
    app = create_app(
        settings=settings,
        repository=repository,
        job_manager=DeterministicJobManager(repository),  # type: ignore[arg-type]
        search_service=DeterministicSearchService(),  # type: ignore[arg-type]
    )
    app.state.e2e_temporary_directory = temporary_directory
    return app


app = build_app()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    arguments = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=arguments.port, log_level="warning")
