from __future__ import annotations

import base64
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from classification import supported_extension
from connectors.base import Connector, ConnectorError, ConnectorState, DocumentCandidate
from connectors.base import EndpointAttempt
from models import Issuer

LOGGER = logging.getLogger(__name__)

ANNUAL_URL = (
    "https://www.cmvm.pt/PInstitucional/Content?"
    "Input=BD77C8DEEB2702712300D99098915461C2A4F65FE4368A561E6AB83D1E580C4D"
)
SEMIANNUAL_URL = (
    "https://www.cmvm.pt/PInstitucional/Content?"
    "Input=BDE789823CC76C2E3AC485A0044E62E6A6D6C2F998A849F517349592E7B4D25C"
)
ANONYMOUS_CSRF_TOKEN = "T6C+9iB49TLra4jEsMeSckDMNhQ="
VIEW_NAME = "MainFlow.Content"
LIST_FIELDS = (
    "ID",
    "Time",
    "DATA_FACT",
    "DSC_FACT",
    "PDF_FACT",
    "IsZip",
    "IsEN",
    "EncryptedURL",
)
FINANCIAL_TERMS = (
    "relatorio financeiro anual",
    "relatorio e contas anual",
    "relatorio e contas",
    "relatorio anual",
    "annual financial report",
    "annual report",
    "relatorio financeiro semestral",
    "relatorio semestral",
    "half-year financial report",
    "half year financial report",
    "contas anuais",
    "contas semestrais",
    "publicacao de contas anuais",
    "publicacao de contas semestrais",
    "esef",
    "xhtml",
    "zip",
    "pdf",
)


def _normalize(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    value = re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold())
    return re.sub(r"\s+", " ", value).strip()


def _normalized_issuer(value: str) -> str:
    text = _normalize(value)
    suffixes = (
        " sociedade anonima",
        " sgps sa",
        " sgps",
        " s a",
        " sa",
        " plc",
        " nv",
    )
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                changed = True
    return text


def _issuer_from_title(title: str) -> str:
    match = re.match(
        r"^\s*(.+?)\s+(?:informa(?:\s+sobre)?|informs?|comunica|announces)\b",
        title,
        flags=re.IGNORECASE,
    )
    if match:
        return match.group(1).strip(" ,-")
    return title.split(" - ", 1)[0].strip()


def _record_id_from_url(url: str) -> str | None:
    match = re.search(r"[?&]Input=([A-Fa-f0-9]+)", url)
    return match.group(1) if match else None


def _parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    for pattern in (r"(\d{2})[/-](\d{2})[/-](\d{4})",):
        match = re.search(pattern, text)
        if match:
            try:
                return date(
                    int(match.group(3)),
                    int(match.group(2)),
                    int(match.group(1)),
                )
            except ValueError:
                return None
    return None


@dataclass(frozen=True, slots=True)
class PortugalFile:
    file_id: str
    filename: str
    file_type: str
    download_url: str


@dataclass(frozen=True, slots=True)
class PortugalNotice:
    record_id: str
    published_date: date | None
    issuer_name: str
    title: str
    document_type: str
    detail_url: str
    files: tuple[PortugalFile, ...]
    isin_codes: tuple[str, ...] = ()
    period: str = "annual"


@dataclass(frozen=True, slots=True)
class ParsedPortugalPage:
    notices: tuple[PortugalNotice, ...]
    total_count: int
    next_url: str | None
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PortugalEndpointCandidate:
    url: str
    role: str
    format: str
    pagination: str
    fields: tuple[str, ...]
    verified: bool
    state: ConnectorState
    http_status: int | None
    records_count: int | None


@dataclass(frozen=True, slots=True)
class PortugalSourceDiscovery:
    source: str
    query: str
    candidates: tuple[PortugalEndpointCandidate, ...]
    notices: tuple[PortugalNotice, ...]
    attempts: tuple[EndpointAttempt, ...]


@dataclass(frozen=True, slots=True)
class PortugalSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    api_url: str
    http_status: int | None
    total_count: int
    detected_count: int
    fields: tuple[str, ...]
    formats: tuple[str, ...]
    example_notice: dict[str, Any] | None
    checks: dict[str, bool]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PortugalIssuerResolution:
    found: bool
    matched_name: str | None
    sdi_url: str | None
    detail_url: str | None
    record_id: str | None
    home_member_state: str | None
    match_score: float
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


