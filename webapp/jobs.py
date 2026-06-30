from __future__ import annotations

import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from webapp.repositories import WebSearchRepository
from webapp.services.document_search import (
    DocumentSearchService,
    LinkSearchRequest,
)


class JobManager:
    def __init__(
        self,
        *,
        repository: WebSearchRepository,
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
        self.repository.mark_job_running(job_id)
        result_set = self.search_service.search_links(request)
        for summary in result_set.market_summaries:
            self.repository.upsert_market_run(job_id, summary)
        self.repository.replace_results(job_id, result_set.documents)
        if result_set.errors and result_set.documents:
            status = "partial"
        elif result_set.errors:
            status = "failed"
        else:
            status = "done"
        self.repository.finish_job(
            job_id,
            status=status,
            results_count=len(result_set.documents),
            warnings=result_set.warnings,
            errors=result_set.errors,
        )

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