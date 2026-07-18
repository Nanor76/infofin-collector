from __future__ import annotations

import copy
import hashlib
import json
from typing import Protocol
from uuid import uuid4

from db import utc_now
from webapp.repositories import (
    _document_to_row,
    _request_from_json,
    _request_to_json,
)
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    MarketSearchSummary,
)
from webapp.services.filters import document_matches_query


DocumentPath = tuple[str, ...]


class DocumentStore(Protocol):
    def get(self, path: DocumentPath) -> dict[str, object] | None: ...

    def set(self, path: DocumentPath, data: dict[str, object]) -> None: ...

    def update(self, path: DocumentPath, data: dict[str, object]) -> None: ...

    def delete(self, path: DocumentPath) -> None: ...

    def list(
        self, collection_path: DocumentPath
    ) -> list[tuple[str, dict[str, object]]]: ...


class InMemoryDocumentStore:
    """Minimal deterministic document store used by the offline test suite."""

    def __init__(self) -> None:
        self._documents: dict[DocumentPath, dict[str, object]] = {}

    def get(self, path: DocumentPath) -> dict[str, object] | None:
        value = self._documents.get(path)
        return copy.deepcopy(value) if value is not None else None

    def set(self, path: DocumentPath, data: dict[str, object]) -> None:
        self._documents[path] = copy.deepcopy(data)

    def update(self, path: DocumentPath, data: dict[str, object]) -> None:
        if path not in self._documents:
            raise KeyError(path)
        self._documents[path].update(copy.deepcopy(data))

    def delete(self, path: DocumentPath) -> None:
        self._documents.pop(path, None)

    def list(
        self,
        collection_path: DocumentPath,
    ) -> list[tuple[str, dict[str, object]]]:
        expected_length = len(collection_path) + 1
        return [
            (path[-1], copy.deepcopy(value))
            for path, value in sorted(self._documents.items())
            if len(path) == expected_length and path[:-1] == collection_path
        ]

    def paths(self) -> tuple[DocumentPath, ...]:
        return tuple(sorted(self._documents))


class GoogleFirestoreDocumentStore:
    def __init__(self, *, project: str, database: str = "(default)") -> None:
        try:
            from google.cloud import firestore
        except ImportError as exc:  # pragma: no cover - deployment dependency
            raise RuntimeError(
                "google-cloud-firestore est requis pour le backend Firestore"
            ) from exc
        self.client = firestore.Client(project=project, database=database)

    def _document(self, path: DocumentPath):
        if not path or len(path) % 2:
            raise ValueError(
                "Un chemin de document doit contenir des paires collection/id"
            )
        reference = self.client.collection(path[0]).document(path[1])
        for index in range(2, len(path), 2):
            reference = reference.collection(path[index]).document(path[index + 1])
        return reference

    def _collection(self, path: DocumentPath):
        if not path or len(path) % 2 == 0:
            raise ValueError(
                "Un chemin de collection doit se terminer par une collection"
            )
        reference = self.client.collection(path[0])
        for index in range(1, len(path), 2):
            reference = reference.document(path[index]).collection(path[index + 1])
        return reference

    def get(self, path: DocumentPath) -> dict[str, object] | None:
        snapshot = self._document(path).get()
        if not snapshot.exists:
            return None
        return dict(snapshot.to_dict() or {})

    def set(self, path: DocumentPath, data: dict[str, object]) -> None:
        self._document(path).set(data)

    def update(self, path: DocumentPath, data: dict[str, object]) -> None:
        self._document(path).update(data)

    def delete(self, path: DocumentPath) -> None:
        self._document(path).delete()

    def list(
        self,
        collection_path: DocumentPath,
    ) -> list[tuple[str, dict[str, object]]]:
        return [
            (snapshot.id, dict(snapshot.to_dict() or {}))
            for snapshot in self._collection(collection_path).stream()
        ]