class MemoryDownloadResponse:
    def __init__(
        self,
        content: bytes,
        *,
        content_type: str,
        filename: str,
    ) -> None:
        self.status_code = 200
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(content)),
            "Content-Disposition": f'attachment; filename="{filename}"',
        }
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Any:
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset : offset + chunk_size]

    def close(self) -> None:
        self._content = b""


def _file_entries(
    *,
    record_id: str,
    filename: str,
    detail_url: str,
    is_zip: bool,
) -> tuple[PortugalFile, ...]:
    if is_zip or "esefviewer" in detail_url.casefold():
        return (
            PortugalFile(
                f"{record_id}:xhtml",
                f"{filename or record_id}.xhtml",
                "xhtml",
                f"{detail_url}#format=xhtml",
            ),
            PortugalFile(
                f"{record_id}:zip",
                f"{filename or record_id}.zip",
                "zip",
                f"{detail_url}#format=zip",
            ),
        )
    return (
        PortugalFile(
            f"{record_id}:pdf",
            filename if filename.casefold().endswith(".pdf") else f"{filename}.pdf",
            "pdf",
            detail_url,
        ),
    )


def parse_api_notice(
    item: dict[str, Any],
    *,
    base_url: str,
    period: str,
) -> PortugalNotice | None:
    title = str(item.get("DSC_FACT") or item.get("title") or "").strip()
    detail_url = urljoin(
        base_url.rstrip("/") + "/",
        str(item.get("EncryptedURL") or item.get("url") or ""),
    )
    numeric_id = str(item.get("ID") or "").strip()
    record_id = numeric_id or _record_id_from_url(detail_url) or ""
    filename = str(item.get("PDF_FACT") or item.get("filename") or record_id)
    missing = [
        field
        for field, value in (
            ("ID", record_id),
            ("DATA_FACT", item.get("DATA_FACT")),
            ("DSC_FACT", title),
            ("EncryptedURL", detail_url),
        )
        if not value
    ]
    if missing:
        LOGGER.debug("CMVM notice champs manquants: %s", ", ".join(missing))
    if not title or not detail_url or not record_id:
        return None
    files = _file_entries(
        record_id=record_id,
        filename=filename,
        detail_url=detail_url,
        is_zip=bool(item.get("IsZip")),
    )
    document_type = (
        "half_year_financial_report"
        if period == "semiannual"
        else "annual_financial_report"
    )
    return PortugalNotice(
        record_id=record_id,
        published_date=_parse_date(item.get("DATA_FACT")),
        issuer_name=_issuer_from_title(title),
        title=title,
        document_type=document_type,
        detail_url=detail_url,
        files=files,
        period=period,
    )


def parse_cmvm_sdi_json(
    payload: dict[str, Any],
    *,
    base_url: str,
    period: str,
) -> ParsedPortugalPage:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    list_value = (
        data.get("InfPeriodicasContAnuaisLst2")
        or data.get("InfPeriodicasContSemestraisLstIn")
        or data.get("InfPeriodicasContSemestraisLst2")
        or data.get("RelatorioList")
        or {}
    )
    if isinstance(list_value, dict):
        rows = list_value.get("List") or []
    else:
        rows = list_value if isinstance(list_value, list) else []
    notices = tuple(
        notice
        for item in rows
        if isinstance(item, dict)
        for notice in [parse_api_notice(item, base_url=base_url, period=period)]
        if notice is not None
    )
    return ParsedPortugalPage(
        notices=notices,
        total_count=int(data.get("Count") or len(notices)),
        next_url=None,
        fields=LIST_FIELDS,
    )


