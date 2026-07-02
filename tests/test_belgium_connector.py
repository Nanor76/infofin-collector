from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from connectors.base import ConnectorState
from connectors.belgium_fsma_stori import (
    ANNUAL_TYPE_ID,
    BelgiumFsmaStoriConnector,
    _financial_type,
    match_issuer_notice,
    parse_api_notice,
    parse_stori_detail_html,
    parse_stori_html,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_URL = "https://www.fsma.be/en/stori"
API_ORIGIN = "https://webapi.fsma.test"
API_ROOT = f"{API_ORIGIN}/api/v1/en/stori"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        text: str = "",
        data: Any = None,
        status_code: int = 200,
        content_type: str = "text/html; charset=utf-8",
        disposition: str = "",
    ) -> None:
        self.url = url
        self.text = text
        self._data = data
        self.status_code = status_code
        self.headers = {"Content-Type": content_type}
        if disposition:
            self.headers["Content-Disposition"] = disposition

    def json(self) -> Any:
        if self._data is not None:
            return self._data
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.downloads: list[str] = []
        self.api_result = json.loads(fixture("belgium_stori_api_result.json"))

    def get(
        self,
        url: str,
        *,
        timeout: int,
        stream: bool = False,
    ) -> FakeResponse:
        if url == PUBLIC_URL:
            html = fixture("belgium_stori_listing.html").replace(
                "<body>",
                (
                    '<body><script>window.drupalSettings = '
                    '{"vueToolsApi":"https:\\/\\/webapi.fsma.test"};'
                    "</script>"
                ),
            )
            return FakeResponse(url=url, text=html)
        if url.endswith("/document-type"):
            return FakeResponse(
                url=url,
                data=[
                    {
                        "documentTypeId": ANNUAL_TYPE_ID,
                        "localisedName": "|- Annual financial report",
                        "isGroup": False,
                    }
                ],
                content_type="application/json",
            )
        if url.endswith("/companies/abbreviated-name"):
            return FakeResponse(
                url=url,
                data=[
                    {
                        "companyId": "company-ab-inbev",
                        "abbreviation": "AB INBEV",
                    }
                ],
                content_type="application/json",
            )
        if "/download?" in url:
            self.downloads.append(url)
            filename = {
                "file-pdf": "AB-InBev-Annual-Report-2025.pdf",
                "file-xhtml": "report.xhtml",
                "file-zip": "report.zip",
            }[url.rsplit("=", 1)[-1]]
            content_type = {
                "file-pdf": "application/pdf",
                "file-xhtml": "application/xhtml+xml",
                "file-zip": "application/zip",
            }[url.rsplit("=", 1)[-1]]
            return FakeResponse(
                url=url,
                content_type=content_type,
                disposition=f'attachment; filename="{filename}"',
            )
        if "record-ab-inbev-2025" in url:
            return FakeResponse(
                url=url,
                text=fixture("belgium_stori_detail.html"),
            )
        if "page=2" in url:
            return FakeResponse(url=url, text="<html><body></body></html>")
        raise AssertionError(f"Unexpected GET {url}")

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: int,
    ) -> FakeResponse:
        assert url == f"{API_ROOT}/result"
        self.posts.append(json)
        return FakeResponse(
            url=url,
            data=self.api_result,
            content_type="application/json",
        )

    def close(self) -> None:
        return None


def make_connector(
    session: FakeSession | None = None,
) -> BelgiumFsmaStoriConnector:
    return BelgiumFsmaStoriConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        base_url=PUBLIC_URL,
        api_base_url=API_ORIGIN,
        rate_limit_seconds=0,
        lookback_days=900,
        timeout=10,
    )


def test_html_listing_and_detail_extract_pdf_xhtml_zip() -> None:
    listing = parse_stori_html(
        fixture("belgium_stori_listing.html"),
        base_url=PUBLIC_URL,
        api_root=API_ROOT,
    )
    files = parse_stori_detail_html(
        fixture("belgium_stori_detail.html"),
        detail_url=f"{PUBLIC_URL}/record-ab-inbev-2025",
        api_root=API_ROOT,
    )

    assert listing.total_count == 2
    assert listing.next_url == f"{PUBLIC_URL}?page=2"
    assert listing.notices[0].company_name == "AB INBEV"
    assert listing.notices[0].isin_codes == ("BE0974293251",)
    assert {item.file_type for item in files} == {"pdf", "xhtml", "zip"}
    assert all("fileDataId=" in item.download_url for item in files)


def test_api_parsing_classification_and_matching() -> None:
    data = json.loads(fixture("belgium_stori_api_result.json"))
    notice = parse_api_notice(
        data["storiResultItems"][0],
        public_url=PUBLIC_URL,
        api_root=API_ROOT,
    )
    issuer = Issuer(
        "Anheuser-Busch InBev",
        "BE0974293251",
        "ABI",
        "Euronext Brussels",
    )
    wrong = Issuer(
        "Barco",
        "BE0974362940",
        "BAR",
        "Euronext Brussels",
    )

    assert notice.published_date.isoformat() == "2026-02-12"
    assert {item.file_type for item in notice.files} == {
        "pdf",
        "xhtml",
        "zip",
    }
    assert match_issuer_notice(issuer, notice)
    assert not match_issuer_notice(wrong, notice)


def test_press_release_attachment_is_not_promoted_to_annual_report() -> None:
    assert (
        _financial_type(
            "Annual financial report",
            "PERSBERICHT - Co.Br.Ha. - jaarresultaten 2025 en resultaten Q1 2026.pdf",
            "PERSBERICHT - Co.Br.Ha. - jaarresultaten 2025 en resultaten Q1 2026.pdf",
        )
        == "other_regulatory_announcement"
    )


def test_diagnose_discover_resolve_and_search() -> None:
    session = FakeSession()
    connector = make_connector(session)

    diagnostic = connector.diagnose()
    discovery = connector.discover("annual financial report")
    resolution = connector.resolve_issuer(
        symbol="ABI",
        name="Anheuser-Busch InBev",
        isin="BE0974293251",
    )
    candidates = connector.search_documents(
        Issuer(
            "Anheuser-Busch InBev",
            "BE0974293251",
            "ABI",
            "Euronext Brussels",
        )
    )

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count == 2
    assert diagnostic.checks["pagination"]
    assert diagnostic.checks["automatic_download"]
    assert {"pdf", "xhtml", "zip"}.issubset(diagnostic.formats)
    assert discovery.notices
    assert discovery.candidates[0].state == ConnectorState.READY
    assert resolution.found
    assert resolution.fsma_record_id == "record-ab-inbev-2025"
    assert resolution.match_score == 100.0
    assert {candidate.metadata["file_format"] for candidate in candidates} == {
        "pdf",
        "xhtml",
        "zip",
    }
    assert all(candidate.source == "fsma_stori" for candidate in candidates)
    assert len(session.posts) >= 4
