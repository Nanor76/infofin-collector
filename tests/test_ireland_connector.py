from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterator

from connectors.base import ConnectorState
from connectors.ireland_euronext_direct import (
    DEFAULT_DUBLIN_URL,
    IrelandEuronextDirectConnector,
    _financial_type,
    match_issuer_notice,
    parse_direct_html,
    parse_direct_json,
)
from db import Database
from download import DocumentDownloader
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
DIRECT_URL = "https://direct.euronext.com"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        text: str = "",
        data: Any = None,
        content: bytes | None = None,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self.url = url
        self.text = text
        self._data = data
        self.content = content if content is not None else text.encode()
        self.status_code = status_code
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(self.content)),
        }

    def json(self) -> Any:
        if self._data is not None:
            return self._data
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield self.content

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.downloads: list[str] = []
        self.oam = json.loads(fixture("ireland_direct_oam.json"))
        self.ris = json.loads(fixture("ireland_direct_ris.json"))

    def get(
        self,
        url: str,
        *,
        timeout: int,
        stream: bool = False,
    ) -> FakeResponse:
        if "/api/PublicAnnouncements/OAMDocument/" in url:
            self.downloads.append(url)
            filename = url.split("/OAMDocument/", 1)[1].split("?", 1)[0]
            suffix = Path(filename).suffix.casefold()
            fixture_name, content_type = {
                ".pdf": ("ireland_document.pdf", "application/octet-stream"),
                ".xhtml": (
                    "ireland_document.xhtml",
                    "application/octet-stream",
                ),
                ".xml": ("ireland_document.xml", "application/xml"),
                ".zip": ("ireland_document.zip", "application/zip"),
            }[suffix]
            return FakeResponse(
                url=url,
                content=fixture_bytes(fixture_name),
                content_type=content_type,
            )
        if url in {DIRECT_URL, f"{DIRECT_URL}/#/oamfiling", f"{DIRECT_URL}/#/rispublication"}:
            return FakeResponse(
                url=url,
                text=fixture("ireland_direct_listing.html"),
            )
        if url == DEFAULT_DUBLIN_URL:
            return FakeResponse(url=url, text="<html>Euronext Dublin</html>")
        raise AssertionError(f"Unexpected GET {url}")

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
    ) -> FakeResponse:
        self.posts.append((url, json))
        payload = self.oam if url.endswith("/OAMs") else self.ris
        if json["page"] > 0:
            payload = {
                "records": [],
                "totalItems": payload["totalItems"],
                "currentPage": json["page"],
                "numberOfPages": payload["numberOfPages"],
            }
        return FakeResponse(
            url=url,
            data=payload,
            content_type="application/json",
        )

    def close(self) -> None:
        return None


def make_connector(
    session: FakeSession | None = None,
) -> IrelandEuronextDirectConnector:
    return IrelandEuronextDirectConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        base_url=DIRECT_URL,
        dublin_url=DEFAULT_DUBLIN_URL,
        rate_limit_seconds=0,
        lookback_days=900,
        timeout=10,
        max_pages=3,
    )


def test_json_and_html_parse_supported_documents() -> None:
    parsed_json = parse_direct_json(
        json.loads(fixture("ireland_direct_oam.json")),
        direct_url=DIRECT_URL,
        source_kind="oam",
    )
    parsed_html = parse_direct_html(
        fixture("ireland_direct_listing.html"),
        direct_url=DIRECT_URL,
        source_kind="oam",
    )

    assert parsed_json.total_count == 54
    assert parsed_json.number_of_pages == 2
    assert {item.files[0].file_type for item in parsed_json.notices} == {
        "pdf",
        "xhtml",
        "xml",
        "zip",
    }
    assert parsed_html.total_count == 2
    assert parsed_html.next_url == f"{DIRECT_URL}/#/oamfiling?page=2"
    assert {item.files[0].file_type for item in parsed_html.notices} == {
        "pdf",
        "xhtml",
    }


def test_financial_type_prefers_title_nature_over_format_or_category() -> None:
    assert (
        _financial_type(
            "Half yearly financial reports and audit reports",
            "DIGI - Q1 2026 Financial Report",
            "digi-q1-2026.pdf",
        )
        == "quarterly_financial_report"
    )
    assert (
        _financial_type(
            "Annual financial and audit reports",
            "2025 Annual Report ESEF",
            "issuer-2025.xhtml",
        )
        == "annual_financial_report"
    )
    assert (
        _financial_type("", "Transaction in Own Shares", "notice.xml.xmlc.xml.html")
        is None
    )


