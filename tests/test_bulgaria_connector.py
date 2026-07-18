from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from connectors.bulgaria_bse_x3news import (
    BulgariaBseX3NewsConnector,
    _issuer_query_match,
    classify_bulgaria_document,
    classify_x3news_category,
    extract_x3news_detail_period,
    extract_bulgaria_date_info,
    parse_apache_index,
    parse_x3news_attachments,
    parse_x3news_listing,
    select_active_buckets,
)


FIXTURES = Path(__file__).parent / "fixtures"
ROOT_HTML = (FIXTURES / "bulgaria_x3news_root.html").read_text(encoding="utf-8")
ISSUER_HTML = (FIXTURES / "bulgaria_x3news_issuer.html").read_text(encoding="utf-8")
X3NEWS_LIST_HTML = (FIXTURES / "bulgaria_x3news_list.html").read_text(
    encoding="utf-8"
)
X3NEWS_DETAIL_HTML = (FIXTURES / "bulgaria_x3news_detail.html").read_text(
    encoding="utf-8"
)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.status_code = 200

    def raise_for_status(self) -> None:
        return None


class FakeX3NewsSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        params = dict(kwargs.get("params") or {})
        self.calls.append((url, params))
        if params.get("page") == "News":
            return FakeResponse(X3NEWS_LIST_HTML)
        if params.get("page") == "ShowNews":
            return FakeResponse(X3NEWS_DETAIL_HTML)
        raise AssertionError(f"Unexpected URL: {url} {params}")


def test_parse_x3news_listing_classifies_annual_and_quarterly_reports() -> None:
    notices, page_count = parse_x3news_listing(
        X3NEWS_LIST_HTML,
        base_url="https://www.x3news.com",
    )

    assert page_count == 2
    assert [notice.extri_id for notice in notices] == ["199230", "198175"]
    assert notices[0].document_type == "quarterly_financial_report"
    assert notices[1].document_type == "annual_financial_report"
    assert notices[1].published_at == date(2026, 4, 30)
    assert notices[1].period_end_date == date(2025, 12, 31)


def test_x3news_semi_annual_category_is_not_misclassified_as_annual() -> None:
    assert classify_x3news_category(
        "Semi-annual report in pursuance of Art. 100f"
    ) == "half_year_financial_report"


def test_parse_x3news_attachments_builds_stable_download_urls() -> None:
    attachments = parse_x3news_attachments(
        X3NEWS_DETAIL_HTML,
        base_url="https://www.x3news.com",
    )

    assert [attachment.attachment_id for attachment in attachments] == [
        "686357",
        "686356",
        "686358",
    ]
    assert attachments[1].download_url == (
        "https://www.x3news.com/show/download.php?id=686356"
    )


def test_x3news_detail_supplies_period_when_listing_category_is_generic() -> None:
    period_end, reporting_year = extract_x3news_detail_period(
        X3NEWS_DETAIL_HTML,
        "annual_financial_report",
    )

    assert period_end == date(2025, 12, 31)
    assert reporting_year == 2025


def test_filtered_search_complements_archive_with_current_x3news_annual_report() -> None:
    session = FakeX3NewsSession()
    connector = BulgariaBseX3NewsConnector(
        session=session,
        base_url="https://archive.example.test",
        portal_base_url="https://www.x3news.com",
        rate_limit_seconds=0,
        max_candidates_per_source=10,
    )

    documents = connector.search_recent_documents_filtered(
        "Bulgarian Stock Exchange",
        since=date(2025, 7, 14),
        until=date(2026, 7, 14),
        document_types=("annual_financial_report",),
        limit=1,
    )

    assert len(documents) == 1
    assert documents[0].source == "bulgaria_x3news"
    assert documents[0].source_document_id == "198175:686356"
    assert documents[0].url.endswith("/show/download.php?id=686356")
    assert documents[0].document_type == "annual_financial_report"
    assert documents[0].period_end_date == date(2025, 12, 31)
    assert documents[0].metadata["file_format"] == "zip"
    assert not any(
        params.get("page") == "ShowNews" and params.get("ExtriID") == "199230"
        for _, params in session.calls
    )