def _document_from_row(row: dict[str, object]) -> LinkSearchDocument:
    metadata = row.get("metadata_json") or "{}"
    return LinkSearchDocument(
        market=str(row.get("market") or ""),
        source=str(row.get("source") or ""),
        source_document_id=str(row.get("source_document_id") or ""),
        published_at=str(row.get("published_at") or ""),
        period_end_date=str(row.get("period_end_date") or ""),
        reporting_year=row.get("reporting_year") or "",
        document_type=str(row.get("document_type") or ""),
        classification=str(row.get("classification") or ""),
        title=str(row.get("title") or ""),
        url=str(row.get("url") or ""),
        issuer_name=str(row.get("issuer_name") or ""),
        issuer_isin=str(row.get("issuer_isin") or ""),
        issuer_lei=str(row.get("issuer_lei") or ""),
        category=str(row.get("category") or ""),
        file_format=str(row.get("file_format") or ""),
        date_confidence=str(row.get("date_confidence") or ""),
        source_publication_date_raw=str(row.get("source_publication_date_raw") or ""),
        metadata=(
            json.loads(str(metadata)) if isinstance(metadata, str) else dict(metadata)
        ),
    )


class FirestoreWebSearchRepository:
    """Firestore implementation matching the local SQLite repository contract."""

    def __init__(self, *, store: DocumentStore, prefix: str = "infofin_web") -> None:
        cleaned_prefix = prefix.strip().strip("_")
        if not cleaned_prefix:
            raise ValueError("Le préfixe Firestore ne peut pas être vide")
        self.store = store
        self.jobs_collection = f"{cleaned_prefix}_jobs"
        self.feedback_collection = f"{cleaned_prefix}_feedback"

    def _job_path(self, job_id: str) -> DocumentPath:
        return (self.jobs_collection, job_id)

    def _child_collection(self, job_id: str, name: str) -> DocumentPath:
        return (*self._job_path(job_id), name)

    def create_job(
        self,
        job_id: str,
        request: LinkSearchRequest,
        owner_id: str | None = None,
    ) -> None:
        self.store.set(
            self._job_path(job_id),
            {
                "id": job_id,
                "owner_id": owner_id,
                "created_at": utc_now(),
                "started_at": None,
                "finished_at": None,
                "status": "queued",
                "request_json": _request_to_json(request),
                "markets_count": len(request.markets),
                "results_count": 0,
                "warnings_json": "[]",
                "errors_json": "[]",
            },
        )

    def count_jobs_for_owner_since(self, owner_id: str, cutoff_iso: str) -> int:
        return sum(
            1
            for _, job in self.store.list((self.jobs_collection,))
            if job.get("owner_id") == owner_id
            and str(job.get("created_at") or "") >= cutoff_iso
        )

    def add_feedback(
        self,
        *,
        feedback_id: str,
        owner_id: str,
        category: str,
        message: str,
        job_id: str | None,
        created_at: str,
    ) -> None:
        self.store.set(
            (self.feedback_collection, feedback_id),
            {
                "id": feedback_id,
                "owner_id": owner_id,
                "job_id": job_id,
                "category": category,
                "message": message,
                "created_at": created_at,
            },
        )

    def mark_job_running(self, job_id: str) -> None:
        self.store.update(
            self._job_path(job_id),
            {"status": "running", "started_at": utc_now()},
        )

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        results_count: int,
        warnings: tuple[str, ...],
        errors: tuple[str, ...],
    ) -> None:
        self.store.update(
            self._job_path(job_id),
            {
                "status": status,
                "finished_at": utc_now(),
                "results_count": results_count,
                "warnings_json": json.dumps(list(warnings), ensure_ascii=False),
                "errors_json": json.dumps(list(errors), ensure_ascii=False),
            },
        )

    def upsert_market_run(
        self,
        job_id: str,
        summary: MarketSearchSummary,
    ) -> None:
        document_id = hashlib.sha256(summary.market.encode("utf-8")).hexdigest()[:20]
        path = (*self._child_collection(job_id, "market_runs"), document_id)
        existing = self.store.get(path) or {}
        started_at = existing.get("started_at") or utc_now()
        self.store.set(
            path,
            {
                "job_id": job_id,
                "market": summary.market,
                "source": summary.source or None,
                "status": summary.status,
                "candidates_returned": summary.candidates_returned,
                "results_count": summary.documents_count,
                "warning": summary.warning or None,
                "error": summary.error or None,
                "started_at": started_at,
                "finished_at": (None if summary.status == "running" else utc_now()),
            },
        )

    def _result_rows(self, job_id: str) -> list[tuple[str, dict[str, object]]]:
        return self.store.list(self._child_collection(job_id, "results"))

    def append_results(
        self,
        job_id: str,
        documents: tuple[LinkSearchDocument, ...],
        dedupe_url: bool = False,
    ) -> None:
        existing = self._result_rows(job_id)
        urls = {
            str(row.get("url") or ""): (document_id, row)
            for document_id, row in existing
            if row.get("url")
        }
        for document in documents:
            if dedupe_url and document.url in urls:
                document_id, row = urls[document.url]
                markets = [value.strip() for value in str(row["market"]).split(",")]
                if document.market not in markets:
                    markets.append(document.market)
                    self.store.update(
                        (*self._child_collection(job_id, "results"), document_id),
                        {"market": ", ".join(markets)},
                    )
                continue
            row = _document_to_row(job_id, document)
            document_id = uuid4().hex
            self.store.set(
                (*self._child_collection(job_id, "results"), document_id),
                row,
            )
            if document.url:
                urls[document.url] = (document_id, row)
        self.store.update(
            self._job_path(job_id),
            {"results_count": len(self._result_rows(job_id))},
        )

    def replace_results(
        self,
        job_id: str,
        documents: tuple[LinkSearchDocument, ...],
    ) -> None:
        collection = self._child_collection(job_id, "results")
        for document_id, _ in self.store.list(collection):
            self.store.delete((*collection, document_id))
        for index, document in enumerate(documents):
            self.store.set(
                (*collection, f"{index:08d}"),
                _document_to_row(job_id, document),
            )
        self.store.update(
            self._job_path(job_id),
            {"results_count": len(documents)},
        )

    def resolve_issuer_market(
        self,
        isin: str | None,
        name: str | None,
    ) -> str | None:
        return None

    def count_results(self, job_id: str) -> int:
        job = self.store.get(self._job_path(job_id))
        return int(job.get("results_count") or 0) if job else 0

    def get_job(self, job_id: str) -> dict[str, object] | None:
        result = self.store.get(self._job_path(job_id))
        if result is None:
            return None
        result["warnings"] = json.loads(str(result.get("warnings_json") or "[]"))
        result["errors"] = json.loads(str(result.get("errors_json") or "[]"))
        result["request"] = _request_from_json(str(result["request_json"]))
        return result

    def list_market_runs(self, job_id: str) -> list[dict[str, object]]:
        rows = [
            row
            for _, row in self.store.list(self._child_collection(job_id, "market_runs"))
        ]
        return sorted(rows, key=lambda row: str(row.get("market") or "").casefold())

    def list_results(
        self,
        job_id: str,
        *,
        document_type: str | None = None,
        market: str | None = None,
        source: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[dict[str, object]], int]:
        rows = [row for _, row in self._result_rows(job_id)]
        if document_type:
            rows = [row for row in rows if row.get("document_type") == document_type]
        if market:
            rows = [row for row in rows if row.get("market") == market]
        if source:
            rows = [row for row in rows if row.get("source") == source]
        if issuer_isin:
            needle = issuer_isin.strip().casefold()
            rows = [
                row
                for row in rows
                if needle in str(row.get("issuer_isin") or "").casefold()
            ]
        if q:
            rows = [
                row
                for row in rows
                if document_matches_query(_document_from_row(row), q)
            ]

        descending = sort.startswith("-")
        sort_field = sort.removeprefix("-")
        if sort_field not in {
            "published_at",
            "title",
            "market",
            "document_type",
            "issuer_name",
            "issuer_lei",
        }:
            sort_field = "published_at"
            descending = True
        rows.sort(
            key=lambda row: str(row.get(sort_field) or "").casefold(),
            reverse=descending,
        )
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        return rows[offset : offset + page_size], len(rows)

    def purge_jobs_older_than(self, cutoff_iso: str) -> int:
        deleted = 0
        for job_id, job in self.store.list((self.jobs_collection,)):
            if str(job.get("created_at") or "") >= cutoff_iso:
                continue
            for child_name in ("market_runs", "results"):
                collection = self._child_collection(job_id, child_name)
                for document_id, _ in self.store.list(collection):
                    self.store.delete((*collection, document_id))
            self.store.delete(self._job_path(job_id))
            deleted += 1
        return deleted

    def purge_feedback_older_than(self, cutoff_iso: str) -> int:
        deleted = 0
        for feedback_id, feedback in self.store.list(
            (self.feedback_collection,)
        ):
            if str(feedback.get("created_at") or "") >= cutoff_iso:
                continue
            self.store.delete((self.feedback_collection, feedback_id))
            deleted += 1
        return deleted
