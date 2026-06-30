from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from connectors.base import ConnectorState
from connectors.portugal_cmvm_sdi import (
    PortugalCmvmSdiConnector,
    match_issuer_notice,
    parse_cmvm_sdi_html,
    parse_cmvm_sdi_json,
)
from models import Issuer

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://www.cmvm.pt/PInstitucional"
SDI_URL = (
    f"{BASE_URL}/Content?"
    "Input=BD77C8DEEB2702712300D99098915461C2A4F65FE4368A561E6AB83D1E580C4D"
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeResponse:
    def __init__(
        self,
        *,
        url: str,
        data: Any = None,
        text: str = "",
        status_code: int = 200,
    ) -> None:
        self.url = url
        self._data = data
        self.text = text
        self.status_code = status_code
        self.content = text.encode()
        self.headers = {"Content-Type": "application/json"}

    def json(self) -> Any:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self) -> None:
        self.posts: list[str] = []
        self.api_result = json.loads(fixture("portugal_cmvm_api_result.json"))

    def get(self, url: str, *, timeout: int, headers: Any = None) -> FakeResponse:
        if "moduleversioninfo" in url:
            return FakeResponse(url=url, data={"versionToken": "module-token"})
        return FakeResponse(url=url, text="<html><body>CMVM SDI</body></html>")

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        self.posts.append(url)
        if "PdfViewerInfPriv/DataAction" in url:
            return FakeResponse(
                url=url,
                data={
                    "data": {
                        "FileBase64": base64.b64encode(b"%PDF-fixture").decode(),
                        "FileName": "annual.pdf",
                    }
                },
            )
        if "InfPeriodicasContAnuais" in url:
            start = json["screenData"]["variables"]["StartIndex"]
            return FakeResponse(
                url=url,
                data=self.api_result if start == 0 else {"data": {"Count": 2}},
            )
        if "InfPeriodicasContSemestrais" in url:
            return FakeResponse(url=url, data={"data": {"Count": 0}})
        raise AssertionError(url)


def make_connector(session: FakeSession | None = None) -> PortugalCmvmSdiConnector:
    return PortugalCmvmSdiConnector(
        session=session or FakeSession(),  # type: ignore[arg-type]
        base_url=BASE_URL,
        sdi_url=SDI_URL,
        rate_limit_seconds=0,
        lookback_days=400,
        timeout=10,
    )


def test_json_and_html_parse_pdf_xhtml_zip() -> None:
    parsed_json = parse_cmvm_sdi_json(
        json.loads(fixture("portugal_cmvm_api_result.json")),
        base_url=BASE_URL,
        period="annual",
    )
    parsed_html = parse_cmvm_sdi_html(
        fixture("portugal_cmvm_listing.html"),
        base_url=BASE_URL,
    )

    assert parsed_json.total_count == 2
    assert {item.file_type for item in parsed_json.notices[1].files} == {
        "xhtml",
        "zip",
    }
    assert parsed_html.total_count == 2
    assert parsed_html.next_url == f"{BASE_URL}/?page=2"
    assert {item.file_type for item in parsed_html.notices[1].files} == {
        "xhtml",
        "zip",
    }

    parsed_detail = parse_cmvm_sdi_html(
        fixture("portugal_cmvm_detail.html"),
        base_url=BASE_URL,
    )
    assert {item.file_type for item in parsed_detail.notices[0].files} == {
        "xhtml",
        "zip",
    }


def test_matching_discovery_diagnostic_and_search() -> None:
    connector = make_connector()
    issuer = Issuer(
        "Altri SGPS SA",
        "PTALT0AE0002",
        "ALTR",
        "Euronext Lisbon",
    )
    parsed = parse_cmvm_sdi_json(
        json.loads(fixture("portugal_cmvm_api_result.json")),
        base_url=BASE_URL,
        period="annual",
    )

    assert match_issuer_notice(issuer, parsed.notices[0])
    discovery = connector.discover("relatório financeiro anual")
    resolution = connector.resolve_issuer(
        symbol="ALTR",
        name="Altri SGPS SA",
        isin="PTALT0AE0002",
    )
    candidates = connector.search_documents(issuer)
    diagnostic = connector.diagnose()

    assert discovery.notices
    assert resolution.found
    assert resolution.record_id == "1383791"
    assert {item.metadata["file_format"] for item in candidates} == {
        "pdf",
        "xhtml",
        "zip",
    }
    assert all(item.source == "cmvm_sdi" for item in candidates)
    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.checks["automatic_download"]
    assert {"pdf", "xhtml", "zip"}.issubset(diagnostic.formats)
