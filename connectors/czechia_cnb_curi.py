from __future__ import annotations

import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

import requests

from connectors.base import (
    Connector,
    ConnectorState,
    DocumentCandidate,
    EndpointAttempt,
)
from models import Issuer

LOGGER = logging.getLogger(__name__)

DEFAULT_CNB_START_URL = (
    "https://oam.cnb.cz/sipresextdad/SIPRESWEB.WEB21.START_INPUT_OAM?p_lang=en"
)
DEFAULT_CNB_SEARCH_URL = (
    "https://oam.cnb.cz/xmlpserver/OAM_CNB_CZ/R1_RES.xdo?par_lang=en_US&_xf=xml"
)
DEFAULT_DOWNLOAD_BASE_URL = (
    "https://oam.cnb.cz/sipresextdad/SIPRESWEB.BIP00.DWNL_FILE"
)

# Supported extensions for periodic reports
SUPPORTED_EXTENSIONS = {".pdf", ".zip", ".xhtml", ".xml", ".xbri"}

def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()

def _parse_czech_date(raw_date_str: str) -> date | None:
    if not raw_date_str:
        return None
    raw = raw_date_str.strip()
    # E.g. "31.05.2026 18:47:38" or "31.05.2026"
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None

def _extract_isins(text: str) -> list[str]:
    pattern = r"(?:[^A-Z0-9]|^)([A-Z]{2}[A-Z0-9]{9}\d)(?:[^A-Z0-9]|$)"
    return sorted(list(set(re.findall(pattern, text.upper()))))

def _extract_czechia_date_info(
    *,
    title: str,
    filename: str,
    published_date: date | None,
    is_annual: bool,
) -> dict[str, Any]:
    text = f"{title} {filename}"
    
    # 1. Look for explicit dates like YYYY-MM-DD or DD-MM-YYYY or DD.MM.YYYY
    date_patterns = [
        r"(?:[^0-9]|^)(20\d{2}-\d{2}-\d{2})(?:[^0-9]|$)",
        r"(?:[^0-9]|^)(\d{2}-\d{2}-20\d{2})(?:[^0-9]|$)",
        r"(?:[^0-9]|^)(\d{2}\.\d{2}\.20\d{2})(?:[^0-9]|$)",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, text)
        if match:
            raw_date = match.group(1).replace("-", ".").replace(" ", ".")
            parsed = _parse_czech_date(raw_date)
            if parsed and published_date and parsed <= published_date + timedelta(days=90):
                return {
                    "period_end_date": parsed,
                    "reporting_year": parsed.year,
                    "confidence": "high",
                    "reason": "Found explicit period end date in filename/title matching published timeframe",
                }

    # 2. Look for year patterns (20xx)
    years = [int(y) for y in re.findall(r"(?:[^0-9]|^)(20\d{2})(?:[^0-9]|$)", text)]
    if years:
        # Check that year is reasonable (between 2000 and current year + 1)
        current_year = date.today().year
        valid_years = [y for y in years if 2000 <= y <= current_year + 1]
        if valid_years:
            reporting_year = max(valid_years)
            if is_annual:
                period_end_date = date(reporting_year, 12, 31)
            else:
                period_end_date = date(reporting_year, 6, 30)
            
            return {
                "period_end_date": period_end_date,
                "reporting_year": reporting_year,
                "confidence": "high",
                "reason": f"Extrapolated period end date ({period_end_date.isoformat()}) from fiscal year {reporting_year} found in title/filename",
            }
            
    # 3. Fallback to published date year if nothing else is found
    if published_date:
        reporting_year = published_date.year
        if published_date.month <= 6:
            # Likely previous year's report
            reporting_year -= 1
        return {
            "period_end_date": None,
            "reporting_year": reporting_year,
            "confidence": "low",
            "reason": f"Fallback to estimated reporting year {reporting_year} based on publication date",
        }

    return {
        "period_end_date": None,
        "reporting_year": None,
        "confidence": "low",
        "reason": "Could not determine reporting period or year from metadata",
    }

