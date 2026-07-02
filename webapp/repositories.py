from __future__ import annotations

import json
import sqlite3
from datetime import date

from db import Database, utc_now
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    MarketSearchSummary,
)
from webapp.services.filters import document_matches_query

_SORT_WHITELIST = {
    "published_at": "published_at ASC",
    "-published_at": "published_at DESC",
    "title": "title COLLATE NOCASE ASC",
    "-title": "title COLLATE NOCASE DESC",
    "market": "market COLLATE NOCASE ASC",
    "-market": "market COLLATE NOCASE DESC",
    "document_type": "document_type COLLATE NOCASE ASC",
    "-document_type": "document_type COLLATE NOCASE DESC",
    "issuer_name": "issuer_name COLLATE NOCASE ASC",
    "-issuer_name": "issuer_name COLLATE NOCASE DESC",
    "issuer_lei": "issuer_lei COLLATE NOCASE ASC",
    "-issuer_lei": "issuer_lei COLLATE NOCASE DESC",
}


def _request_to_json(request: LinkSearchRequest) -> str:
    payload = {
        "markets": list(request.markets),
        "date_from": request.date_from.isoformat(),
        "date_to": request.date_to.isoformat(),
        "document_types": list(request.document_types),
        "query": request.query,
        "issuer_isin": request.issuer_isin,
        "sources": list(request.sources),
        "formats": list(request.formats),
        "date_confidences": list(request.date_confidences),
        "max_candidates": request.max_candidates,
        "dedupe_url": request.dedupe_url,
    }
    return json.dumps(payload, ensure_ascii=False)


def _request_from_json(payload: str) -> LinkSearchRequest:
    data = json.loads(payload)
    return LinkSearchRequest(
        markets=tuple(data["markets"]),
        date_from=date.fromisoformat(data["date_from"]),
        date_to=date.fromisoformat(data["date_to"]),
        document_types=tuple(data.get("document_types") or ()),
        query=data.get("query"),
        issuer_isin=data.get("issuer_isin"),
        sources=tuple(data.get("sources") or ()),
        formats=tuple(data.get("formats") or ()),
        date_confidences=tuple(data.get("date_confidences") or ()),
        max_candidates=int(data.get("max_candidates", 100000)),
        dedupe_url=bool(data.get("dedupe_url", False)),
    )


def _document_to_row(job_id: str, document: LinkSearchDocument) -> dict[str, object]:
    reporting_year = document.reporting_year
    if reporting_year == "":
        reporting_year_value = None
    elif isinstance(reporting_year, int):
        reporting_year_value = reporting_year
    else:
        try:
            reporting_year_value = int(str(reporting_year))
        except ValueError:
            reporting_year_value = None
    return {
        "job_id": job_id,
        "market": document.market,
        "source": document.source,
        "source_document_id": document.source_document_id or None,
        "published_at": document.published_at or None,
        "period_end_date": document.period_end_date or None,
        "reporting_year": reporting_year_value,
        "document_type": document.document_type,
        "classification": document.classification or None,
        "title": document.title,
        "url": document.url,
        "issuer_name": document.issuer_name or None,
        "issuer_isin": document.issuer_isin or None,
        "issuer_lei": document.issuer_lei or None,
        "category": document.category or None,
        "file_format": document.file_format or None,
        "date_confidence": document.date_confidence or None,
        "source_publication_date_raw": (
            document.source_publication_date_raw or None
        ),
        "metadata_json": json.dumps(
            {
                k: v for k, v in document.metadata.items()
                if not k.startswith("_")
            },
            default=str,
            ensure_ascii=False,
        ),
        "created_at": utc_now(),
    }


