from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
        "nume_raportare=T%602025%6007%60SNP%60RFT_SNP_25_10_20251029_080100.pdf"
    )
    assert "`" not in url


def test_build_romania_listing_url() -> None:
    url = build_romania_listing_url(sort_column="refdate", page=2)
    assert (
        url
        == "https://oam.asfromania.ro/oam/loadedPDFReportsForPublic.jsp?"
        "xF4F59A60sortDir=desc&xF4F59A60sortColumn=refdate&"
        "xF4F59A60currentPage=2&xF4F59A60startLink=1"
    )


def _listing_html(
    rows: list[tuple[str, str, str, str]],
    *,
    total: int,
) -> str:
    rendered_rows = []
    for record_id, title, period_type, published_at in rows:
        rendered_rows.append(
            f"""
            <tr>
              <td>Issuer {record_id}</td><td>123</td><td>RO{record_id:0>10}</td>
              <td>Details</td><td>{title}</td><td>{period_type}</td>
              <td>2025</td><td>{published_at} 08:00</td><td></td>
              <td>Prima incarcare</td><td>ro</td>
              <td><a href="DownloadPDFFile.do?nume_raportare={record_id}.pdf">PDF</a></td>
            </tr>
            """
        )
    return f"<html><body><b>Total: {total}</b><table>{''.join(rendered_rows)}</table></body></html>"


class _FakeResponse:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text

    def raise_for_status(self) -> None:
        return None


class _FilteredListingSession:
    def __init__(self) -> None:
        annual_rows = [
            (f"annual-{index}", "Raport Financiar Anual", "anuala", "2026-04-30")
            for index in range(1, 12)
        ]
        annual_rows.extend(
            (
                ("calendar-1", "CALENDAR", "anuala", "2026-06-24"),
                (
                    "board-1",
                    "RAN502 - Raport consiliul de administratie (neconsolidat)",
                    "anuala",
                    "2026-07-10",
                ),
            )
        )
        self.pages = {
            "16": (
                _listing_html(annual_rows[:10], total=13),
                _listing_html(annual_rows[10:], total=13),
            ),
            "15": (
                _listing_html(
                    [("half-1", "Raport semestrial", "semestriala", "2026-02-15")],
                    total=1,
                ),
            ),
            "14": (
                _listing_html(
                    [("quarter-1", "Raport trimestrial", "trimestriala", "2026-05-15")],
                    total=1,
                ),
            ),
        }
        self.active_period = "16"
        self.post_payloads: list[dict[str, str]] = []
        self.get_urls: list[str] = []

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        payload = dict(kwargs["data"])
        self.post_payloads.append(payload)
        self.active_period = payload["xF4F59A60sqlPPERIODICITATE"]
        return _FakeResponse(self.pages[self.active_period][0])

    def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.get_urls.append(url)
        query = parse_qs(urlparse(url).query)
        page = int((query.get("xF4F59A60currentPage") or ["1"])[0])
        pages = self.pages[self.active_period]
        return _FakeResponse(pages[min(page - 1, len(pages) - 1)])


def test_filtered_search_covers_all_periodic_types_and_real_pagination() -> None:
    session = _FilteredListingSession()
    connector = RomaniaAsfOamConnector(
        session=session,
        rate_limit_seconds=0,
        max_pages=2,
    )

    candidates = connector.search_recent_documents_filtered(
        "Bucharest Stock Exchange",
        since=date(2025, 7, 13),
        until=date(2026, 7, 13),
        document_types=(
            "annual_financial_report",
            "half_year_financial_report",
            "quarterly_financial_report",
        ),
        limit=100,
    )

    assert [
        payload["xF4F59A60sqlPPERIODICITATE"]
        for payload in session.post_payloads
    ] == ["16", "15", "14"]
    assert all(
        payload["xF4F59A60sqlPS_START_DATE"] == "13/07/2025"
        and payload["xF4F59A60sqlPE_START_DATE"] == "13/07/2026"
        for payload in session.post_payloads
    )
    assert any(
        "xF4F59A60currentPage=2" in url
        and "xF4F59A60startLink=1" in url
        for url in session.get_urls
    )
    assert len(candidates) == 13
    assert not any(
        candidate.title == "CALENDAR" or "Raport consiliul" in candidate.title
        for candidate in candidates
    )
    assert {candidate.document_type for candidate in candidates} == {
        "annual_financial_report",
        "half_year_financial_report",
        "quarterly_financial_report",
    }


def test_filtered_search_rejects_an_incomplete_page_budget() -> None:
    connector = RomaniaAsfOamConnector(
        session=_FilteredListingSession(),
        rate_limit_seconds=0,
        max_pages=1,
    )

    with pytest.raises(RuntimeError, match="ROMANIA_ASF_OAM_MAX_PAGES=1"):
        connector.search_recent_documents_filtered(
            "Bucharest Stock Exchange",
            since=date(2025, 7, 13),
            until=date(2026, 7, 13),
            document_types=("annual_financial_report",),
            limit=100,
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
        "T%602025%6007%60SNP%60RFT_SNP_25_10_20251029_080100.pdf"
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
    ("title", "filename"),
    [
        ("CALENDAR", "CALENDAR01_BCROBL_26_06_20260624_120329.pdf"),
        (
            "RAN501 - Raport consiliul de administratie",
            "RAN501_FP_26_06_20260602_185616.pdf",
        ),
        (
            "RAN502 - Raport consiliul de administratie (neconsolidat)",
            "RAN502_ETFBRK_26_07_20260710_111011.pdf",
        ),
    ],
)
def test_annual_source_bucket_does_not_override_non_financial_title(
    title: str,
    filename: str,
) -> None:
    classification, _, _, negative = classify_romania_document(
        title,
        "anuala",
        filename,
    )

    assert classification == "other_regulatory_announcement"
    assert negative


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
