from __future__ import annotations

import re
import tempfile
import unicodedata
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import Settings
from connectors import SUPPORTED_WATCH_MARKETS
from db import Database
from webapp.jobs import JobManager
from webapp.repositories import WebSearchRepository
from webapp.schemas import (
    SearchCreateRequest,
    SearchCreateResponse,
    SearchResultsResponse,
    SearchStatusResponse,
)
from webapp.services.document_search import DocumentSearchService, LinkSearchRequest
from webapp.services.exports import write_web_results_export

DOCUMENT_TYPES = (
    ("annual_financial_report", "Rapport annuel"),
    ("half_year_financial_report", "Rapport semestriel"),
    ("quarterly_financial_report", "Rapport trimestriel"),
    ("universal_registration_document", "Document d'enregistrement universel"),
    ("financial_report", "Rapport financier"),
    ("esef", "Package ESEF / XHTML / ZIP"),
)

_WEBAPP_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_WEBAPP_DIR / "templates"))


def _test_id_segment(value: str) -> str:
    transliterated = value.casefold().translate(
        str.maketrans({"ø": "o", "æ": "ae", "œ": "oe", "ł": "l"})
    )
    ascii_value = unicodedata.normalize("NFKD", transliterated).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")


def _request_from_schema(payload: SearchCreateRequest) -> LinkSearchRequest:
    return LinkSearchRequest(
        markets=tuple(payload.markets),
        date_from=payload.date_from,
        date_to=payload.date_to,
        document_types=tuple(payload.document_types),
        query=payload.query,
        issuer_isin=payload.issuer_isin,
        sources=tuple(payload.sources),
        formats=tuple(payload.formats),
        date_confidences=tuple(payload.date_confidences),
        max_candidates=payload.max_candidates,
        dedupe_url=True,
    )


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    job_manager: JobManager | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    resolved_database = database or Database(resolved_settings.db_path)
    resolved_database.initialize_web_search_schema()
    repository = WebSearchRepository(resolved_database)
    search_service = DocumentSearchService(resolved_settings)
    resolved_job_manager = job_manager or JobManager(
        repository=repository,
        search_service=search_service,
        max_workers=resolved_settings.web_workers,
    )

    app = FastAPI(title="InfoFin Document Search")
    app.mount(
        "/static",
        StaticFiles(directory=str(_WEBAPP_DIR / "static")),
        name="static",
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.repository = repository
    app.state.job_manager = resolved_job_manager

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/markets")
    def list_markets() -> dict[str, list[str]]:
        return {"markets": list(SUPPORTED_WATCH_MARKETS)}

    @app.get("/api/document-types")
    def list_document_types() -> dict[str, list[dict[str, str]]]:
        return {
            "document_types": [
                {"value": value, "label": label}
                for value, label in DOCUMENT_TYPES
            ]
        }

    @app.post("/api/searches", response_model=SearchCreateResponse)
    def create_search(payload: SearchCreateRequest) -> SearchCreateResponse:
        if payload.date_from > payload.date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from doit être inférieur ou égal à date_to",
            )
        job_id = resolved_job_manager.submit(_request_from_schema(payload))
        return SearchCreateResponse(
            job_id=job_id,
            status_url=f"/api/searches/{job_id}",
            results_url=f"/api/searches/{job_id}/results",
        )

    @app.get("/api/searches/{job_id}", response_model=SearchStatusResponse)
    def get_search_status(job_id: str) -> SearchStatusResponse:
        status = resolved_job_manager.get_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        return SearchStatusResponse(**status)

    @app.get("/api/searches/{job_id}/results", response_model=SearchResultsResponse)
    def get_search_results(
        job_id: str,
        document_type: str | None = None,
        market: str | None = None,
        source: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> SearchResultsResponse:
        if resolved_job_manager.get_status(job_id) is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        results, total = repository.list_results(
            job_id,
            document_type=document_type,
            market=market,
            source=source,
            q=q,
            issuer_isin=issuer_isin,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        return SearchResultsResponse(
            job_id=job_id,
            total=total,
            page=page,
            page_size=page_size,
            results=results,
        )

    @app.get("/api/searches/{job_id}/export")
    def export_search_results(
        job_id: str,
        format: str = Query(default="csv", pattern="^(csv|json)$"),
        document_type: str | None = None,
        market: str | None = None,
        source: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
    ) -> FileResponse:
        if resolved_job_manager.get_status(job_id) is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        results: list[dict[str, object]] = []
        page = 1
        while True:
            batch, total = repository.list_results(
                job_id,
                document_type=document_type,
                market=market,
                source=source,
                q=q,
                issuer_isin=issuer_isin,
                page=page,
                page_size=200,
            )
            results.extend(batch)
            if len(results) >= total or not batch:
                break
            page += 1
        rows = [
            {
                "market": row.get("market", ""),
                "source": row.get("source", ""),
                "source_document_id": row.get("source_document_id", ""),
                "published_at": row.get("published_at", ""),
                "period_end_date": row.get("period_end_date", ""),
                "reporting_year": row.get("reporting_year", ""),
                "document_type": row.get("document_type", ""),
                "classification": row.get("classification", ""),
                "title": row.get("title", ""),
                "url": row.get("url", ""),
                "issuer_name": row.get("issuer_name", ""),
                "issuer_isin": row.get("issuer_isin", ""),
                "issuer_lei": row.get("issuer_lei", ""),
                "category": row.get("category", ""),
                "date_confidence": row.get("date_confidence", ""),
                "source_publication_date_raw": row.get(
                    "source_publication_date_raw", ""
                ),
                "file_format": row.get("file_format", ""),
                "job_id": job_id,
                "created_at": row.get("created_at", ""),
            }
            for row in results
        ]
        suffix = ".csv" if format == "csv" else ".json"
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            delete=False,
        ) as temp_file:
            target = Path(temp_file.name)
        write_web_results_export(
            rows=rows,
            output_format=format,
            target=target,
        )
        media_type = "text/csv" if format == "csv" else "application/json"
        return FileResponse(
            target,
            media_type=media_type,
            filename=f"search_{job_id}{suffix}",
        )

    @app.post("/api/searches/{job_id}/cancel")
    def cancel_search(job_id: str) -> dict[str, bool]:
        return {"cancelled": resolved_job_manager.cancel(job_id)}

    @app.get("/", response_class=HTMLResponse)
    def search_page(request: Request) -> HTMLResponse:
        market_metadata = {
            "Euronext Paris": {"city": "Paris", "country": "France", "code": "FR"},
            "Oslo Børs": {"city": "Oslo", "country": "Norvège", "code": "NO"},
            "Euronext Milan": {"city": "Milan", "country": "Italie", "code": "IT"},
            "Euronext Star Milan": {"city": "Milan", "country": "Italie", "code": "IT"},
            "Euronext Growth Milan": {"city": "Milan", "country": "Italie", "code": "IT"},
            "Euronext MIV Milan": {"city": "Milan", "country": "Italie", "code": "IT"},
            "Euronext Amsterdam": {"city": "Amsterdam", "country": "Pays-Bas", "code": "NL"},
            "Euronext Brussels": {"city": "Bruxelles", "country": "Belgique", "code": "BE"},
            "Euronext Growth Brussels": {"city": "Bruxelles", "country": "Belgique", "code": "BE"},
            "Euronext Lisbon": {"city": "Lisbonne", "country": "Portugal", "code": "PT"},
            "Euronext Dublin": {"city": "Dublin", "country": "Irlande", "code": "IE"},
            "Bolsa de Madrid": {"city": "Madrid", "country": "Espagne", "code": "ES"},
            "Bolsa de Barcelona": {"city": "Barcelone", "country": "Espagne", "code": "ES"},
            "Bolsa de Bilbao": {"city": "Bilbao", "country": "Espagne", "code": "ES"},
            "Bolsa de Valencia": {"city": "Valence", "country": "Espagne", "code": "ES"},
            "BME Growth": {"city": "Madrid", "country": "Espagne", "code": "ES"},
            "BME Scaleup": {"city": "Madrid", "country": "Espagne", "code": "ES"},
            "Nasdaq Stockholm": {"city": "Stockholm", "country": "Suède", "code": "SE"},
            "Nordic Growth Market": {"city": "Stockholm", "country": "Suède", "code": "SE"},
            "Nasdaq Copenhagen": {"city": "Copenhague", "country": "Danemark", "code": "DK"},
            "Nasdaq Helsinki": {"city": "Helsinki", "country": "Finlande", "code": "FI"},
            "Vienna Stock Exchange": {"city": "Vienne", "country": "Autriche", "code": "AT"},
            "Warsaw Stock Exchange": {"city": "Varsovie", "country": "Pologne", "code": "PL"},
            "Prague Stock Exchange": {"city": "Prague", "country": "République Tchèque", "code": "CZ"},
            "Zagreb Stock Exchange": {"city": "Zagreb", "country": "Croatie", "code": "HR"},
            "Ljubljana Stock Exchange": {"city": "Ljubljana", "country": "Slovénie", "code": "SI"},
            "Tallinn Stock Exchange": {"city": "Tallinn", "country": "Estonie", "code": "EE"},
            "Riga Stock Exchange": {"city": "Riga", "country": "Lettonie", "code": "LV"},
            "Vilnius Stock Exchange": {"city": "Vilnius", "country": "Lituanie", "code": "LT"},
            "Bratislava Stock Exchange": {"city": "Bratislava", "country": "Slovaquie", "code": "SK"},
            "Bucharest Stock Exchange": {"city": "Bucarest", "country": "Roumanie", "code": "RO"},
            "Bulgarian Stock Exchange": {"city": "Sofia", "country": "Bulgarie", "code": "BG"},
            "Malta Stock Exchange": {"city": "Malte", "country": "Malte", "code": "MT"},
        }
        
        markets_list = []
        for name in SUPPORTED_WATCH_MARKETS:
            meta = market_metadata.get(name, {"city": name, "country": "Europe", "code": "EU"})
            markets_list.append({
                "name": name,
                "city": meta["city"],
                "country": meta["country"],
                "code": meta["code"],
                "test_id_segment": _test_id_segment(name),
            })
            
        # Sort by city (alphabetical), then by market name
        markets_list.sort(key=lambda m: (m["city"].casefold(), m["name"].casefold()))
        
        return _TEMPLATES.TemplateResponse(
            request,
            "search.html",
            {
                "markets": markets_list,
                "document_types": DOCUMENT_TYPES,
            },
        )

    @app.get("/searches/{job_id}", response_class=HTMLResponse)
    def results_page(request: Request, job_id: str) -> HTMLResponse:
        status = resolved_job_manager.get_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        context: dict[str, object] = {
            "job_id": job_id,
            "status": status,
            "document_types": DOCUMENT_TYPES,
            "show_initial_results": False,
        }
        if status["status"] in {"done", "partial", "failed"}:
            results, total = repository.list_results(job_id, page=1, page_size=50)
            page_size = 50
            context.update(
                {
                    "show_initial_results": True,
                    "results": results,
                    "total": total,
                    "page": 1,
                    "page_size": page_size,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                    "filters": {
                        "document_type": "",
                        "market": "",
                        "source": "",
                        "q": "",
                        "issuer_isin": "",
                        "sort": "-published_at",
                    },
                }
            )
        return _TEMPLATES.TemplateResponse(request, "results.html", context)

    @app.get("/partials/searches/{job_id}/status", response_class=HTMLResponse)
    def job_status_partial(request: Request, job_id: str) -> HTMLResponse:
        status = resolved_job_manager.get_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/job_status.html",
            {"status": status},
        )

    @app.get("/partials/searches/{job_id}/results", response_class=HTMLResponse)
    def results_table_partial(
        request: Request,
        job_id: str,
        document_type: str | None = None,
        market: str | None = None,
        source: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> HTMLResponse:
        if resolved_job_manager.get_status(job_id) is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        results, total = repository.list_results(
            job_id,
            document_type=document_type,
            market=market,
            source=source,
            q=q,
            issuer_isin=issuer_isin,
            sort=sort,
            page=page,
            page_size=page_size,
        )
        total_pages = max(1, (total + page_size - 1) // page_size)
        return _TEMPLATES.TemplateResponse(
            request,
            "partials/results_table.html",
            {
                "job_id": job_id,
                "results": results,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "filters": {
                    "document_type": document_type or "",
                    "market": market or "",
                    "source": source or "",
                    "q": q or "",
                    "issuer_isin": issuer_isin or "",
                    "sort": sort,
                },
            },
        )

    return app
