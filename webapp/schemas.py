from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field


class SearchCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    markets: list[str] = Field(min_length=1)
    date_from: date
    date_to: date
    document_types: list[str] = []
    query: str | None = None
    issuer_isin: str | None = None


class SearchCreateResponse(BaseModel):
    job_id: str
    status_url: str
    results_url: str


class InternalSearchRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=128)


class BetaFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(pattern="^(missing|accuracy|usability|speed|feature|other)$")
    message: str = Field(min_length=3, max_length=2000)
    job_id: str | None = Field(default=None, max_length=128)


class BetaFeedbackResponse(BaseModel):
    feedback_id: str
    accepted: bool = True


class SearchMarketStatus(BaseModel):
    market: str
    status: str
    results_count: int
    warning: str | None
    error: str | None


class SearchStatusResponse(BaseModel):
    job_id: str
    status: str
    results_count: int
    warnings: list[str]
    errors: list[str]
    markets: list[SearchMarketStatus]


class SearchResult(BaseModel):
    market: str
    published_at: str | None
    period_end_date: str | None
    reporting_year: int | None
    document_type: str
    title: str
    issuer_name: str | None
    issuer_isin: str | None
    issuer_lei: str | None
    file_format: str | None
    document_url: str


class SearchResultsResponse(BaseModel):
    job_id: str
    total: int
    page: int
    page_size: int
    results: list[SearchResult]