@dataclass(frozen=True, slots=True)
class CzechiaNotice:
    form_id: str
    published_raw: str
    published_at: date | None
    report_type: str
    issuer_name: str
    ico: str
    lei: str | None
    files: tuple[CzechiaFile, ...] = ()

@dataclass(frozen=True, slots=True)
class CzechiaFile:
    filename: str
    file_id: str
    file_type: str
    language: str | None

@dataclass(frozen=True, slots=True)
class CzechiaSourceDiagnostic:
    source: str
    state: ConnectorState
    called_url: str
    http_status: int | None
    method_used: str
    total_count: int
    detected_count: int
    fields: tuple[str, ...]
    categories: dict[str, int]
    formats: tuple[str, ...]
    example_notice: dict[str, Any] | None
    http_calls: int
    request_efficiency: float
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None

@dataclass(frozen=True, slots=True)
class CzechiaSourceDiscovery:
    source: str
    query: str
    notices: tuple[CzechiaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None

@dataclass(frozen=True, slots=True)
class CzechiaIssuerResolution:
    found: bool
    matched_name: str | None = None
    czechia_cnb_curi_name: str | None = None
    czechia_cnb_curi_issuer_url: str | None = None
    czechia_cnb_curi_detail_url: str | None = None
    czechia_cnb_curi_record_id: str | None = None
    home_member_state: str | None = "Czechia"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None

class CzechiaCnbCuriConnector(Connector):
    market = "Prague Stock Exchange"
    source_name = "czechia_cnb_curi"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        start_url: str = DEFAULT_CNB_START_URL,
        search_url: str = DEFAULT_CNB_SEARCH_URL,
        download_base_url: str = DEFAULT_DOWNLOAD_BASE_URL,
        rate_limit_seconds: float = 0.5,
        lookback_days: int = 30,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.start_url = start_url
        self.search_url = search_url
        self.download_base_url = download_base_url
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        
        # Caches for feed response
        self._feed_cache: tuple[CzechiaNotice, ...] | None = None
        self._scanned_notices = 0
        self._details_visited = 0
        self._cache_hits = 0

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.rate_limit_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _request(
        self,
        method: str,
        url: str,
        data: dict[str, str] | str | None = None,
        headers: dict[str, str] | None = None,
        name: str = "CNB OAM API",
    ) -> requests.Response:
        self._wait()
        self._last_request_at = time.monotonic()
        response = None
        try:
            if method.upper() == "GET":
                response = self.session.get(
                    url,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            else:
                response = self.session.post(
                    url,
                    data=data,
                    headers=headers,
                    timeout=self.timeout,
                    verify=self.verify_ssl,
                )
            response.raise_for_status()
            self.attempts.append(
                EndpointAttempt(
                    name=name,
                    base_url="https://oam.cnb.cz",
                    dataset="OAM_CNB_CZ",
                    endpoint=url.replace("https://oam.cnb.cz", ""),
                    method=method.upper(),
                    http_status=response.status_code,
                    success=True,
                )
            )
            return response
        except Exception as exc:
            self.attempts.append(
                EndpointAttempt(
                    name=name,
                    base_url="https://oam.cnb.cz",
                    dataset="OAM_CNB_CZ",
                    endpoint=url.replace("https://oam.cnb.cz", ""),
                    method=method.upper(),
                    http_status=(
                        getattr(response, "status_code", None)
                        if response is not None
                        else None
                    ),
                    success=False,
                    error=str(exc),
                )
            )
            raise

    def _fetch_feed(self, since: date | None = None) -> tuple[CzechiaNotice, ...]:
        if self._feed_cache is not None:
            self._cache_hits += 1
            return self._feed_cache

        # 1. Initialize session on start page
        start_headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        }
        self._request("GET", self.start_url, headers=start_headers, name="CNB OAM Start Page")

        # 2. Prepare search parameters
        date_from = since or (date.today() - timedelta(days=self.lookback_days))
        date_from_str = date_from.strftime("%d.%m.%Y")
        
        search_data = {
            "_xpf": "",
            "_xpt": "1",
            "_xdo": "/OAM_CNB_CZ/R1_RES.xdo",
            "_paramspar_cur_fol": "OAM_CNB_CZ",
            "_paramspar_dad": (
                "https://oam.cnb.cz/sipresextdad/&_paramspar_lang=cs&_xt=lay_R1&_xf=html"
            ),
            "id": "oam_cnb_cz",
            "_xuil": "en_US",
            "_paramspar_oznam": "OZNAM*",
            "_paramspar_emit": "EMIT*",
            "_paramspar_oznam_ico": "OZNAM-ICO*",
            "_paramspar_osemit": "OSEMIT*",
            "_paramspar_dattrans_od": "1.1.1900",
            "_paramspar_dattrans_do": "1.1.4000",
            "_paramspar_emit_ico": "EMIT-ICO*",
            "_paramspar_emit_lei": "EMIT-LEI*",
            "_paramspar_ISIN": "ISIN*",
            "_paramspar_typ_inf": "",
            "_paramspar_typ_zpr": "",
            "_paramspar_datum_od": date_from_str,
            "_paramspar_datum_do": "01.01.4000",
            "_paramspar_count": "1000",
            "_xmode": "",
            "_paramspar_typ_inf_csv": "01",  # Category 01 = Annual/Half-yearly financial report
            "_paramspar_typ_zprav_csv": "",
        }

        # 3. Post search request
        search_headers = {
            "Referer": self.start_url,
            "Origin": "https://oam.cnb.cz",
        }
        resp_search = self._request(
            "POST",
            self.search_url,
            data=search_data,
            headers=search_headers,
            name="CNB OAM Report Query",
        )

        # 4. Extract polling URL from loading page Javascript
        match = re.search(r'var url = "(/xmlpserver/servlet/xdo[^"]+)";', resp_search.text)
        if not match:
            LOGGER.error("CNB OAM: Could not find polling URL in Javascript response.")
            raise ConnectorError("Oracle BI Publisher loading page mismatch")

        poll_path = match.group(1)
        poll_url = "https://oam.cnb.cz" + poll_path

        # 5. Poll status until completed
        poll_headers = {
            "Referer": self.search_url,
            "Origin": "https://oam.cnb.cz",
        }
        max_attempts = 15
        ready = False
        for attempt in range(max_attempts):
            resp_poll = self._request(
                "POST",
                poll_url,
                data="",
                headers=poll_headers,
                name=f"CNB OAM Status Poll {attempt+1}",
            )
            poll_text = resp_poll.text.strip()
            if poll_text != "":
                ready = True
                break
            time.sleep(1.0)

        if not ready:
            raise ConnectorError("Oracle BI Publisher status check timed out")

        # 6. Retrieve structured XML report data
        final_data = {"fromLoadingPage": "true", "finalRequest": "true"}
        resp_final = self._request(
            "POST",
            poll_url,
            data=final_data,
            headers=poll_headers,
            name="CNB OAM XML Fetch",
        )

        # 7. Parse XML payload
        notices = self._parse_xml(resp_final.text)
        self._feed_cache = notices
        self._scanned_notices = len(notices)
        return notices

    def _parse_xml(self, xml_text: str) -> tuple[CzechiaNotice, ...]:
        try:
            # Clean up the xml_text in case it has leading HTML junk or declarations
            xml_text_clean = xml_text.strip()
            # If the XML starts with HTML comments or other tags, slice them out
            if not xml_text_clean.startswith("<?xml"):
                xml_start = xml_text_clean.find("<?xml")
                if xml_start != -1:
                    xml_text_clean = xml_text_clean[xml_start:]
            
            root = ET.fromstring(xml_text_clean.encode("utf-8"))
        except Exception as exc:
            LOGGER.error("Failed to parse CNB XML response: %s", exc)
            # Log first 500 chars to debug
            LOGGER.debug("XML content snippet: %r", xml_text[:500])
            return ()

        # Find reports in DS_R1_A
        reports_el = root.find("DS_R1_A")
        if reports_el is None:
            LOGGER.warning("CNB XML: DS_R1_A not found in payload")
            return ()

        # Build attachment maps from DS_R1_ATTACH
        attachments_map: dict[str, list[CzechiaFile]] = {}
        attach_el = root.find("DS_R1_ATTACH")
        if attach_el is not None:
            for attach_row in attach_el.findall("ROW"):
                form_id_el = attach_row.find("ID_FORMULARE")
                file_id_el = attach_row.find("PRILOHA_ID")
                filename_el = attach_row.find("JM_SOUBORU")
                file_type_el = attach_row.find("TP_TYP_PRIL")
                lang_el = attach_row.find("TEXT_EN")
                
                if form_id_el is not None and file_id_el is not None and filename_el is not None:
                    form_id = form_id_el.text.strip()
                    file_id = file_id_el.text.strip()
                    filename = filename_el.text.strip()
                    file_type = file_type_el.text.strip() if file_type_el is not None else "zip"
                    lang = lang_el.text.strip() if lang_el is not None else None
                    
                    attachments_map.setdefault(form_id, []).append(
                        CzechiaFile(
                            filename=filename,
                            file_id=file_id,
                            file_type=file_type,
                            language=lang,
                        )
                    )

        notices: list[CzechiaNotice] = []
        for row in reports_el.findall("ROW"):
            form_id_el = row.find("ID_FORMULARE")
            published_raw_el = row.find("DATUM_PRIJETI")
            report_type_el = row.find("TYP_ZPRAVY_EN")
            issuer_name_el = row.find("IDENTIFIKACE")
            ico_el = row.find("IDENTIFIKACNI_CISLO_HODNOTA")
            lei_el = row.find("LEI")

            if (
                form_id_el is not None
                and published_raw_el is not None
                and report_type_el is not None
                and issuer_name_el is not None
                and ico_el is not None
            ):
                form_id = form_id_el.text.strip()
                published_raw = published_raw_el.text.strip()
                published_at = _parse_czech_date(published_raw)
                report_type = report_type_el.text.strip()
                issuer_name = issuer_name_el.text.strip()
                ico = ico_el.text.strip()
                lei = lei_el.text.strip() if lei_el is not None else None

                notices.append(
                    CzechiaNotice(
                        form_id=form_id,
                        published_raw=published_raw,
                        published_at=published_at,
                        report_type=report_type,
                        issuer_name=issuer_name,
                        ico=ico,
                        lei=lei,
                        files=tuple(attachments_map.get(form_id, ())),
                    )
                )

        return tuple(notices)

    def _candidate(
        self,
        notice: CzechiaNotice,
        file: CzechiaFile,
    ) -> DocumentCandidate:
        # Document Type classification
        is_annual = "annual" in notice.report_type.lower()
        doc_type = "annual_financial_report" if is_annual else "half_year_financial_report"
        
        # File suffix verification
        suffix = PurePosixPath(file.filename).suffix.casefold()
        if suffix not in SUPPORTED_EXTENSIONS:
            doc_type = "other_regulatory_announcement"
            reason = f"Excluded file format '{suffix}' for filename '{file.filename}'"
            positive = []
            negative = [suffix]
        else:
            reason = f"CNB official periodic report: {notice.report_type}"
            positive = [notice.report_type]
            negative = []

        # Date heuristics
        dates = _extract_czechia_date_info(
            title=notice.report_type,
            filename=file.filename,
            published_date=notice.published_at,
            is_annual=is_annual,
        )

        download_url = f"{self.download_base_url}?file_id={file.file_id}"
        
        # Try to extract ISIN from filename
        extracted_isins = _extract_isins(file.filename)

        return DocumentCandidate(
            title=f"{notice.report_type} - {file.filename}",
            url=download_url,
            published_date=notice.published_at,
            document_type=doc_type,
            source=self.source_name,
            source_document_id=f"{notice.form_id}:{file.file_id}",
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_ico": notice.ico,
                "issuer_lei": notice.lei,
                "issuer_country": "Czechia",
                "home_member_state": "Czechia",
                "pea_geography_status": "eu_candidate",
                "form_id": notice.form_id,
                "file_id": file.file_id,
                "filename": file.filename,
                "file_format": suffix.lstrip("."),
                "file_language": file.language,
                "extracted_isins": extracted_isins,
                "parent_page_url": self.start_url,
                "date_extraction_reason": dates["reason"],
            },
            classification=doc_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            published_at=notice.published_at,
            period_end_date=dates["period_end_date"],
            reporting_year=dates["reporting_year"],
            source_publication_date_raw=notice.published_raw,
            source_period_date_raw=dates["period_end_date"].isoformat() if dates["period_end_date"] else None,
            date_confidence=dates["confidence"],
            date_extraction_reason=dates["reason"],
        )

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        
        try:
            notices = self._fetch_feed(since)
        except Exception as exc:
            self.state = ConnectorState.DEGRADED
            self.last_error = str(exc)
            raise

        candidates: list[DocumentCandidate] = []
        for notice in notices:
            for file in notice.files:
                candidate = self._candidate(notice, file)
                candidates.append(candidate)
                if limit is not None and len(candidates) >= limit:
                    break
            if limit is not None and len(candidates) >= limit:
                break
        return candidates

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        try:
            notices = self._fetch_feed()
        except Exception as exc:
            self.state = ConnectorState.DEGRADED
            self.last_error = str(exc)
            raise

        expected_name = _normalize(issuer.name)
        expected_ico = _normalize(getattr(issuer, "symbol", ""))  # Often symbol stores registration code or ticker
        
        candidates: list[DocumentCandidate] = []
        for notice in notices:
            # Check watchlist matches
            lei_match = issuer.isin and notice.lei and notice.lei.upper() == issuer.isin.upper() # Wait, isin field in models might store LEI if we don't have ISIN
            # Let's match by LEI or ICO or Name
            # We also check if ISIN is extracted from filename
            ico_match = expected_ico and (expected_ico == _normalize(notice.ico) or expected_ico in _normalize(notice.issuer_name))
            name_match = expected_name and (expected_name == _normalize(notice.issuer_name) or expected_name in _normalize(notice.issuer_name))
            
            isin_in_files = False
            if issuer.isin:
                for file in notice.files:
                    if issuer.isin.upper() in _extract_isins(file.filename):
                        isin_in_files = True
                        break

            if not lei_match and not ico_match and not name_match and not isin_in_files:
                continue

            for file in notice.files:
                candidates.append(self._candidate(notice, file))
        return candidates

    def resolve_issuer(self, issuer: Issuer) -> CzechiaIssuerResolution:
        try:
            notices = self._fetch_feed()
        except Exception as exc:
            return CzechiaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

        expected_name = _normalize(issuer.name)
        expected_symbol = _normalize(issuer.symbol)
        expected_isin = issuer.isin.upper() if issuer.isin else ""

        best: tuple[float, CzechiaNotice] | None = None
        for notice in notices:
            score = 0.0
            
            # Match by LEI
            if expected_isin and notice.lei and expected_isin == notice.lei.upper():
                score = 100.0
            # Match by ICO
            elif expected_symbol and expected_symbol == _normalize(notice.ico):
                score = 95.0
            # Match by Name
            elif expected_name:
                observed_name = _normalize(notice.issuer_name)
                if expected_name == observed_name:
                    score = 90.0
                elif expected_name in observed_name or observed_name in expected_name:
                    score = 80.0
            
            # Extract ISINs from files as secondary check
            if not score and expected_isin:
                for file in notice.files:
                    if expected_isin in _extract_isins(file.filename):
                        score = 85.0
                        break

            if score > 0 and (best is None or score > best[0]):
                best = (score, notice)
                if score == 100.0:
                    break

        if best is None:
            return CzechiaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error="No matching Czech issuer found in CNB CÚRI feed",
            )

        score, notice = best
        issuer_url = f"{self.start_url}&p_ico={notice.ico}"
        return CzechiaIssuerResolution(
            found=True,
            matched_name=notice.issuer_name,
            czechia_cnb_curi_name=notice.issuer_name,
            czechia_cnb_curi_issuer_url=issuer_url,
            czechia_cnb_curi_detail_url=self.start_url,
            czechia_cnb_curi_record_id=notice.ico,
            home_member_state="Czechia",
            match_score=score,
            attempts=tuple(self.attempts),
        )

    def discover(self, query: str, limit: int = 25) -> CzechiaSourceDiscovery:
        try:
            notices = self._fetch_feed()
        except Exception as exc:
            return CzechiaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

        normalized_query = _normalize(query)
        matching_notices: list[CzechiaNotice] = []
        candidates: list[DocumentCandidate] = []

        for notice in notices:
            # Check if query is in report type, issuer name, or filenames
            haystack = _normalize(
                " ".join([
                    notice.report_type,
                    notice.issuer_name,
                    notice.ico,
                    notice.lei or "",
                    " ".join(f.filename for f in notice.files)
                ])
            )
            if normalized_query in haystack:
                matching_notices.append(notice)
                for file in notice.files:
                    candidates.append(self._candidate(notice, file))
                if len(candidates) >= limit:
                    break

        return CzechiaSourceDiscovery(
            source=self.source_name,
            query=query,
            notices=tuple(matching_notices),
            candidates=tuple(candidates[:limit]),
            attempts=tuple(self.attempts),
        )

    def diagnose(self) -> CzechiaSourceDiagnostic:
        try:
            notices = self._fetch_feed()
            categories: dict[str, int] = {}
            formats: set[str] = set()
            for notice in notices:
                categories[notice.report_type] = categories.get(notice.report_type, 0) + 1
                for file in notice.files:
                    suffix = PurePosixPath(file.filename).suffix.casefold()
                    if suffix:
                        formats.add(suffix.lstrip("."))
            
            example = None
            if notices:
                first = notices[0]
                example = {
                    "form_id": first.form_id,
                    "published_raw": first.published_raw,
                    "report_type": first.report_type,
                    "issuer_name": first.issuer_name,
                    "ico": first.ico,
                    "lei": first.lei,
                    "files": [
                        {
                            "filename": f.filename,
                            "file_id": f.file_id,
                            "type": f.file_type,
                            "lang": f.language,
                        }
                        for f in first.files
                    ],
                }

            efficiency = 1.0
            if len(self.attempts) > 0:
                # Scanned vs calls
                efficiency = len(notices) / len(self.attempts)

            status = 200
            for attempt in reversed(self.attempts):
                if attempt.success and attempt.http_status:
                    status = attempt.http_status
                    break

            return CzechiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if notices else ConnectorState.DEGRADED,
                called_url=self.search_url,
                http_status=status,
                method_used="Oracle BI Publisher stateful XML search and polling",
                total_count=len(notices),
                detected_count=len(notices),
                fields=(
                    "ID_FORMULARE",
                    "DATUM_PRIJETI",
                    "TYP_ZPRAVY_EN",
                    "IDENTIFIKACE",
                    "IDENTIFIKACNI_CISLO_HODNOTA",
                    "LEI",
                    "attachments",
                ),
                categories=categories,
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                request_efficiency=round(efficiency, 2),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return CzechiaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.search_url,
                http_status=None,
                method_used="Oracle BI Publisher stateful XML search and polling",
                total_count=0,
                detected_count=0,
                fields=(),
                categories={},
                formats=(),
                example_notice=None,
                http_calls=len(self.attempts),
                request_efficiency=0.0,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        # Usually 1 start page GET + 1 search POST + ~2-4 polling POSTs + 1 final XML fetch
        return 7

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 7
