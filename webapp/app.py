from __future__ import annotations

import base64
import binascii
import re
import secrets
import tempfile
import unicodedata
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import Settings
from connectors import SUPPORTED_WATCH_MARKETS
from db import Database
from webapp.cloud_jobs import CloudRunJobLauncher, CloudTasksJobLauncher
from webapp.firestore_repository import (
    FirestoreWebSearchRepository,
    GoogleFirestoreDocumentStore,
)
from webapp.jobs import CloudJobManager, JobManager, run_stored_search
from webapp.repositories import WebSearchRepository
from webapp.schemas import (
    SearchCreateRequest,
    SearchCreateResponse,
    InternalSearchRunRequest,
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


def _request_from_schema(
    payload: SearchCreateRequest,
    *,
    max_candidates: int,
) -> LinkSearchRequest:
    return LinkSearchRequest(
        markets=tuple(payload.markets),
        date_from=payload.date_from,
        date_to=payload.date_to,
        document_types=tuple(payload.document_types),
        query=payload.query,
        issuer_isin=payload.issuer_isin,
        sources=(),
        formats=(),
        date_confidences=(),
        max_candidates=max_candidates,
        dedupe_url=True,
    )


def _public_search_status(status: dict[str, object]) -> dict[str, object]:
    raw_warnings = list(status.get("warnings") or [])
    raw_errors = list(status.get("errors") or [])
    public_status = str(status.get("status") or "")
    if public_status == "queued":
        public_status = "running"
    public_markets = []
    for run in list(status.get("markets") or []):
        public_markets.append(
            {
                "market": str(run.get("market") or ""),
                "status": str(run.get("status") or ""),
                "results_count": int(run.get("results_count") or 0),
                "warning": (
                    "Les résultats peuvent être incomplets pour ce marché."
                    if run.get("warning")
                    else None
                ),
                "error": (
                    "La recherche n'a pas abouti pour ce marché."
                    if run.get("error")
                    else None
                ),
            }
        )
    return {
        "job_id": str(status.get("job_id") or ""),
        "status": public_status,
        "results_count": int(status.get("results_count") or 0),
        "warnings": (
            ["Certains résultats peuvent être incomplets."]
            if raw_warnings
            else []
        ),
        "errors": (
            ["Une partie de la recherche n'a pas abouti."]
            if raw_errors
            else []
        ),
        "markets": public_markets,
    }


def _public_result(row: dict[str, object]) -> dict[str, object]:
    return {
        "market": row.get("market") or "",
        "published_at": row.get("published_at"),
        "period_end_date": row.get("period_end_date"),
        "reporting_year": row.get("reporting_year"),
        "document_type": row.get("document_type") or "",
        "title": row.get("title") or "",
        "issuer_name": row.get("issuer_name"),
        "issuer_isin": row.get("issuer_isin"),
        "issuer_lei": row.get("issuer_lei"),
        "file_format": row.get("file_format"),
        "document_url": row.get("url") or "",
    }


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    repository=None,
    job_manager=None,
    search_service=None,
) -> FastAPI:
    resolved_settings = settings or Settings.from_env()
    if resolved_settings.web_storage_backend not in {"sqlite", "firestore"}:
        raise ValueError("INFOFIN_WEB_STORAGE_BACKEND doit valoir sqlite ou firestore")
    if resolved_settings.web_job_backend not in {
        "local",
        "cloud-run",
        "cloud-tasks",
    }:
        raise ValueError(
            "INFOFIN_WEB_JOB_BACKEND doit valoir local, cloud-run ou cloud-tasks"
        )
    if (
        resolved_settings.web_job_backend in {"cloud-run", "cloud-tasks"}
        and resolved_settings.web_storage_backend != "firestore"
    ):
        raise ValueError("Les backends cloud nécessitent le stockage firestore")

    resolved_database = database
    resolved_repository = repository
    if resolved_repository is None:
        if resolved_settings.web_storage_backend == "firestore":
            if not resolved_settings.google_cloud_project:
                raise ValueError("GOOGLE_CLOUD_PROJECT est requis avec Firestore")
            resolved_repository = FirestoreWebSearchRepository(
                store=GoogleFirestoreDocumentStore(
                    project=resolved_settings.google_cloud_project,
                ),
                prefix=resolved_settings.firestore_collection_prefix,
            )
        else:
            resolved_database = resolved_database or Database(resolved_settings.db_path)
            resolved_database.initialize_web_search_schema()
            resolved_repository = WebSearchRepository(resolved_database)

    resolved_search_service = search_service or DocumentSearchService(
        resolved_settings
    )
    resolved_job_manager = job_manager
    if resolved_job_manager is None:
        if resolved_settings.web_job_backend == "cloud-run":
            if not resolved_settings.cloud_run_search_job:
                raise ValueError("INFOFIN_CLOUD_RUN_JOB est requis")
            resolved_job_manager = CloudJobManager(
                repository=resolved_repository,
                launcher=CloudRunJobLauncher(
                    project=resolved_settings.google_cloud_project,
                    region=resolved_settings.google_cloud_region,
                    job_name=resolved_settings.cloud_run_search_job,
                ),
            )
        elif resolved_settings.web_job_backend == "cloud-tasks":
            if not resolved_settings.cloud_tasks_queue:
                raise ValueError("INFOFIN_CLOUD_TASKS_QUEUE est requis")
            if not resolved_settings.web_service_url:
                raise ValueError("INFOFIN_WEB_SERVICE_URL est requis")
            resolved_job_manager = CloudJobManager(
                repository=resolved_repository,
                launcher=CloudTasksJobLauncher(
                    project=resolved_settings.google_cloud_project,
                    region=resolved_settings.google_cloud_region,
                    queue_name=resolved_settings.cloud_tasks_queue,
                    service_url=resolved_settings.web_service_url,
                    username=resolved_settings.web_access_username,
                    password=resolved_settings.web_access_password,
                ),
            )
        else:
            resolved_job_manager = JobManager(
                repository=resolved_repository,
                search_service=resolved_search_service,
                max_workers=resolved_settings.web_workers,
            )

    app = FastAPI(
        title="InfoFin Document Search",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def require_basic_auth(request: Request, call_next):
        expected_password = resolved_settings.web_access_password
        if not expected_password:
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        scheme, _, encoded_credentials = authorization.partition(" ")
        try:
            decoded_credentials = base64.b64decode(
                encoded_credentials,
                validate=True,
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            decoded_credentials = ""
        username, separator, password = decoded_credentials.partition(":")
        username_matches = secrets.compare_digest(
            username.encode("utf-8"),
            resolved_settings.web_access_username.encode("utf-8"),
        )
        password_matches = secrets.compare_digest(
            password.encode("utf-8"),
            expected_password.encode("utf-8"),
        )
        if (
            scheme.casefold() != "basic"
            or not separator
            or not username_matches
            or not password_matches
        ):
            return PlainTextResponse(
                "Authentification requise.",
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Basic realm="InfoFin", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
        return await call_next(request)

    app.mount(
        "/static",
        StaticFiles(directory=str(_WEBAPP_DIR / "static")),
        name="static",
    )
    app.state.settings = resolved_settings
    app.state.database = resolved_database
    app.state.repository = resolved_repository
    app.state.job_manager = resolved_job_manager

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "storage_backend": resolved_settings.web_storage_backend,
            "job_backend": resolved_settings.web_job_backend,
        }

    @app.post("/internal/search-worker", status_code=204)
    def run_internal_search(payload: InternalSearchRunRequest) -> Response:
        if resolved_settings.web_job_backend != "cloud-tasks":
            raise HTTPException(status_code=404, detail="Route inconnue")
        job = resolved_repository.get_job(payload.job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        if job["status"] != "queued":
            return Response(status_code=204)
        run_stored_search(
            repository=resolved_repository,
            search_service=resolved_search_service,
            job_id=payload.job_id,
            request=job["request"],
        )
        return Response(status_code=204)

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
        job_id = resolved_job_manager.submit(
            _request_from_schema(
                payload,
                max_candidates=resolved_settings.web_max_candidates,
            )
        )
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
        return SearchStatusResponse(**_public_search_status(status))

    @app.get("/api/searches/{job_id}/results", response_model=SearchResultsResponse)
    def get_search_results(
        job_id: str,
        document_type: str | None = None,
        market: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> SearchResultsResponse:
        if resolved_job_manager.get_status(job_id) is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        results, total = resolved_repository.list_results(
            job_id,
            document_type=document_type,
            market=market,
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
            results=[_public_result(row) for row in results],
        )

    @app.get("/api/searches/{job_id}/export")
    def export_search_results(
        job_id: str,
        format: str = Query(default="csv", pattern="^(csv|json)$"),
        document_type: str | None = None,
        market: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
    ) -> FileResponse:
        if resolved_job_manager.get_status(job_id) is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        results: list[dict[str, object]] = []
        page = 1
        while True:
            batch, total = resolved_repository.list_results(
                job_id,
                document_type=document_type,
                market=market,
                q=q,
                issuer_isin=issuer_isin,
                page=page,
                page_size=200,
            )
            results.extend(batch)
            if len(results) >= total or not batch:
                break
            page += 1
        rows = [_public_result(row) for row in results]
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
            "status": _public_search_status(status),
            "document_types": DOCUMENT_TYPES,
            "show_initial_results": False,
        }
        if status["status"] in {"done", "partial", "failed"}:
            results, total = resolved_repository.list_results(
                job_id, page=1, page_size=50
            )
            page_size = 50
            context.update(
                {
                    "show_initial_results": True,
                    "results": [
                        _public_result(row) for row in results
                    ],
                    "total": total,
                    "page": 1,
                    "page_size": page_size,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                    "filters": {
                        "document_type": "",
                        "market": "",
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
            {"status": _public_search_status(status)},
        )

    @app.get("/partials/searches/{job_id}/results", response_class=HTMLResponse)
    def results_table_partial(
        request: Request,
        job_id: str,
        document_type: str | None = None,
        market: str | None = None,
        q: str | None = None,
        issuer_isin: str | None = None,
        sort: str = "-published_at",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=50, ge=1, le=200),
    ) -> HTMLResponse:
        status = resolved_job_manager.get_status(job_id)
        if status is None:
            raise HTTPException(status_code=404, detail="Job inconnu")
        if (
            resolved_settings.web_job_backend in {"cloud-run", "cloud-tasks"}
            and status["status"] in {"queued", "running"}
        ):
            # Avoid rereading every Firestore result during two-second polling.
            results, total = [], 0
        else:
            results, total = resolved_repository.list_results(
                job_id,
                document_type=document_type,
                market=market,
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
                "results": [_public_result(row) for row in results],
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": total_pages,
                "filters": {
                    "document_type": document_type or "",
                    "market": market or "",
                    "q": q or "",
                    "issuer_isin": issuer_isin or "",
                    "sort": sort,
                },
            },
        )

    return app