def test_parse_apache_index_cyrillic_bucket_names() -> None:
    entries = parse_apache_index(ROOT_HTML)
    assert len(entries) == 3
    assert entries[0].name == "Финансови отчети към 31.12.2024/"
    assert entries[0].last_modified == "2025-06-09 11:10"


def test_select_active_buckets_prefers_recent_periodic() -> None:
    entries = parse_apache_index(ROOT_HTML)
    selected = select_active_buckets(
        entries,
        since=date(2025, 1, 1),
        max_buckets=2,
        today=date(2026, 6, 18),
    )
    names = [bucket.name for bucket in selected]
    assert any("30.06.2025" in name for name in names)
    assert all("чл. 17" not in name for name in names)


def test_last_modified_parsing() -> None:
    dates = extract_bulgaria_date_info(
        bucket_name="Финансови отчети към 31.12.2024/",
        last_modified="2025-04-02 09:40",
        filename="Доклад за дейността.pdf",
    )
    assert dates["published_at"] == date(2025, 4, 2)
    assert dates["period_end_date"] == date(2024, 12, 31)
    assert dates["reporting_year"] == 2024


@pytest.mark.parametrize(
    ("filename", "bucket", "expected"),
    [
        (
            "Доклад за дейността.pdf",
            "Финансови отчети към 31.12.2024/",
            "annual_financial_report",
        ),
        (
            "ГФО-30.06.2025 г..pdf",
            "Финансови отчети към 30.06.2025 г./",
            "half_year_financial_report",
        ),
        (
            "Одиторски доклад, отчети и приложение ГФО.zip",
            "Финансови отчети към 31.12.2024/",
            "annual_financial_report",
        ),
    ],
)
def test_periodic_classification(filename: str, bucket: str, expected: str) -> None:
    assert classify_bulgaria_document(filename, bucket_name=bucket)[0] == expected


@pytest.mark.parametrize(
    "filename",
    [
        "Вътрешна информация по чл.7 от Регламент ЕС 596 2014.pdf",
        "Декларация чл.100, ал.4.pdf",
        "BondReport-30-06-2025.pdf",
    ],
)
def test_mar_and_non_periodic_rejection(filename: str) -> None:
    classification, _, _, negative = classify_bulgaria_document(
        filename,
        bucket_name="Финансови отчети към 30.06.2025 г./",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_issuer_index_filters_accepted_files() -> None:
    files = parse_apache_index(ISSUER_HTML)
    connector = BulgariaBseX3NewsConnector(session=object())
    accepted = 0
    for file_entry in files:
        filing = connector._build_filing(
            issuer_name="Тибиш ЕАД",
            bucket=parse_apache_index(ROOT_HTML)[0],
            bucket_url="https://example.test/bucket/",
            file_entry=file_entry,
            file_url=f"https://example.test/{file_entry.href}",
        )
        if filing is not None:
            accepted += 1
    assert accepted == 3


def test_discover_prefix_match_links_tibish_to_tibiel() -> None:
    assert _issuer_query_match(
        "Тибиш",
        "Тибиел ЕООД/",
        mode="discover",
    )
    assert not _issuer_query_match(
        "Тибиш",
        "Тибиел ЕООД/",
        mode="strict",
    )


def test_stable_source_document_id_is_deterministic() -> None:
    connector = BulgariaBseX3NewsConnector(session=object())
    filing = connector._build_filing(
        issuer_name="Тибиш ЕАД",
        bucket=parse_apache_index(ROOT_HTML)[0],
        bucket_url="https://example.test/bucket/",
        file_entry=parse_apache_index(ISSUER_HTML)[0],
        file_url="https://download.bse-sofia.bg/x3news/example.pdf",
    )
    assert filing is not None
    candidate = connector._filing_candidate(filing)
    assert candidate.source_document_id
    assert candidate.metadata["strict_issuer_name_match"] is True
    assert "Тибиш ЕАД" in candidate.metadata["issuer_aliases"]
