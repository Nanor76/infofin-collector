from __future__ import annotations

from webapp.services.document_search import LinkSearchDocument
from webapp.services.filters import filter_documents, normalize_search_text


def _document(**overrides) -> LinkSearchDocument:
    defaults = {
        "market": "Euronext Paris",
        "source": "fake-oam",
        "source_document_id": "doc-1",
        "published_at": "2026-06-12",
        "period_end_date": "",
        "reporting_year": "",
        "document_type": "annual_financial_report",
        "classification": "",
        "title": "Rapport annuel TotalEnergies",
        "url": "https://official.test/report.pdf",
        "issuer_name": "TotalEnergies SE",
        "issuer_isin": "FR0000120271",
        "issuer_lei": "529900S21EQ1BO4ESM68",
        "category": "annual",
        "file_format": "pdf",
        "date_confidence": "high",
        "source_publication_date_raw": "",
    }
    defaults.update(overrides)
    return LinkSearchDocument(**defaults)


def test_filter_by_annual_document_type() -> None:
    documents = (
        _document(document_type="annual_financial_report"),
        _document(
            document_type="half_year_financial_report",
            title="Rapport semestriel",
        ),
    )
    filtered = filter_documents(
        documents,
        document_types=("annual_financial_report",),
    )
    assert len(filtered) == 1
    assert filtered[0].document_type == "annual_financial_report"


def test_filter_by_accent_insensitive_query() -> None:
    documents = (
        _document(title="Rapport annuel TotalEnergies"),
        _document(title="Autre société", issuer_name="Autre"),
    )
    filtered = filter_documents(documents, query="totalenergies")
    assert len(filtered) == 1
    filtered_accent = filter_documents(documents, query="énergies")
    assert len(filtered_accent) == 1


def test_normalize_search_text_strips_accents() -> None:
    assert normalize_search_text("Éléphant") == "elephant"


def test_filter_by_isin_in_comma_joined_list() -> None:
    documents = (
        _document(issuer_isin="FR0000120271, US0000000001"),
        _document(issuer_isin="DE0000000001"),
    )
    filtered = filter_documents(documents, issuer_isin="us0000000001")
    assert len(filtered) == 1
    assert "US0000000001" in filtered[0].issuer_isin


def test_filter_by_source() -> None:
    documents = (
        _document(source="fake-oam"),
        _document(source="other-source"),
    )
    filtered = filter_documents(documents, sources=("fake-oam",))
    assert len(filtered) == 1
    assert filtered[0].source == "fake-oam"


def test_text_query_does_not_match_private_provenance() -> None:
    documents = (
        _document(
            source="private-collector",
            source_document_id="secret-42",
            category="private-taxonomy",
        ),
    )

    assert filter_documents(documents, query="private-collector") == ()
    assert filter_documents(documents, query="secret-42") == ()
    assert filter_documents(documents, query="private-taxonomy") == ()


def test_filter_by_format() -> None:
    documents = (
        _document(file_format="pdf"),
        _document(file_format="xhtml"),
    )
    filtered = filter_documents(documents, formats=("pdf",))
    assert len(filtered) == 1
    assert filtered[0].file_format == "pdf"


def test_filter_by_date_confidence() -> None:
    documents = (
        _document(date_confidence="high"),
        _document(date_confidence="low"),
    )
    filtered = filter_documents(documents, date_confidences=("high",))
    assert len(filtered) == 1
    assert filtered[0].date_confidence == "high"


def test_filter_composes_multiple_filters() -> None:
    documents = (
        _document(
            document_type="annual_financial_report",
            source="fake-oam",
            file_format="pdf",
            date_confidence="high",
            issuer_isin="FR0000120271",
        ),
        _document(
            document_type="half_year_financial_report",
            source="fake-oam",
            file_format="pdf",
            date_confidence="high",
        ),
    )
    filtered = filter_documents(
        documents,
        document_types=("annual_financial_report",),
        sources=("fake-oam",),
        formats=("pdf",),
        date_confidences=("high",),
        issuer_isin="FR0000120271",
        query="total",
    )
    assert len(filtered) == 1