def test_matching_prefers_isin_then_name_then_symbol() -> None:
    notice = parse_direct_json(
        json.loads(fixture("ireland_direct_oam.json")),
        direct_url=DIRECT_URL,
        source_kind="oam",
    ).notices[0]

    assert match_issuer_notice(
        Issuer(
            "Unrelated",
            "IE00BD1RP616",
            "XXX",
            "Euronext Dublin",
        ),
        notice,
    )
    assert match_issuer_notice(
        Issuer(
            "BANK OF IRELAND GP",
            "GB00BYYN4225",
            "BIRG",
            "Euronext Dublin",
        ),
        notice,
    )
    assert not match_issuer_notice(
        Issuer(
            "RYANAIR HOLDINGS PLC",
            "IE00BYTBXV33",
            "RYA",
            "Euronext Dublin",
        ),
        notice,
    )


def test_diagnose_discover_resolve_and_search() -> None:
    session = FakeSession()
    connector = make_connector(session)
    issuer = Issuer(
        "BANK OF IRELAND GP",
        "IE00BD1RP616",
        "BIRG",
        "Euronext Dublin",
    )

    diagnostic = connector.diagnose()
    discovery = connector.discover("annual report")
    resolution = connector.resolve_issuer(
        symbol="BIRG",
        name="BANK OF IRELAND GP",
        isin="IE00BD1RP616",
    )
    candidates = connector.search_documents(issuer)

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.checks["oam_api"]
    assert diagnostic.checks["ris_api"]
    assert diagnostic.checks["pagination"]
    assert diagnostic.checks["automatic_download"]
    assert {"pdf", "xhtml", "xml", "zip"}.issubset(diagnostic.formats)
    assert discovery.notices
    assert discovery.candidates[0].verified
    assert resolution.found
    assert resolution.record_id == "fixture-pdf-id"
    assert {item.metadata["file_format"] for item in candidates} == {
        "pdf",
        "xhtml",
        "xml",
        "zip",
    }
    assert all(item.source == "euronext_direct" for item in candidates)
    assert session.downloads


def test_ireland_recent_search_uses_global_oam_and_ris_queries() -> None:
    session = FakeSession()
    connector = make_connector(session)

    candidates = connector.search_recent_documents(
        "Euronext Dublin",
        since=date(2026, 2, 1),
        limit=10,
    )

    assert connector.supports_source_first
    assert candidates
    assert len(session.posts) == 2
    assert {url.rsplit("/", 1)[-1] for url, _ in session.posts} == {
        "OAMs",
        "RIS",
    }
    assert all(payload["companyName"] == "" for _, payload in session.posts)
    assert all(payload["firstLetter"] == "" for _, payload in session.posts)
    assert all(
        payload["startDate"] == "2026-02-01"
        for _, payload in session.posts
    )
    assert any(
        candidate.metadata["issuer_isins"] == ["IE00BD1RP616"]
        for candidate in candidates
    )


def test_downloads_all_ireland_fixture_formats(tmp_path: Path) -> None:
    database = Database(tmp_path / "ireland-formats.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [
            Issuer(
                "BANK OF IRELAND GP",
                "IE00BD1RP616",
                "BIRG",
                "Euronext Dublin",
            )
        ]
    )
    issuer = database.list_issuers("Euronext Dublin")[0]
    session = FakeSession()
    candidates = make_connector(session).search_documents(issuer)
    downloader = DocumentDownloader(
        database=database,
        session=session,  # type: ignore[arg-type]
        data_dir=tmp_path / "raw",
        timeout=10,
        max_download_bytes=1024 * 1024,
    )

    first_results = [
        downloader.download(issuer, candidate) for candidate in candidates
    ]
    second_results = [
        downloader.download(issuer, candidate) for candidate in candidates
    ]

    assert {result.path.suffix for result in first_results if result.path} == {
        ".pdf",
        ".xhtml",
        ".xml",
        ".zip",
    }
    assert all(result.status == "downloaded" for result in first_results)
    assert all(result.status == "duplicate" for result in second_results)
    assert all(
        result.path
        and result.path.parent
        == tmp_path / "raw" / "ireland" / "IE00BD1RP616"
        for result in first_results
    )
