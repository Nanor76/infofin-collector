from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from connectors.bulgaria_bse_x3news import (
    BulgariaBseX3NewsConnector,
    _issuer_query_match,
    classify_bulgaria_document,
    extract_bulgaria_date_info,
    parse_apache_index,
    select_active_buckets,
)


FIXTURES = Path(__file__).parent / "fixtures"
ROOT_HTML = (FIXTURES / "bulgaria_x3news_root.html").read_text(encoding="utf-8")
ISSUER_HTML = (FIXTURES / "bulgaria_x3news_issuer.html").read_text(encoding="utf-8")


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