from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from models import Issuer


@dataclass(frozen=True, slots=True)
class DocumentCandidate:
    title: str
    url: str
    published_date: date | None
    document_type: str
    source: str
    source_document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    classification: str | None = None
    classification_reason: str | None = None
    matched_positive_terms: list[str] | None = None
    matched_negative_terms: list[str] | None = None
    published_at: date | None = None
    period_end_date: date | None = None
    reporting_year: int | None = None
    date_confidence: str | None = None  # "high" or "low"
    date_extraction_reason: str | None = None
    source_publication_date_raw: str | None = None
    source_period_date_raw: str | None = None

    def __post_init__(self) -> None:
        if self.published_at is None and self.published_date is not None:
            object.__setattr__(self, "published_at", self.published_date)
        if self.date_confidence is None:
            object.__setattr__(
                self,
                "date_confidence",
                "high" if self.published_at is not None else "low",
            )



class ConnectorError(RuntimeError):
    pass


class ConnectorState(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    STUB = "stub"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EndpointAttempt:
    name: str
    base_url: str
    dataset: str | None
    endpoint: str
    method: str
    http_status: int | None
    success: bool
    total_count: int | None = None
    response_excerpt: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class DatasetCandidate:
    dataset_id: str
    title: str
    records_count: int | None
    base_url: str


@dataclass(frozen=True, slots=True)
class SourceDiscovery:
    source: str
    query: str
    candidates: tuple[DatasetCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    source: str
    state: ConnectorState
    base_url: str
    dataset: str
    selected_endpoint: str | None
    total_count: int | None
    fields: tuple[str, ...]
    example_record: dict[str, Any] | None
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


class Connector(ABC):
    market: str
    source_name: str
    state: ConnectorState = ConnectorState.READY
    last_error: str | None = None
    supports_source_first: bool = False

    def mark_degraded(self, error: str) -> None:
        self.state = ConnectorState.DEGRADED
        self.last_error = error

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        raise NotImplementedError(
            f"{self.source_name} ne supporte pas le mode source-first"
        )

    def search_recent_documents_filtered(
        self,
        market: str,
        since: date | None = None,
        until: date | None = None,
        document_types: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        """Use source-side filters when available, otherwise filter downstream."""
        return self.search_recent_documents(market, since=since, limit=limit)

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        return self.search_documents(issuer)

    def materialize_candidate(
        self,
        candidate: DocumentCandidate,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        return [candidate]

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 1

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 1

    @property
    def scanned_notices(self) -> int:
        return int(getattr(self, "_scanned_notices", 0))

    @property
    def details_visited(self) -> int:
        return int(getattr(self, "_details_visited", 0))

    @property
    def cache_hits(self) -> int:
        return int(getattr(self, "_cache_hits", 0))

    @abstractmethod
    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        """Return rule-selected official documents for one issuer."""
