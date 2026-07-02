from __future__ import annotations

from datetime import date
from pathlib import Path

from config import Settings
from connectors.base import Connector, ConnectorState, DocumentCandidate
from models import Issuer
from webapp.services.document_search import (
    DocumentSearchService,
    LinkSearchRequest,
)


class FakeSession:
    closed = False

    def close(self) -> None:
        self.closed = True


class FakeSourceFirstConnector(Connector):
    supports_source_first = True
    source_name = "fake-oam"
    state = ConnectorState.READY
    last_error = None

    def __init__(self, candidates: list[DocumentCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, date | None, int | None]] = []

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        self.calls.append((market, since, limit))
        return self.candidates[:limit]

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        raise AssertionError("watchlist/issuer mode must not be used")


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "unused.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024 * 1024,
        amf_base_url="https://www.info-financiere.gouv.fr",
        amf_fallback_base_urls=(),
        amf_dataset="flux-amf-new-prod",
        amf_rows=100,
    )


def test_search_links_filters_dates_and_dedupes(tmp_path: Path) -> None:
    in_range = DocumentCandidate(
        title="Annual report",
        url="https://official.test/report.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
        metadata={
            "issuer_name": "Issuer A",
            "issuer_isins": ["FR0000000001"],
        },
    )
    duplicate = DocumentCandidate(
        title="Annual report duplicate",
        url="https://official.test/report-copy.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
    )
    out_of_range = DocumentCandidate(
        title="Old annual report",
        url="https://official.test/old.pdf",
        published_date=date(2026, 6, 1),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-old",
    )
    connector = FakeSourceFirstConnector([in_range, duplicate, out_of_range])
    session = FakeSession()

    result = DocumentSearchService(
        make_settings(tmp_path),
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: connector,
    ).search_links(
        LinkSearchRequest(
            markets=("Euronext Paris",),
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 15),
        )
    )

    assert len(result.documents) == 1
    document = result.documents[0]
    assert document.market == "Euronext Paris"
    assert document.source_document_id == "doc-1"
    assert document.issuer_name == "Issuer A"
    assert document.issuer_isin == "FR0000000001"
    assert document.file_format == "pdf"
    assert result.errors == ()
    assert connector.calls == [("Euronext Paris", date(2026, 6, 10), 100000)]
    assert session.closed is True


def test_search_links_connector_error_does_not_block_other_markets(
    tmp_path: Path,
) -> None:
    candidate = DocumentCandidate(
        title="Annual report",
        url="https://official.test/report.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
    )
    connectors = {
        "Unknown Market": None,
        "Euronext Paris": FakeSourceFirstConnector([candidate]),
    }

    result = DocumentSearchService(
        make_settings(tmp_path),
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connectors[market],
    ).search_links(
        LinkSearchRequest(
            markets=("Unknown Market", "Euronext Paris"),
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 15),
        )
    )

    assert len(result.documents) == 1
    assert result.errors == ("Unknown Market: aucun connecteur",)


def test_search_links_dedupe_url_aggregates_markets(tmp_path: Path) -> None:
    candidate = DocumentCandidate(
        title="Annual report",
        url="https://official.test/shared.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-1",
    )
    connectors = {
        "Euronext Brussels": FakeSourceFirstConnector([candidate]),
        "Euronext Growth Brussels": FakeSourceFirstConnector([candidate]),
    }

    result = DocumentSearchService(
        make_settings(tmp_path),
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connectors[market],
    ).search_links(
        LinkSearchRequest(
            markets=("Euronext Brussels", "Euronext Growth Brussels"),
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 15),
            dedupe_url=True,
        )
    )

    assert len(result.documents) == 1
    assert result.documents[0].market == (
        "Euronext Brussels, Euronext Growth Brussels"
    )


def test_search_links_closes_session_on_connector_exception(tmp_path: Path) -> None:
    class FailingConnector(FakeSourceFirstConnector):
        def search_recent_documents(
            self,
            market: str,
            since: date | None = None,
            limit: int | None = None,
        ) -> list[DocumentCandidate]:
            raise RuntimeError("boom")

    session = FakeSession()
    result = DocumentSearchService(
        make_settings(tmp_path),
        session_factory=lambda **kwargs: session,
        connector_factory=lambda market, **kwargs: FailingConnector([]),
    ).search_links(
        LinkSearchRequest(
            markets=("Euronext Paris",),
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 15),
        )
    )

    assert result.documents == ()
    assert result.errors == ("Euronext Paris: boom",)
    assert session.closed is True


def test_search_links_rejects_invalid_date_range(tmp_path: Path) -> None:
    service = DocumentSearchService(make_settings(tmp_path))
    try:
        service.search_links(
            LinkSearchRequest(
                markets=("Euronext Paris",),
                date_from=date(2026, 6, 15),
                date_to=date(2026, 6, 10),
            )
        )
    except ValueError as exc:
        assert "date-from" in str(exc)
    else:
        raise AssertionError("ValueError attendue")


def test_search_links_parallel_and_callback(tmp_path: Path) -> None:
    candidate_paris = DocumentCandidate(
        title="Paris report",
        url="https://official.test/paris.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-paris",
    )
    candidate_brussels = DocumentCandidate(
        title="Brussels report",
        url="https://official.test/brussels.pdf",
        published_date=date(2026, 6, 12),
        document_type="annual_financial_report",
        source="fake-oam",
        source_document_id="doc-brussels",
    )

    connectors = {
        "Euronext Paris": FakeSourceFirstConnector([candidate_paris]),
        "Euronext Brussels": FakeSourceFirstConnector([candidate_brussels]),
    }

    completed_summaries = []
    completed_documents = []

    def on_market_complete(summary, docs):
        completed_summaries.append(summary)
        completed_documents.extend(docs)

    result = DocumentSearchService(
        make_settings(tmp_path),
        session_factory=lambda **kwargs: FakeSession(),
        connector_factory=lambda market, **kwargs: connectors[market],
    ).search_links(
        LinkSearchRequest(
            markets=("Euronext Paris", "Euronext Brussels"),
            date_from=date(2026, 6, 10),
            date_to=date(2026, 6, 15),
        ),
        on_market_complete=on_market_complete,
    )

    # Verify callback was called for both markets
    assert len(completed_summaries) == 2
    assert {s.market for s in completed_summaries} == {"Euronext Paris", "Euronext Brussels"}
    assert len(completed_documents) == 2

    # Verify final result contains both
    assert len(result.documents) == 2
    assert len(result.market_summaries) == 2
    assert result.market_summaries[0].market == "Euronext Paris"
    assert result.market_summaries[1].market == "Euronext Brussels"