def _enrich_row_lei(connection, row: dict[str, object], database: Database) -> None:
    if row.get("issuer_lei"):
        return
    isin = row.get("issuer_isin")
    name = row.get("issuer_name")
    if not isin and not name:
        return

    # 1. Try local database lookup first (extremely fast)
    try:
        if isin:
            db_row = connection.execute(
                "SELECT lei FROM issuers WHERE isin = ?", (isin,)
            ).fetchone()
            if db_row and db_row["lei"]:
                row["issuer_lei"] = db_row["lei"]
                return
        if name:
            db_row = connection.execute(
                "SELECT lei FROM issuers WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
            if db_row and db_row["lei"]:
                row["issuer_lei"] = db_row["lei"]
                return
    except sqlite3.OperationalError:
        pass

    # 2. Check local memory/file cache (fast, no network)
    try:
        import lei_resolver
        cache = lei_resolver.load_lei_cache()
        isin_key = isin.upper().strip() if isin else None
        name_key = name.upper().strip() if name else None
        
        if isin_key and isin_key in cache:
            row["issuer_lei"] = cache[isin_key]
            return
        if name_key and name_key in cache:
            row["issuer_lei"] = cache[name_key]
            return
    except Exception:
        pass

    # 3. If still not found, queue background resolution (non-blocking)
    try:
        import lei_resolver
        lei_resolver.queue_background_resolution(database, isin, name)
    except Exception:
        pass


def _row_to_dict(row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


class WebSearchRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create_job(self, job_id: str, request: LinkSearchRequest) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO web_search_jobs(
                    id, created_at, status, request_json, markets_count
                )
                VALUES (?, ?, 'queued', ?, ?)
                """,
                (
                    job_id,
                    utc_now(),
                    _request_to_json(request),
                    len(request.markets),
                ),
            )

    def mark_job_running(self, job_id: str) -> None:
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE web_search_jobs
                SET status = 'running', started_at = ?
                WHERE id = ?
                """,
                (utc_now(), job_id),
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
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE web_search_jobs
                SET status = ?, finished_at = ?, results_count = ?,
                    warnings_json = ?, errors_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    utc_now(),
                    results_count,
                    json.dumps(list(warnings), ensure_ascii=False),
                    json.dumps(list(errors), ensure_ascii=False),
                    job_id,
                ),
            )

    def upsert_market_run(
        self,
        job_id: str,
        summary: MarketSearchSummary,
    ) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, started_at FROM web_search_market_runs WHERE job_id = ? AND market = ?",
                (job_id, summary.market),
            ).fetchone()
            
            finished_at = None if summary.status == "running" else utc_now()
            
            if row:
                started_at = row["started_at"] or utc_now()
                connection.execute(
                    """
                    UPDATE web_search_market_runs
                    SET source = ?, status = ?, candidates_returned = ?, results_count = ?,
                        warning = ?, error = ?, finished_at = ?, started_at = ?
                    WHERE id = ?
                    """,
                    (
                        summary.source or None,
                        summary.status,
                        summary.candidates_returned,
                        summary.documents_count,
                        summary.warning or None,
                        summary.error or None,
                        finished_at,
                        started_at,
                        row["id"],
                    ),
                )
            else:
                started_at = utc_now()
                connection.execute(
                    """
                    INSERT INTO web_search_market_runs(
                        job_id, market, source, status,
                        candidates_returned, results_count,
                        warning, error, started_at, finished_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        summary.market,
                        summary.source or None,
                        summary.status,
                        summary.candidates_returned,
                        summary.documents_count,
                        summary.warning or None,
                        summary.error or None,
                        started_at,
                        finished_at,
                    ),
                )

    def append_results(
        self,
        job_id: str,
        documents: tuple[LinkSearchDocument, ...],
        dedupe_url: bool = False,
    ) -> None:
        with self.database.connect() as connection:
            for document in documents:
                if dedupe_url and document.url:
                    existing = connection.execute(
                        """
                        SELECT id, market FROM web_search_results
                        WHERE job_id = ? AND url = ?
                        """,
                        (job_id, document.url),
                    ).fetchone()
                    if existing:
                        existing_markets = [m.strip() for m in existing["market"].split(",")]
                        new_market = document.market.strip()
                        if new_market not in existing_markets:
                            existing_markets.append(new_market)
                            updated_market = ", ".join(existing_markets)
                            connection.execute(
                                """
                                UPDATE web_search_results
                                SET market = ?
                                WHERE id = ?
                                """,
                                (updated_market, existing["id"]),
                            )
                        continue

                row = _document_to_row(job_id, document)
                _enrich_row_lei(connection, row, self.database)
                connection.execute(
                    """
                    INSERT INTO web_search_results(
                        job_id, market, source, source_document_id,
                        published_at, period_end_date, reporting_year,
                        document_type, classification, title, url,
                        issuer_name, issuer_isin, issuer_lei, category,
                        file_format, date_confidence,
                        source_publication_date_raw, metadata_json,
                        created_at
                    )
                    VALUES (
                        :job_id, :market, :source, :source_document_id,
                        :published_at, :period_end_date, :reporting_year,
                        :document_type, :classification, :title, :url,
                        :issuer_name, :issuer_isin, :issuer_lei, :category,
                        :file_format, :date_confidence,
                        :source_publication_date_raw, :metadata_json,
                        :created_at
                    )
                    """,
                    row,
                )

    def replace_results(
        self,
        job_id: str,
        documents: tuple[LinkSearchDocument, ...],
    ) -> None:
        with self.database.connect() as connection:
            connection.execute(
                "DELETE FROM web_search_results WHERE job_id = ?",
                (job_id,),
            )
            for document in documents:
                row = _document_to_row(job_id, document)
                _enrich_row_lei(connection, row, self.database)
                connection.execute(
                    """
                    INSERT INTO web_search_results(
                        job_id, market, source, source_document_id,
                        published_at, period_end_date, reporting_year,
                        document_type, classification, title, url,
                        issuer_name, issuer_isin, issuer_lei, category,
                        file_format, date_confidence,
                        source_publication_date_raw, metadata_json,
                        created_at
                    )
                    VALUES (
                        :job_id, :market, :source, :source_document_id,
                        :published_at, :period_end_date, :reporting_year,
                        :document_type, :classification, :title, :url,
                        :issuer_name, :issuer_isin, :issuer_lei, :category,
                        :file_format, :date_confidence,
                        :source_publication_date_raw, :metadata_json,
                        :created_at
                    )
                    """,
                    row,
                )

    def count_results(self, job_id: str) -> int:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) as cnt FROM web_search_results WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return row["cnt"] if row else 0

    def get_job(self, job_id: str) -> dict[str, object] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM web_search_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        result = _row_to_dict(row)
        result["warnings"] = json.loads(result["warnings_json"])
        result["errors"] = json.loads(result["errors_json"])
        result["request"] = _request_from_json(result["request_json"])
        return result

    def list_market_runs(self, job_id: str) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM web_search_market_runs
                WHERE job_id = ?
                ORDER BY id ASC
                """,
                (job_id,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

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
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        offset = (page - 1) * page_size
        order_clause = _SORT_WHITELIST.get(sort, "published_at DESC")

        conditions = ["job_id = ?"]
        params: list[object] = [job_id]
        if document_type:
            conditions.append("document_type = ?")
            params.append(document_type)
        if market:
            conditions.append("market = ?")
            params.append(market)
        if source:
            conditions.append("source = ?")
            params.append(source)
        if issuer_isin:
            conditions.append(
                "LOWER(issuer_isin) LIKE ?"
            )
            params.append(f"%{issuer_isin.strip().casefold()}%")

        where_clause = " AND ".join(conditions)
        with self.database.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM web_search_results
                WHERE {where_clause}
                ORDER BY {order_clause}
                """,
                params,
            ).fetchall()

        results = [_row_to_dict(row) for row in rows]
        if q:
            results = [
                row
                for row in results
                if document_matches_query(
                    LinkSearchDocument(
                        market=str(row.get("market") or ""),
                        source=str(row.get("source") or ""),
                        source_document_id=str(
                            row.get("source_document_id") or ""
                        ),
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
                        source_publication_date_raw=str(
                            row.get("source_publication_date_raw") or ""
                        ),
                    ),
                    q,
                )
            ]

        total = len(results)
        return results[offset : offset + page_size], total

    def purge_jobs_older_than(self, cutoff_iso: str) -> int:
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM web_search_jobs
                WHERE created_at < ?
                """,
                (cutoff_iso,),
            )
            return cursor.rowcount