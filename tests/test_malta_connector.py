from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from connectors.malta_mse_oam import (
    MaltaMseOamConnector,
    build_malta_oam_url,
    classify_malta_document,
    extract_malta_date_info,
    normalize_malta_cdn_url,
    parse_malta_listing,
)


FIXTURES = Path(__file__).parent / "fixtures"
LISTING_HTML = (FIXTURES / "malta_oam_listing.html").read_text(encoding="utf-8")


def test_build_malta_oam_url() -> None:
    url = build_malta_oam_url(
        from_date=date(2026, 5, 1),
        to_date=date(2026, 6, 19),
    )
    assert url.endswith("/officially-appointed-mechanism?from=2026-05-01&to=2026-06-19")


def test_normalize_malta_cdn_url_strips_backslash() -> None:
    raw = (
        "https://cdn.borzamalta.com.mt/ESEFAPP/\\TEP_20260131_CON_AFR_"
        "635400OFBLZGT1KLUV50_20260528113200425.zip"
    )
    assert "\\" not in normalize_malta_cdn_url(raw)
    assert normalize_malta_cdn_url(raw).endswith(".zip")


def test_listing_parsing_finds_trident_annual_report() -> None:
    page = parse_malta_listing(LISTING_HTML)
    annual = next(
        notice
        for notice in page.notices
        if notice.issuer_name == "Trident Estates plc"
        and "Annual Report" in notice.title
    )
    assert annual.published_at == date(2026, 5, 28)
    assert annual.issuer_lei == "635400OFBLZGT1KLUV50"
    assert len(annual.files) == 3
    assert any(item.file_format == "pdf" for item in annual.files)
    assert any(item.file_format == "zip" for item in annual.files)
    assert any(item.file_format == "xhtml" for item in annual.files)


def test_listing_parsing_rejects_fas_buyback_agm() -> None:
    page = parse_malta_listing(LISTING_HTML)
    titles = {notice.title for notice in page.notices}
    assert "Updated Financial Analysis Summary" in titles
    assert "SHARE BUYBACK PROGRAMME WEEK ENDED 12 JUNE 2026" in titles
    assert "26th Annual General Meeting - Notice to Shareholders" in titles
    connector = MaltaMseOamConnector(session=object())
    accepted = [
        notice.title
        for notice in page.notices
        if connector._accepted_notice(notice)
    ]
    assert "Annual Report and Financial Statements 2026" in accepted
    assert "Interim report Q1 2026" in accepted
    assert "Updated Financial Analysis Summary" not in accepted
    assert "SHARE BUYBACK PROGRAMME WEEK ENDED 12 JUNE 2026" not in accepted
    assert "26th Annual General Meeting - Notice to Shareholders" not in accepted


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Annual Report and Financial Statements 2026", "annual_financial_report"),
        ("Interim report Q1 2026", "quarterly_financial_report"),
        ("2026 HALF YEARLY", "half_year_financial_report"),
    ],
)
def test_periodic_classification(title: str, expected: str) -> None:
    assert classify_malta_document(title)[0] == expected


@pytest.mark.parametrize(
    ("title", "esef_url", "expected"),
    [
        (
            "Approval and publication of Audited Financial Statements for the financial year ended 31 December 2025 for Issuer and Guarantor",
            "https://cdn.borzamalta.com.mt/ESEFAPP/SGFP_20251231_CON_AFR_213800A1PC5JJQ42YB81_20260429120834896.zip",
            "annual_financial_report",
        ),
        (
            "Approval of the Financial Statements and Declaration of an Interim Dividend",
            "https://cdn.borzamalta.com.mt/ESEFAPP/IHIP_20251231_CON_AFR_529900LVB0R279MUX376_20260424145718346.zip",
            "annual_financial_report",
        ),
    ],
)
def test_esef_afr_is_not_confused_by_ids_or_interim_dividend(
    title: str,
    esef_url: str,
    expected: str,
) -> None:
    assert classify_malta_document(title, esef_urls=(esef_url,))[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Updated Financial Analysis Summary",
        "SHARE BUYBACK PROGRAMME",
        "Scheduled Annual General Meeting",
        "Notice of Approval of P3 Disclosures Report 2025",
    ],
)
def test_non_periodic_classification(title: str) -> None:
    classification, _, _, negative = classify_malta_document(title)
    assert classification == "other_regulatory_announcement"
    assert negative


def test_esef_date_extraction() -> None:
    dates = extract_malta_date_info(
        "Annual Report and Financial Statements 2026",
        "28-05-2026 13:10",
        filenames=("TRI88.pdf",),
        esef_urls=(
            "https://cdn.borzamalta.com.mt/ESEFAPP/TEP_20260131_CON_AFR_"
            "635400OFBLZGT1KLUV50_20260528113200425.zip",
        ),
    )
    assert dates["published_at"] == date(2026, 5, 28)
    assert dates["period_end_date"] == date(2026, 1, 31)
    assert dates["reporting_year"] == 2026


def test_materialize_candidate_expands_esef_and_pdf() -> None:
    page = parse_malta_listing(LISTING_HTML)
    annual = next(
        notice
        for notice in page.notices
        if "Annual Report" in notice.title
    )
    connector = MaltaMseOamConnector(session=object())
    connector._notice_cache[annual.record_id] = annual
    candidate = connector._notice_candidate(annual)
    from models import Issuer

    issuer = Issuer(
        "Trident Estates plc",
        "MT0001670109",
        "TRI",
        "Malta Stock Exchange",
    )
    materialized = connector.materialize_candidate(candidate, issuer)
    formats = {item.metadata.get("file_format") for item in materialized}
    assert "pdf" in formats
    assert "zip" in formats
    assert "xhtml" in formats
    assert connector._details_visited == 0
