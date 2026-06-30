from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class SearchCreateRequest(BaseModel):
    markets: list[str] = Field(min_length=1)
    date_from: date
    date_to: date
    document_types: list[str] = []
    query: str | None = None
    issuer_isin: str | None = None
    sources: list[str] = []
    formats: list[str] = []
    date_confidences: list[str] = []
    max_candidates: int = Field(default=100000, ge=1, le=500000)
    dedupe_url: bool = False


class SearchCreateResponse(BaseModel):
    job_id: str
    status_url: str
    results_url: str


class SearchStatusResponse(BaseModel):
    job_id: str
    status: str
    results_count: int
    warnings: list[str]
    errors: list[str]
    markets: list[dict[str, object]]


class SearchResultsResponse(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    results: list[dict[str, object]]