from __future__ import annotations

from dataclasses import replace
import logging
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Protocol

from load_watchlist import normalize_market
from webapp.services.document_search import (
    DocumentSearchService,
    LinkSearchRequest,
    LinkSearchDocument,
    MarketSearchSummary,
)


LOGGER = logging.getLogger(__name__)


class JobLauncher(Protocol):
    def launch(self, job_id: str) -> None: ...


def _status_from_repository(repository, job_id: str) -> dict[str, object] | None:
    job = repository.get_job(job_id)
    if job is None:
        return None
    markets = repository.list_market_runs(job_id)
    results_count = job["results_count"]
    if job["status"] in {"running", "queued"}:
        results_count = repository.count_results(job_id)
    return {
        "job_id": job_id,
        "status": job["status"],
        "results_count": results_count,
        "warnings": job["warnings"],
        "errors": job["errors"],
        "markets": markets,
    }


def run_stored_search(
    *,
    repository,
    search_service: DocumentSearchService,
    job_id: str,
    request: LinkSearchRequest,
) -> None:
    """Execute one persisted search, usable by local threads and Cloud Run Jobs."""
    try:
        repository.mark_job_running(job_id)

        for market in request.markets:
            summary = MarketSearchSummary(
                market=normalize_market(market),
                source="",
                status="running",
            )
            repository.upsert_market_run(job_id, summary)

        def on_market_complete(
            summary: MarketSearchSummary,
            documents: tuple[LinkSearchDocument, ...],
        ) -> None:
            repository.upsert_market_run(job_id, summary)
            repository.append_results(
                job_id,
                documents,
                dedupe_url=request.dedupe_url,
            )

        import inspect

        signature = inspect.signature(search_service.search_links)
        if "on_market_complete" in signature.parameters:
            result_set = search_service.search_links(
                request,
                on_market_complete=on_market_complete,
            )
        else:
            result_set = search_service.search_links(request)

        final_counts: dict[str, int] = {}
        enriched_documents: list[LinkSearchDocument] = []
        for document in result_set.documents:
            database_market = repository.resolve_issuer_market(
                document.issuer_isin,
                document.issuer_name,
            )
            if database_market:
                document = replace(document, market=database_market)
            enriched_documents.append(document)
            for market in document.market.split(","):
                cleaned_market = market.strip()
                final_counts[cleaned_market] = final_counts.get(cleaned_market, 0) + 1

        for summary in result_set.market_summaries:
            repository.upsert_market_run(
                job_id,
                replace(
                    summary,
                    documents_count=final_counts.get(summary.market, 0),
                ),
            )

        repository.replace_results(job_id, tuple(enriched_documents))
        if result_set.errors and enriched_documents:
            status = "partial"
        elif result_set.errors:
            status = "failed"
        else:
            status = "done"
        repository.finish_job(
            job_id,
            status=status,
            results_count=len(enriched_documents),
            warnings=result_set.warnings,
            errors=result_set.errors,
        )
    except Exception:
        LOGGER.exception("La recherche %s a échoué", job_id)
        repository.finish_job(
            job_id,
            status="failed",
            results_count=repository.count_results(job_id),
            warnings=(),
            errors=("La recherche a échoué pendant son exécution.",),
        )
        raise


class JobManager:
    def __init__(
        self,
        *,
        repository,
        search_service: DocumentSearchService,
        max_workers: int = 2,
    ) -> None:
        self.repository = repository
        self.search_service = search_service
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures: dict[str, Future[None]] = {}

    def submit(self, request: LinkSearchRequest) -> str:
        job_id = uuid.uuid4().hex
        self.repository.create_job(job_id, request)
        future = self._executor.submit(self._run_job, job_id, request)
        self._futures[job_id] = future
        return job_id

    def _run_job(self, job_id: str, request: LinkSearchRequest) -> None:
        run_stored_search(
            repository=self.repository,
            search_service=self.search_service,
            job_id=job_id,
            request=request,
        )

    def get_status(self, job_id: str) -> dict[str, object] | None:
        return _status_from_repository(self.repository, job_id)

    def cancel(self, job_id: str) -> bool:
        future = self._futures.get(job_id)
        if future is None:
            job = self.repository.get_job(job_id)
            if job is None:
                return False
            return job["status"] == "cancelled"
        if future.cancel():
            self.repository.finish_job(
                job_id,
                status="cancelled",
                results_count=0,
                warnings=(),
                errors=(),
            )
            return True
        return False

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class CloudJobManager:
    """Persist a search then dispatch it to a Cloud Run Job execution."""

    def __init__(self, *, repository, launcher: JobLauncher) -> None:
        self.repository = repository
        self.launcher = launcher

    def submit(self, request: LinkSearchRequest) -> str:
        job_id = uuid.uuid4().hex
        self.repository.create_job(job_id, request)
        try:
            self.launcher.launch(job_id)
        except Exception:
            LOGGER.exception("Impossible de lancer le Cloud Run Job pour %s", job_id)
            self.repository.finish_job(
                job_id,
                status="failed",
                results_count=0,
                warnings=(),
                errors=("Impossible de démarrer la recherche distante.",),
            )
        return job_id

    def get_status(self, job_id: str) -> dict[str, object] | None:
        return _status_from_repository(self.repository, job_id)

    def cancel(self, job_id: str) -> bool:
        return False

    def shutdown(self) -> None:
        return None