def parse_cmvm_sdi_html(
    html: str,
    *,
    base_url: str,
    period: str = "annual",
) -> ParsedPortugalPage:
    soup = BeautifulSoup(html, "html.parser")
    notices: list[PortugalNotice] = []
    containers = soup.select(
        "[data-record-id], article, .gc-card-layout, .notice, tr"
    )
    for container in containers:
        link = container.select_one(
            'a[href*="PdfViewerInfPriv"], a[href*="EsefViewer"], '
            'a[href$=".pdf"], a[href$=".xhtml"], a[href$=".zip"]'
        )
        if link is None or not link.get("href"):
            continue
        detail_url = urljoin(base_url.rstrip("/") + "/", str(link["href"]))
        title_node = container.select_one(
            "[data-title], .title, .description, h2, h3, h4"
        )
        title = (
            str(title_node.get("data-title") or title_node.get_text(" ", strip=True))
            if title_node is not None
            else container.get_text(" ", strip=True)
        )
        date_node = container.select_one("time, [data-date], .date")
        date_value = ""
        if date_node is not None:
            date_value = str(
                date_node.get("datetime")
                or date_node.get("data-date")
                or date_node.get_text(" ", strip=True)
            )
        record_id = str(
            container.get("data-record-id")
            or _record_id_from_url(detail_url)
            or len(notices) + 1
        )
        filename = str(
            container.get("data-filename")
            or urlparse(detail_url).path.rsplit("/", 1)[-1]
            or record_id
        )
        is_zip = (
            "esefviewer" in detail_url.casefold()
            or container.select_one('a[href$=".zip"], [data-format="zip"]')
            is not None
        )
        files = _file_entries(
            record_id=record_id,
            filename=filename,
            detail_url=detail_url,
            is_zip=is_zip,
        )
        notices.append(
            PortugalNotice(
                record_id=record_id,
                published_date=_parse_date(date_value),
                issuer_name=str(
                    container.get("data-issuer") or _issuer_from_title(title)
                ),
                title=title,
                document_type=(
                    "half_year_financial_report"
                    if period == "semiannual"
                    else "annual_financial_report"
                ),
                detail_url=detail_url,
                files=files,
                isin_codes=tuple(
                    filter(
                        None,
                        re.split(
                            r"[\s,;]+",
                            str(container.get("data-isin") or ""),
                        ),
                    )
                ),
                period=period,
            )
        )
    next_link = soup.select_one(
        'a[rel="next"], a.next, a[aria-label*="next" i], '
        'a[aria-label*="seguinte" i]'
    )
    total_node = soup.select_one("[data-total-count], .total-count")
    total_count = len(notices)
    if total_node is not None:
        match = re.search(
            r"\d+",
            str(total_node.get("data-total-count") or total_node.get_text()),
        )
        if match:
            total_count = int(match.group())
    return ParsedPortugalPage(
        notices=tuple(notices),
        total_count=total_count,
        next_url=(
            urljoin(base_url.rstrip("/") + "/", str(next_link.get("href")))
            if next_link is not None and next_link.get("href")
            else None
        ),
        fields=LIST_FIELDS,
    )


def match_issuer_notice(issuer: Issuer, notice: PortugalNotice) -> bool:
    if notice.isin_codes and issuer.isin:
        if issuer.isin.upper() in {value.upper() for value in notice.isin_codes}:
            return True
    expected = _normalized_issuer(issuer.name)
    observed = _normalized_issuer(notice.issuer_name)
    if expected and observed:
        if expected == observed or expected in observed or observed in expected:
            return True
        if SequenceMatcher(None, expected, observed).ratio() >= 0.82:
            return True
    symbol = _normalize(issuer.symbol)
    return len(symbol) >= 3 and re.search(
        rf"\b{re.escape(symbol)}\b",
        _normalize(f"{notice.issuer_name} {notice.title}"),
    ) is not None


def _download_headers(page_url: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=UTF-8",
        "X-CSRFToken": ANONYMOUS_CSRF_TOKEN,
        "OutSystems-Locale": "pt-PT",
        "Referer": page_url,
        "Origin": f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}",
    }


def _module_version(
    session: requests.Session,
    *,
    base_url: str,
    timeout: int,
) -> str:
    response = session.get(
        f"{base_url.rstrip('/')}/moduleservices/moduleversioninfo?{int(time.time() * 1000)}",
        headers={"Accept": "application/json", "OutSystems-client-env": "browser"},
        timeout=timeout,
    )
    response.raise_for_status()
    token = response.json().get("versionToken")
    if not token:
        raise ConnectorError("Version du module CMVM absente")
    return str(token)


