from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from connectors.romania_asf_oam import (
    RomaniaAsfOamConnector,
    build_romania_download_url,
    build_romania_listing_url,
    classify_romania_document,
    extract_romania_date_info,
    parse_romania_listing,
)


FIXTURES = Path(__file__).parent / "fixtures"
LISTING_HTML = (FIXTURES / "romania_oam_listing.html").read_text(encoding="utf-8")


def test_build_romania_download_url_normalizes_participants_href() -> None:
    filename, url = build_romania_download_url(
        "participants/DownloadPDFFile.do?"
        "nume_raportare=T`2025`07`SNP`RFT_SNP_25_10_20251029_080100.pdf"
    )
    assert filename == "RFT_SNP_25_10_20251029_080100.pdf"
    assert (
        url
        == "https://oam.asfromania.ro/oam/DownloadPDFFile.do?"
        "nume_raportare=T`2025`07`SNP`RFT_SNP_25_10_20251029_080100.pdf"
    )


def test_build_romania_listing_url() -> None:
    url = build_romania_listing_url(sort_column="refdate", page=2)
    assert (
        url
        == "https://oam.asfromania.ro/oam/loadedPDFReportsForPublic.jsp?"
        "xF4F59A60sortDir=desc&xF4F59A60sortColumn=refdate&xF4F59A60page=2"
    )


def test_listing_parsing_finds_petrom_quarterly_report() -> None:
    page = parse_romania_listing(LISTING_HTML)
    quarterly = next(
        notice
        for notice in page.notices
        if notice.isin == "ROSNPPACNOR9"
    )
    assert quarterly.issuer_name == "OMV PETROM S.A."
    assert quarterly.period_type == "trimestriala"
    assert quarterly.files[0].filename == "RFT_SNP_25_10_20251029_080100.pdf"
    assert quarterly.files[0].file_format == "pdf"
    assert quarterly.published_at == date(2025, 10, 29)
    assert quarterly.files[0].download_url.endswith(
        "oam/DownloadPDFFile.do?nume_raportare="
        "T`2025`07`SNP`RFT_SNP_25_10_20251029_080100.pdf"
    )


def test_listing_parsing_skips_filter_and_header_rows() -> None:
    page = parse_romania_listing(LISTING_HTML)
    assert len(page.notices) == 2
    assert all("Filtreaza" not in notice.issuer_name for notice in page.notices)


@pytest.mark.parametrize(
    ("title", "period_type", "expected"),
    [
        (
            "Raport Financiar Trimestrial - RFT",
            "trimestriala",
            "quarterly_financial_report",
        ),
        (
            "Situatia semi-anuala a activelor",
            "semestriala",
            "half_year_financial_report",
        ),
        (
            "Emitenti - STFINANUAL01 - Situatii financiare anuale",
            "anuala",
            "annual_financial_report",
        ),
    ],
)
def test_periodic_classification(
    title: str,
    period_type: str,
    expected: str,
) -> None:
    assert classify_romania_document(title, period_type)[0] == expected


@pytest.mark.parametrize(
    "title",
    [
        "Plata dividend - RC17",
        "Convocarea adunarii generale a actionarilor",
        "Majorare capital social - RC12",
        "Emitent de obligatiuni - prospectus",
    ],
)
def test_non_periodic_classification(title: str) -> None:
    classification, _, _, negative = classify_romania_document(
        title,
        "exceptionala",
    )
    assert classification == "other_regulatory_announcement"
    assert negative


def test_publication_period_and_reporting_year_are_separate() -> None:
    quarterly = extract_romania_date_info(
        "Raport Financiar Trimestrial - RFT",
        "2025-10-29 08:01",
        "trimestriala",
        "Trim 3/2025",
        "RFT_SNP_25_10_20251029_080100.pdf",
    )
    assert quarterly["published_at"] == date(2025, 10, 29)
    assert quarterly["reporting_year"] == 2025
    assert quarterly["period_end_date"] == date(2025, 9, 30)


def test_materialize_candidate_without_extra_http() -> None:
    page = parse_romania_listing(LISTING_HTML)
    quarterly = next(
        notice for notice in page.notices if notice.isin == "ROSNPPACNOR9"
    )
    connector = RomaniaAsfOamConnector(session=object())
    connector._notice_cache[quarterly.record_id] = quarterly
    candidate = connector._notice_candidate(quarterly)
    from models import Issuer

    issuer = Issuer(
        "OMV PETROM S.A.",
        "ROSNPPACNOR9",
        "SNP",
        "Bucharest Stock Exchange",
    )
    materialized = connector.materialize_candidate(candidate, issuer)
    assert len(materialized) == 1
    assert "/oam/DownloadPDFFile.do" in materialized[0].url
    assert "/participants/" not in materialized[0].url
    assert materialized[0].document_type == "quarterly_financial_report"
    assert connector._details_visited == 0