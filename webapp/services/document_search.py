from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse

from connectors import connector_for_market
from connectors.base import Connector, DocumentCandidate
from http_client import build_http_session
from load_watchlist import normalize_market
from webapp.services.filters import filter_documents


@dataclass(frozen=True, slots=True)
class LinkSearchRequest:
    markets: tuple[str, ...]
    date_from: date
    date_to: date
    document_types: tuple[str, ...] = ()
    query: str | None = None
    issuer_isin: str | None = None
    sources: tuple[str, ...] = ()
    formats: tuple[str, ...] = ()
    date_confidences: tuple[str, ...] = ()
    max_candidates: int = 100000
    dedupe_url: bool = False


@dataclass(frozen=True, slots=True)
class LinkSearchDocument:
    market: str
    source: str
    source_document_id: str
    published_at: str
    period_end_date: str
    reporting_year: int | str
    document_type: str
    classification: str
    title: str
    url: str
    issuer_name: str
    issuer_isin: str
    issuer_lei: str
    category: str
    file_format: str
    date_confidence: str
    source_publication_date_raw: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MarketSearchSummary:
    market: str
    source: str
    status: str
    candidates_returned: int = 0
    documents_count: int = 0
    warning: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class LinkSearchResultSet:
    request: LinkSearchRequest
    documents: tuple[LinkSearchDocument, ...]
    market_summaries: tuple[MarketSearchSummary, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _document_publication_date(candidate: DocumentCandidate) -> date | None:
    return candidate.published_at or candidate.published_date


def _join_metadata_value(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item is not None)
    return "" if value is None else str(value)


def _url_extension_without_dot(url: str) -> str:
    extension = PurePosixPath(urlparse(url).path).suffix
    return extension.lstrip(".") if extension else ""


def _candidate_to_document(
    market: str,
    candidate: DocumentCandidate,
) -> LinkSearchDocument:
    metadata = dict(candidate.metadata or {})
    publication_date = _document_publication_date(candidate)
    file_format = _join_metadata_value(metadata.get("file_format"))
    if not file_format:
        file_format = _url_extension_without_dot(candidate.url)
    return LinkSearchDocument(
        market=market,
        source=candidate.source,
        source_document_id=candidate.source_document_id or "",
        published_at=(
            publication_date.isoformat() if publication_date else ""
        ),
        period_end_date=(
            candidate.period_end_date.isoformat()
            if candidate.period_end_date
            else ""
        ),
        reporting_year=candidate.reporting_year or "",
        document_type=candidate.document_type,
        classification=candidate.classification or "",
        title=candidate.title,
        url=candidate.url,
        issuer_name=_join_metadata_value(
            metadata.get("issuer_name")
            or metadata.get("issuer")
            or metadata.get("company_name")
        ),
        issuer_isin=_join_metadata_value(
            metadata.get("issuer_isin")
            or metadata.get("issuer_isins")
            or metadata.get("isin")
        ),
        issuer_lei=_join_metadata_value(
            metadata.get("issuer_lei") or metadata.get("lei")
        ),
        category=_join_metadata_value(metadata.get("category")),
        file_format=file_format,
        date_confidence=candidate.date_confidence or "",
        source_publication_date_raw=(
            candidate.source_publication_date_raw or ""
        ),
        metadata=metadata,
    )


def _dedupe_documents_by_url(
    documents: tuple[LinkSearchDocument, ...],
) -> tuple[LinkSearchDocument, ...]:
    deduped: dict[str, LinkSearchDocument] = {}
    markets_by_url: dict[str, list[str]] = {}
    for document in documents:
        url = document.url
        if not url:
            continue
        if url not in deduped:
            deduped[url] = document
            markets_by_url[url] = []
        if document.market and document.market not in markets_by_url[url]:
            markets_by_url[url].append(document.market)
    result: list[LinkSearchDocument] = []
    for url, document in deduped.items():
        result.append(
            replace(document, market=", ".join(markets_by_url[url]))
        )
    return tuple(result)


class DocumentSearchService:
    def __init__(
        self,
        settings,
        *,
        session_factory=build_http_session,
        connector_factory=connector_for_market,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.connector_factory = connector_factory

    def search_links(self, request: LinkSearchRequest) -> LinkSearchResultSet:
        if request.date_from > request.date_to:
            raise ValueError(
                "--date-from doit être inférieur ou égal à --date-to"
            )
        if request.max_candidates < 1:
            raise ValueError("max_candidates doit être supérieur ou égal à 1")

        session = self.session_factory(
            retries=self.settings.http_retries,
            backoff_factor=self.settings.http_backoff_factor,
            user_agent=self.settings.user_agent,
            verify=self.settings.http_verify_ssl,
        )
        documents: list[LinkSearchDocument] = []
        errors: list[str] = []
        warnings: list[str] = []
        market_summaries: list[MarketSearchSummary] = []
        try:
            for market in request.markets:
                normalized_market = normalize_market(market)
                connector = self.connector_factory(
                    normalized_market,
                    settings=self.settings,
                    session=session,
                )
                if connector is None:
                    errors.append(f"{normalized_market}: aucun connecteur")
                    market_summaries.append(
                        MarketSearchSummary(
                            market=normalized_market,
                            source="",
                            status="error",
                            error="aucun connecteur",
                        )
                    )
                    continue
                if not getattr(connector, "supports_source_first", False):
                    message = "source-first non supporté"
                    errors.append(f"{normalized_market}: {message}")
                    market_summaries.append(
                        MarketSearchSummary(
                            market=normalized_market,
                            source=getattr(connector, "source_name", ""),
                            status="error",
                            error=message,
                        )
                    )
                    continue

                try:
                    candidates = connector.search_recent_documents(
                        normalized_market,
                        since=request.date_from,
                        limit=request.max_candidates,
                    )
                except Exception as exc:
                    errors.append(f"{normalized_market}: {exc}")
                    market_summaries.append(
                        MarketSearchSummary(
                            market=normalized_market,
                            source=getattr(connector, "source_name", ""),
                            status="error",
                            error=str(exc),
                        )
                    )
                    continue

                unique: dict[tuple[str, str], DocumentCandidate] = {}
                for candidate in candidates:
                    publication_date = _document_publication_date(candidate)
                    if publication_date is None:
                        continue
                    if (
                        publication_date < request.date_from
                        or publication_date > request.date_to
                    ):
                        continue
                    key = (
                        candidate.source,
                        candidate.source_document_id or candidate.url,
                    )
                    unique.setdefault(key, candidate)

                market_documents = tuple(
                    _candidate_to_document(normalized_market, candidate)
                    for candidate in sorted(
                        unique.values(),
                        key=lambda item: (
                            _document_publication_date(item) or date.min,
                            item.source,
                            item.title.casefold(),
                            item.url,
                        ),
                        reverse=True,
                    )
                )
                documents.extend(market_documents)
                warning = ""
                if len(candidates) >= request.max_candidates:
                    warning = (
                        "le nombre de candidats retournés atteint "
                        f"--max-candidates={request.max_candidates}; "
                        "augmenter ce plafond pour prouver "
                        "l'exhaustivité sur cette période"
                    )
                    warnings.append(f"{normalized_market}: {warning}")
                market_summaries.append(
                    MarketSearchSummary(
                        market=normalized_market,
                        source=getattr(connector, "source_name", ""),
                        status="ok",
                        candidates_returned=len(candidates),
                        documents_count=len(market_documents),
                        warning=warning,
                    )
                )
        finally:
            close = getattr(session, "close", None)
            if callable(close):
                close()

        filtered = filter_documents(
            tuple(documents),
            document_types=request.document_types,
            query=request.query,
            issuer_isin=request.issuer_isin,
            sources=request.sources,
            formats=request.formats,
            date_confidences=request.date_confidences,
        )
        if request.dedupe_url:
            filtered = _dedupe_documents_by_url(filtered)

        return LinkSearchResultSet(
            request=request,
            documents=filtered,
            market_summaries=tuple(market_summaries),
            warnings=tuple(warnings),
            errors=tuple(errors),
        )