def fetch_cmvm_download(
    session: requests.Session,
    candidate: DocumentCandidate,
    *,
    timeout: int,
) -> MemoryDownloadResponse:
    kind = str(candidate.metadata.get("cmvm_download_kind") or "")
    detail_url = urldefrag(
        str(candidate.metadata.get("detail_url") or candidate.url)
    ).url
    base_url = str(
        candidate.metadata.get("cmvm_base_url")
        or f"{urlparse(detail_url).scheme}://{urlparse(detail_url).netloc}/PInstitucional"
    ).rstrip("/")
    session.get(detail_url, timeout=timeout).raise_for_status()
    module_version = _module_version(
        session,
        base_url=base_url,
        timeout=timeout,
    )
    encrypted_input = _record_id_from_url(detail_url)
    if not encrypted_input:
        raise ConnectorError(f"Paramètre CMVM Input absent: {detail_url}")

    if kind == "pdf":
        endpoint = (
            f"{base_url}/screenservices/PInstitucional/MainFlow/"
            "PdfViewerInfPriv/DataActionFetchDecriptInput"
        )
        payload = {
            "versionInfo": {
                "moduleVersion": module_version,
                "apiVersion": "4XmmQW9Dv_erLDTa6ay3Mw",
            },
            "viewName": "MainFlow.PdfViewerInfPriv",
            "screenData": {
                "variables": {
                    "Input": encrypted_input,
                    "_inputInDataFetchStatus": 1,
                }
            },
        }
        response = session.post(
            endpoint,
            json=payload,
            headers=_download_headers(detail_url),
            timeout=max(timeout, 120),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        encoded = data.get("FileBase64")
        if not encoded:
            raise ConnectorError(
                f"PDF CMVM indisponible: {body.get('exception') or 'réponse vide'}"
            )
        filename = str(data.get("FileName") or candidate.metadata.get("filename") or "cmvm.pdf")
        return MemoryDownloadResponse(
            base64.b64decode(encoded),
            content_type="application/pdf",
            filename=filename,
        )

    if kind == "xhtml":
        endpoint = (
            f"{base_url}/screenservices/PInstitucional/MainFlow/"
            "EsefViewer/DataActionDataActionDecode"
        )
        payload = {
            "versionInfo": {
                "moduleVersion": module_version,
                "apiVersion": "NM1pXP9gBDQaL_uhITOoOg",
            },
            "viewName": "MainFlow.EsefViewer",
            "screenData": {
                "variables": {
                    "IsLoading": True,
                    "Input": encrypted_input,
                    "_inputInDataFetchStatus": 1,
                }
            },
            "clientVariables": {
                "HasLocaleChanged": False,
                "PortalId": "1",
                "Language": "",
                "Url_list_History": "",
            },
        }
        response = session.post(
            endpoint,
            json=payload,
            headers=_download_headers(detail_url),
            timeout=max(timeout, 180),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        content = str(data.get("HTML") or "").encode("utf-8")
        if not content:
            encoded = data.get("Base64")
            content = base64.b64decode(encoded) if encoded else b""
        if not content:
            raise ConnectorError(
                f"XHTML CMVM indisponible: {body.get('exception') or 'réponse vide'}"
            )
        return MemoryDownloadResponse(
            content,
            content_type="application/xhtml+xml",
            filename=str(candidate.metadata.get("filename") or "cmvm.xhtml"),
        )

    if kind == "zip":
        period = str(candidate.metadata.get("period") or "annual")
        static_page = (
            "SDI_Emitentes_RelCont_Semestrais"
            if period == "semiannual"
            else "SDI_Emitentes_RelCont_Anuais"
        )
        endpoint = (
            f"{base_url}/screenservices/PInstitucional/SDI_StaticPages/"
            f"{static_page}/ActionInfoDiariaPeriodoDownloadZip"
        )
        payload = {
            "versionInfo": {
                "moduleVersion": module_version,
                "apiVersion": "Dw9ttqwv05VNq9eMeicySQ",
            },
            "viewName": VIEW_NAME,
            "inputParameters": {
                "LocalLanguageId": 1,
                "Language": {"PortugueseId": 0, "EnglishId": 2},
                "IsPT": not bool(candidate.metadata.get("is_english")),
                "Id": int(candidate.metadata["cmvm_numeric_id"]),
                "Tab": "Z",
            },
        }
        response = session.post(
            endpoint,
            json=payload,
            headers=_download_headers(detail_url),
            timeout=max(timeout, 240),
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data") or {}
        encoded = data.get("Base64")
        if not encoded:
            raise ConnectorError(
                f"ZIP CMVM indisponible: {body.get('exception') or 'réponse vide'}"
            )
        file_info = data.get("DownloadFich") or {}
        filename = (
            f"{file_info.get('NomFich') or candidate.metadata.get('filename') or 'cmvm'}"
            f"{file_info.get('TIP_DOC') or '.zip'}"
        )
        return MemoryDownloadResponse(
            base64.b64decode(encoded),
            content_type="application/zip",
            filename=filename,
        )
    raise ConnectorError(f"Type de téléchargement CMVM inconnu: {kind!r}")


class PortugalCmvmSdiConnector(Connector):
    market = "Euronext Lisbon"
    source_name = "cmvm_sdi"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        base_url: str,
        sdi_url: str = ANNUAL_URL,
        market: str = "Euronext Lisbon",
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 900,
        timeout: int = 30,
        max_pages: int = 50,
    ) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.sdi_url = sdi_url
        self.semiannual_url = SEMIANNUAL_URL
        self.market = market
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.max_pages = max(1, max_pages)
        self.state = ConnectorState.READY
        self.last_error = None
        self._last_request_at = 0.0
        self._notice_cache: tuple[PortugalNotice, ...] | None = None
        self._attempts: list[EndpointAttempt] = []
        headers = getattr(self.session, "headers", None)
        if headers is not None:
            current_user_agent = str(headers.get("User-Agent") or "")
            if "mozilla/" not in current_user_agent.casefold():
                headers["User-Agent"] = (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/136.0.0.0 Safari/537.36 InfoFin/1.0"
                )
            headers["Accept-Language"] = "pt-PT,pt;q=0.9,en;q=0.8"

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _get(self, url: str) -> requests.Response:
        self._wait()
        response = self.session.get(url, timeout=self.timeout)
        self._last_request_at = time.monotonic()
        return response

    def _post(self, url: str, payload: dict[str, Any], referer: str) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(2):
            self._wait()
            try:
                response = self.session.post(
                    url,
                    json=payload,
                    headers=_download_headers(referer),
                    timeout=max(self.timeout, 60),
                )
                self._last_request_at = time.monotonic()
                if response.status_code >= 500 and attempt == 0:
                    time.sleep(0.8)
                    continue
                return response
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.8)
        raise ConnectorError(str(last_error or "POST CMVM impossible"))

    def _module_version(self) -> str:
        self._wait()
        token = _module_version(
            self.session,
            base_url=self.base_url,
            timeout=self.timeout,
        )
        self._last_request_at = time.monotonic()
        return token

    @staticmethod
    def _empty_list() -> dict[str, Any]:
        return {
            "List": [],
            "EmptyListItem": {
                "ID": 0,
                "Time": "",
                "DATA_FACT": "1900-01-01",
                "DSC_FACT": "",
                "PDF_FACT": "",
                "IsZip": False,
                "IsEN": False,
                "EncryptedURL": "",
            },
        }

    def _listing_payload(
        self,
        *,
        module_version: str,
        period: str,
        start_index: int,
        year: int,
        since: date,
        until: date,
    ) -> tuple[str, dict[str, Any], str]:
        if period == "semiannual":
            block = "InfPeriodicasContSemestrais_NewFileLink"
            api_version = "p8wbcQXW0EC65GLV_nyBTA"
            variables = {
                "StartIndex": start_index,
                "MaxRecords": 30,
                "InfPeriodicasContSemestraisLst": self._empty_list(),
                "IsLoading": False,
                "Ano": str(year),
                "_anoInDataFetchStatus": 1,
                "LanguageId": "1",
                "_languageIdInDataFetchStatus": 1,
                "EntitiesList": "",
                "_entitiesListInDataFetchStatus": 1,
                "StartDate": since.isoformat(),
                "_startDateInDataFetchStatus": 1,
                "EndDate": until.isoformat(),
                "_endDateInDataFetchStatus": 1,
                "GetLanguage": {
                    "Language": {"PortugueseId": 0, "EnglishId": 2},
                    "DataFetchStatus": 1,
                },
            }
            referer = self.semiannual_url
        else:
            block = "InfPeriodicasContAnuais_NewFileLink"
            api_version = "eM65FMlMFcusoKBjVBgcyQ"
            variables = {
                "StartIndex": start_index,
                "MaxRecord": 30,
                "InfPeriodicasContAnuaisLst": self._empty_list(),
                "IsLoading": True,
                "LanguageId": "1",
                "_languageIdInDataFetchStatus": 1,
                "EntitiesList": "",
                "_entitiesListInDataFetchStatus": 1,
                "StartDate": since.isoformat(),
                "_startDateInDataFetchStatus": 1,
                "EndDate": until.isoformat(),
                "_endDateInDataFetchStatus": 1,
                "GetLanguage": {
                    "Language": {"PortugueseId": 0, "EnglishId": 2},
                    "DataFetchStatus": 1,
                },
            }
            referer = self.sdi_url
        endpoint = (
            f"{self.base_url}/screenservices/CMVM_SDI_Emitentes_CW/"
            f"Relatorios/{block}/DataActionGetData"
        )
        return endpoint, {
            "versionInfo": {
                "moduleVersion": module_version,
                "apiVersion": api_version,
            },
            "viewName": VIEW_NAME,
            "screenData": {"variables": variables},
        }, referer

    def _load_period(
        self,
        *,
        period: str,
        module_version: str,
        since: date,
        until: date,
    ) -> list[PortugalNotice]:
        notices: list[PortugalNotice] = []
        # Both public SDI actions currently return their complete result set
        # regardless of the year input. Query once and enforce the date window
        # locally to avoid repeated requests for identical pages.
        years = (until.year,)
        for year in years:
            start_index = 0
            for _ in range(self.max_pages):
                endpoint, payload, referer = self._listing_payload(
                    module_version=module_version,
                    period=period,
                    start_index=start_index,
                    year=year,
                    since=since,
                    until=until,
                )
                response = self._post(endpoint, payload, referer)
                success = response.status_code == 200
                try:
                    body = response.json()
                except ValueError:
                    body = {}
                parsed = parse_cmvm_sdi_json(
                    body,
                    base_url=self.base_url,
                    period=period,
                )
                exception = body.get("exception") if isinstance(body, dict) else None
                error = (
                    str(exception.get("message"))
                    if isinstance(exception, dict)
                    else None
                )
                self._attempts.append(
                    EndpointAttempt(
                        name=f"cmvm_{period}_json",
                        base_url=self.base_url,
                        dataset=period,
                        endpoint=endpoint,
                        method="POST",
                        http_status=response.status_code,
                        success=success and not error,
                        total_count=parsed.total_count,
                        response_excerpt=(
                            parsed.notices[0].title[:180]
                            if parsed.notices
                            else None
                        ),
                        error=error,
                    )
                )
                if error:
                    raise ConnectorError(error)
                notices.extend(parsed.notices)
                start_index += len(parsed.notices)
                if not parsed.notices or start_index >= parsed.total_count:
                    break
        return notices

    def _load_html_fallback(self) -> list[PortugalNotice]:
        notices: list[PortugalNotice] = []
        for period, url in (
            ("annual", self.sdi_url),
            ("semiannual", self.semiannual_url),
        ):
            response = self._get(url)
            parsed = parse_cmvm_sdi_html(
                response.text,
                base_url=self.base_url,
                period=period,
            )
            self._attempts.append(
                EndpointAttempt(
                    name=f"cmvm_{period}_html",
                    base_url=self.base_url,
                    dataset=period,
                    endpoint=url,
                    method="GET",
                    http_status=response.status_code,
                    success=response.status_code == 200,
                    total_count=parsed.total_count,
                    response_excerpt=(
                        parsed.notices[0].title[:180]
                        if parsed.notices
                        else response.text[:180]
                    ),
                )
            )
            notices.extend(parsed.notices)
        return notices

    def _load_notices(self) -> tuple[PortugalNotice, ...]:
        if self._notice_cache is not None:
            return self._notice_cache
        today = date.today()
        since = today - timedelta(days=self.lookback_days)
        try:
            portal = self._get(self.base_url + "/")
            portal.raise_for_status()
            module_version = self._module_version()
            notices = self._load_period(
                period="annual",
                module_version=module_version,
                since=since,
                until=today,
            )
            notices.extend(
                self._load_period(
                    period="semiannual",
                    module_version=module_version,
                    since=since,
                    until=today,
                )
            )
        except Exception as exc:
            LOGGER.warning("API JSON CMVM indisponible, repli HTML: %s", exc)
            try:
                notices = self._load_html_fallback()
            except Exception as fallback_exc:
                self.state = ConnectorState.UNAVAILABLE
                self.last_error = str(fallback_exc)
                raise ConnectorError(str(fallback_exc)) from fallback_exc
            self.state = ConnectorState.DEGRADED
            self.last_error = f"API JSON indisponible, repli HTML: {exc}"
        unique: dict[tuple[str, str], PortugalNotice] = {}
        for notice in notices:
            if notice.published_date and notice.published_date < since:
                continue
            unique[(notice.record_id, notice.detail_url)] = notice
        self._notice_cache = tuple(
            sorted(
                unique.values(),
                key=lambda item: item.published_date or date.min,
                reverse=True,
            )
        )
        if not self._notice_cache and self.state == ConnectorState.READY:
            self.state = ConnectorState.DEGRADED
            self.last_error = "SDI accessible mais aucune notice financière parsée"
        return self._notice_cache

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        for notice in self._load_notices():
            if not match_issuer_notice(issuer, notice):
                continue
            for item in notice.files:
                document_type = (
                    "esef"
                    if item.file_type in {"xhtml", "zip"}
                    else notice.document_type
                )
                candidates.append(
                    DocumentCandidate(
                        title=notice.title,
                        url=item.download_url,
                        published_date=notice.published_date,
                        document_type=document_type,
                        source=self.source_name,
                        source_document_id=item.file_id,
                        metadata={
                            "cmvm_record_id": notice.record_id,
                            "cmvm_numeric_id": notice.record_id,
                            "cmvm_sdi_url": self.sdi_url,
                            "detail_url": notice.detail_url,
                            "home_member_state": "Portugal",
                            "issuer_isins": list(notice.isin_codes),
                            "issuer_name": notice.issuer_name,
                            "issuer_symbol": None,
                            "file_format": item.file_type,
                            "filename": item.filename,
                            "cmvm_download_kind": item.file_type,
                            "cmvm_base_url": self.base_url,
                            "period": notice.period,
                        },
                    )
                )
        return candidates

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        cutoff = since or (date.today() - timedelta(days=7))
        candidate_limit = max(1, limit or 1000)
        notices = [
            notice
            for notice in self._load_notices()
            if (
                notice.published_date is None
                or notice.published_date >= cutoff
            )
        ]
        self._scanned_notices = len(notices)
        candidates: list[DocumentCandidate] = []
        for notice in notices:
            for item in notice.files:
                candidates.append(
                    DocumentCandidate(
                        title=notice.title,
                        url=item.download_url,
                        published_date=notice.published_date,
                        document_type=(
                            "esef"
                            if item.file_type in {"xhtml", "zip"}
                            else notice.document_type
                        ),
                        source=self.source_name,
                        source_document_id=item.file_id,
                        metadata={
                            "cmvm_record_id": notice.record_id,
                            "cmvm_numeric_id": notice.record_id,
                            "cmvm_sdi_url": self.sdi_url,
                            "detail_url": notice.detail_url,
                            "home_member_state": "Portugal",
                            "issuer_isins": list(notice.isin_codes),
                            "issuer_name": notice.issuer_name,
                            "issuer_symbol": None,
                            "file_format": item.file_type,
                            "filename": item.filename,
                            "cmvm_download_kind": item.file_type,
                            "cmvm_base_url": self.base_url,
                            "period": notice.period,
                        },
                    )
                )
                if len(candidates) >= candidate_limit:
                    return candidates
        return candidates

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        pages = min(
            self.max_pages,
            max(1, ((limit or 1000) + 49) // 50),
        )
        return pages * 2 + 2

    def resolve_issuer(
        self,
        *,
        symbol: str,
        name: str,
        isin: str | None = None,
    ) -> PortugalIssuerResolution:
        issuer = Issuer(
            name=name,
            isin=isin or "",
            symbol=symbol,
            market=self.market,
        )
        best: tuple[float, PortugalNotice] | None = None
        for notice in self._load_notices():
            score = SequenceMatcher(
                None,
                _normalized_issuer(name),
                _normalized_issuer(notice.issuer_name),
            ).ratio() * 100
            if match_issuer_notice(issuer, notice):
                score = max(score, 90.0)
            if best is None or score > best[0]:
                best = (score, notice)
        found = best is not None and best[0] >= 75.0
        notice = best[1] if found and best else None
        return PortugalIssuerResolution(
            found=found,
            matched_name=notice.issuer_name if notice else None,
            sdi_url=self.sdi_url if notice else None,
            detail_url=notice.detail_url if notice else None,
            record_id=notice.record_id if notice else None,
            home_member_state="Portugal" if notice else None,
            match_score=best[0] if best else 0.0,
            attempts=tuple(self._attempts),
            error=None if found else "Aucune notice CMVM correspondante",
        )

    def discover(self, query: str) -> PortugalSourceDiscovery:
        notices = self._load_notices()
        normalized_query = _normalize(query)
        selected = tuple(
            notice
            for notice in notices
            if normalized_query in _normalize(notice.title)
        )
        if not selected and any(
            term in normalized_query or normalized_query in term
            for term in FINANCIAL_TERMS
        ):
            if any(
                term in normalized_query
                for term in ("semestral", "half year", "half-year")
            ):
                selected = tuple(
                    notice
                    for notice in notices
                    if notice.period == "semiannual"
                )
            elif any(
                term in normalized_query
                for term in ("anual", "annual", "contas anuais")
            ):
                selected = tuple(
                    notice for notice in notices if notice.period == "annual"
                )
            else:
                selected = notices
        selected = selected[:50]
        endpoint = next(
            (
                attempt.endpoint
                for attempt in self._attempts
                if attempt.name == "cmvm_annual_json"
            ),
            self.sdi_url,
        )
        return PortugalSourceDiscovery(
            source=self.source_name,
            query=query,
            candidates=(
                PortugalEndpointCandidate(
                    url=endpoint,
                    role="CMVM SDI annual and half-year regulated information",
                    format="json",
                    pagination="StartIndex + MaxRecord(s), 30 records",
                    fields=LIST_FIELDS,
                    verified=bool(notices),
                    state=self.state,
                    http_status=200 if notices else None,
                    records_count=len(notices),
                ),
                PortugalEndpointCandidate(
                    url=self.sdi_url,
                    role="official rendered HTML fallback",
                    format="html",
                    pagination="public SDI infinite scroll / next page",
                    fields=LIST_FIELDS,
                    verified=True,
                    state=ConnectorState.DEGRADED,
                    http_status=200,
                    records_count=None,
                ),
            ),
            notices=selected,
            attempts=tuple(self._attempts),
        )

    def diagnose(self) -> PortugalSourceDiagnostic:
        portal_status: int | None = None
        sdi_status: int | None = None
        download_ok = False
        try:
            portal_response = self._get(self.base_url + "/")
            portal_status = portal_response.status_code
            sdi_response = self._get(self.sdi_url)
            sdi_status = sdi_response.status_code
            notices = self._load_notices()
            first_pdf = next(
                (
                    (notice, item)
                    for notice in notices
                    for item in notice.files
                    if item.file_type == "pdf"
                ),
                None,
            )
            if first_pdf:
                notice, item = first_pdf
                probe = fetch_cmvm_download(
                    self.session,
                    DocumentCandidate(
                        title=notice.title,
                        url=item.download_url,
                        published_date=notice.published_date,
                        document_type=notice.document_type,
                        source=self.source_name,
                        metadata={
                            "cmvm_download_kind": "pdf",
                            "detail_url": notice.detail_url,
                            "cmvm_base_url": self.base_url,
                            "filename": item.filename,
                        },
                    ),
                    timeout=self.timeout,
                )
                download_ok = int(probe.headers["Content-Length"]) > 0
                probe.close()
        except Exception as exc:
            if portal_status is None or portal_status >= 400:
                state = ConnectorState.UNAVAILABLE
            else:
                state = ConnectorState.DEGRADED
            self.state = state
            self.last_error = str(exc)
            notices = self._notice_cache or ()
        formats = tuple(
            sorted(
                {
                    item.file_type
                    for notice in notices
                    for item in notice.files
                }
            )
        )
        if notices and download_ok:
            state = ConnectorState.READY
        elif portal_status == 200 or sdi_status == 200:
            state = ConnectorState.DEGRADED
        else:
            state = ConnectorState.UNAVAILABLE
        self.state = state
        first = notices[0] if notices else None
        return PortugalSourceDiagnostic(
            source=self.source_name,
            state=state,
            called_url=self.sdi_url,
            api_url=next(
                (
                    attempt.endpoint
                    for attempt in self._attempts
                    if attempt.name == "cmvm_annual_json"
                ),
                self.sdi_url,
            ),
            http_status=sdi_status,
            total_count=len(notices),
            detected_count=len(notices),
            fields=LIST_FIELDS,
            formats=formats,
            example_notice=(
                {
                    "record_id": first.record_id,
                    "date": (
                        first.published_date.isoformat()
                        if first.published_date
                        else None
                    ),
                    "issuer": first.issuer_name,
                    "title": first.title,
                    "detail_url": first.detail_url,
                    "files": [
                        {
                            "format": item.file_type,
                            "url": item.download_url,
                        }
                        for item in first.files
                    ],
                }
                if first
                else None
            ),
            checks={
                "portal_accessible": portal_status == 200,
                "sdi_accessible": sdi_status == 200,
                "public_listing": bool(notices),
                "pagination": any(
                    attempt.total_count is not None
                    for attempt in self._attempts
                ),
                "real_notices": bool(notices),
                "pdf_xhtml_zip_detection": bool(formats),
                "automatic_download": download_ok,
            },
            attempts=tuple(self._attempts),
            error=None if state == ConnectorState.READY else self.last_error,
        )
