from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
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


DEFAULT_FEED_URL = (
    "https://my.oekb.at/issuer-info/rest/public/meldedaten/iic"
)
DEFAULT_DOWNLOAD_BASE_URL = (
    "https://my.oekb.at/issuer-info/rest/public/meldedaten/download"
)
DEFAULT_ISSUER_LIST_URL = (
    "https://my.oekb.at/kapitalmarkt-services/kms-output/oamn/iic/list"
)

PERIODIC_TYPE_MAP = {
    "EP_JFB": "annual_financial_report",
    "EP_JFB_XBRL": "annual_financial_report",
    "EP_HJFB": "half_year_financial_report",
    "EP_HJFB_XBRL": "half_year_financial_report",
    "EP_QUARTALBER": "quarterly_financial_report",
}

KNOWN_REJECTED_TYPES = {
    "EP_AD_HOC",
    "EP_EIGENGESCHAEFT_VON_FUEHRUNGSKRAFT",
    "EP_AEND_WESENTL_STIMMRECHTSSCHWELLEN",
    "EP_AEND_WESENTL_ANTEILSSCHWELLEN_EIG_AKTIEN",
    "EP_AEND_STIMMRECHTSGESAMTZAHL",
    "EP_OPTIONEN_RUECKKAUF_VERAEUSSERUNG",
    "EP_BEKANNTGABEN_ZU_RUECKKAUFPROGRAMMEN",
    "EP_HV_ANKUENDIGUNG",
    "EP_HV_ERGEBNISSE",
    "EP_SONST_KAP_MASSNAHMEN",
    "EP_BER_ZAHLUNG_STAAT",
}

SUPPORTED_FORMATS = {"pdf", "zip", "xhtml", "xht", "xbri"}


def _normalize(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.casefold()).strip()


def _parse_source_date(value: object) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            seconds = float(value)
            if seconds > 10_000_000_000:
                seconds /= 1000
            return datetime.fromtimestamp(seconds, UTC).date()
        except (OverflowError, OSError, ValueError):
            return None
    raw = str(value).strip()
    if raw.isdigit():
        return _parse_source_date(int(raw))
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", raw)
    if match:
        try:
            return date.fromisoformat(match.group(1))
        except ValueError:
            return None
    return None


def _extract_isins(*values: object) -> tuple[str, ...]:
    found: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                visit(item)
            return
        if value is None:
            return
        found.update(
            re.findall(
                r"\b[A-Z]{2}[A-Z0-9]{9}\d\b",
                str(value).upper(),
            )
        )

    for value in values:
        visit(value)
    return tuple(sorted(found))


def _file_format(filename: str) -> str | None:
    suffix = PurePosixPath(filename).suffix.casefold().lstrip(".")
    if suffix not in SUPPORTED_FORMATS:
        return None
    return "xhtml" if suffix == "xht" else suffix


def classify_austria_document(
    meldetyp_code: str,
) -> tuple[str, str, list[str], list[str]]:
    code = (meldetyp_code or "").strip().upper()
    document_type = PERIODIC_TYPE_MAP.get(code)
    if document_type:
        return (
            document_type,
            f"OeKB periodic category: {code}",
            [code],
            [],
        )
    if code in KNOWN_REJECTED_TYPES:
        return (
            "other_regulatory_announcement",
            f"OeKB excluded regulatory category: {code}",
            [],
            [code],
        )
    return (
        "other_regulatory_announcement",
        f"OeKB category is not an accepted periodic report: {code or 'missing'}",
        [],
        [code] if code else [],
    )


def extract_austria_date_info(
    *,
    title: str,
    filename: str,
    published_raw: object,
    period_start_raw: object = None,
    period_end_raw: object = None,
) -> dict[str, Any]:
    published_at = _parse_source_date(published_raw)
    period_end_date = _parse_source_date(period_end_raw)
    reporting_year = period_end_date.year if period_end_date else None
    source_period_raw = (
        str(period_end_raw).strip()
        if period_end_raw not in (None, "")
        else None
    )
    confidence = "high" if published_at else "low"
    reason = (
        "Reporting period end supplied explicitly by OeKB"
        if period_end_date
        else "No unambiguous reporting-period end supplied by OeKB"
    )

    if period_end_date is None:
        text = f"{title} {filename}"
        plausible_dates: list[date] = []
        for raw_date in re.findall(r"\b(20\d{2}-\d{2}-\d{2})\b", text):
            try:
                parsed = date.fromisoformat(raw_date)
            except ValueError:
                continue
            if published_at and parsed > published_at:
                continue
            if published_at and (published_at - parsed) < timedelta(days=14):
                continue
            plausible_dates.append(parsed)
        if plausible_dates:
            period_end_date = max(plausible_dates)
            reporting_year = period_end_date.year
            source_period_raw = period_end_date.isoformat()
            reason = (
                "OeKB publication timestamp parsed; reporting period extracted "
                "from an explicit filename date distinct from publication"
            )

    if reporting_year is None:
        text = f"{title} {filename}"
        ranges = re.findall(
            r"(?<![A-Za-z0-9])(20\d{2})\s*[/_-]\s*"
            r"(\d{2}|20\d{2})(?![-_]\d{2})(?![A-Za-z0-9])",
            text,
        )
        if ranges:
            first, second = ranges[-1]
            reporting_year = int(
                second if len(second) == 4 else first[:2] + second
            )
            source_period_raw = source_period_raw or f"{first}/{second}"
            reason = (
                "OeKB publication timestamp parsed; reporting year extracted "
                "from an explicit fiscal-year range"
            )
        else:
            normalized = _normalize(text)
            if any(
                token in normalized
                for token in (
                    "annual report",
                    "annual financial report",
                    "jahresfinanzbericht",
                    "half year",
                    "half yearly",
                    "halbjahresfinanzbericht",
                    "quarter",
                    "quartal",
                )
            ):
                year_text = re.sub(
                    r"20\d{2}-\d{2}-\d{2}",
                    " ",
                    text,
                )
                years = [
                    int(value)
                    for value in re.findall(
                        r"(?<!\d)(20\d{2})(?!\d)",
                        year_text,
                    )
                    if value != "2018"
                ]
                if years:
                    reporting_year = years[-1]
                    source_period_raw = source_period_raw or str(reporting_year)
                    reason = (
                        "OeKB publication timestamp parsed; reporting year "
                        "extracted from report title or filename"
                    )

    return {
        "published_at": published_at,
        "period_end_date": period_end_date,
        "reporting_year": reporting_year,
        "source_publication_date_raw": (
            str(published_raw) if published_raw not in (None, "") else None
        ),
        "source_period_date_raw": source_period_raw,
        "date_confidence": confidence,
        "date_extraction_reason": reason,
        "period_start_date": _parse_source_date(period_start_raw),
    }


@dataclass(frozen=True, slots=True)
class AustriaFile:
    file_id: str
    filename: str
    file_format: str
    language: str | None
    declared_size: int | None


@dataclass(frozen=True, slots=True)
class AustriaNotice:
    record_id: str
    issuer_id: str | None
    issuer_name: str
    issuer_lei: str | None
    issuer_city: str | None
    title: str
    description: str
    meldetyp_code: str
    language: str | None
    published_raw: object
    period_start_raw: object
    period_end_raw: object
    status: str | None
    issuer_isins: tuple[str, ...]
    files: tuple[AustriaFile, ...]


@dataclass(frozen=True, slots=True)
class AustriaSourceDiagnostic:
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
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AustriaSourceDiscovery:
    source: str
    query: str
    notices: tuple[AustriaNotice, ...]
    candidates: tuple[DocumentCandidate, ...]
    attempts: tuple[EndpointAttempt, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AustriaIssuerResolution:
    found: bool
    matched_name: str | None = None
    austria_oekb_oam_id: str | None = None
    austria_oekb_oam_issuer_url: str | None = None
    austria_oekb_oam_detail_url: str | None = None
    austria_home_member_state: str | None = None
    austria_pea_country_check: str | None = "eu_candidate"
    match_score: float = 0.0
    attempts: tuple[EndpointAttempt, ...] = ()
    error: str | None = None


def parse_austria_feed(payload: object) -> tuple[AustriaNotice, ...]:
    if not isinstance(payload, dict):
        raise ValueError("OeKB feed payload must be a JSON object")
    raw_documents = payload.get("dokumente")
    if not isinstance(raw_documents, list):
        raise ValueError("OeKB feed is missing the 'dokumente' list")

    notices: list[AustriaNotice] = []
    for raw_notice in raw_documents:
        if not isinstance(raw_notice, dict):
            continue
        record_id = str(raw_notice.get("id") or "").strip()
        issuer = raw_notice.get("emittent")
        issuer_data = issuer if isinstance(issuer, dict) else {}
        raw_files = raw_notice.get("dateien")
        files: list[AustriaFile] = []
        file_isins: list[object] = []
        for raw_file in raw_files if isinstance(raw_files, list) else []:
            if not isinstance(raw_file, dict):
                continue
            file_id = str(raw_file.get("id") or "").strip()
            filename = str(raw_file.get("dateiname") or "").strip()
            file_format = _file_format(filename)
            if not file_id or not filename or not file_format:
                continue
            declared_size = raw_file.get("sizeInKB")
            try:
                parsed_size = int(declared_size)
            except (TypeError, ValueError):
                parsed_size = None
            files.append(
                AustriaFile(
                    file_id=file_id,
                    filename=filename,
                    file_format=file_format,
                    language=str(raw_file.get("sprachcode") or "").strip()
                    or None,
                    declared_size=parsed_size,
                )
            )
            file_isins.append(raw_file.get("isinBezug"))
        if not record_id:
            continue
        notices.append(
            AustriaNotice(
                record_id=record_id,
                issuer_id=str(issuer_data.get("id") or "").strip() or None,
                issuer_name=str(issuer_data.get("name") or "").strip(),
                issuer_lei=str(issuer_data.get("lei") or "").strip() or None,
                issuer_city=str(issuer_data.get("ort") or "").strip() or None,
                title=str(raw_notice.get("titel") or "").strip(),
                description=str(
                    raw_notice.get("enKurzbeschreibung")
                    or raw_notice.get("kurzbeschreibung")
                    or ""
                ).strip(),
                meldetyp_code=str(
                    raw_notice.get("meldetypCode") or ""
                ).strip(),
                language=str(raw_notice.get("sprachcode") or "").strip()
                or None,
                published_raw=raw_notice.get("uploadzeitpunkt"),
                period_start_raw=raw_notice.get("von"),
                period_end_raw=raw_notice.get("bis"),
                status=str(raw_notice.get("status") or "").strip() or None,
                issuer_isins=_extract_isins(
                    raw_notice.get("isinBezug"),
                    file_isins,
                ),
                files=tuple(files),
            )
        )
    return tuple(notices)


class AustriaOekbOamConnector(Connector):
    market = "Vienna Stock Exchange"
    source_name = "oekb_oam"
    supports_source_first = True

    def __init__(
        self,
        *,
        session: requests.Session,
        feed_url: str = DEFAULT_FEED_URL,
        download_base_url: str = DEFAULT_DOWNLOAD_BASE_URL,
        issuer_list_url: str = DEFAULT_ISSUER_LIST_URL,
        rate_limit_seconds: float = 0.2,
        lookback_days: int = 30,
        timeout: int = 30,
        verify_ssl: bool = True,
    ) -> None:
        self.session = session
        self.feed_url = feed_url
        self.download_base_url = download_base_url.rstrip("/")
        self.issuer_list_url = issuer_list_url
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.lookback_days = max(1, lookback_days)
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.state = ConnectorState.READY
        self.last_error: str | None = None
        self.attempts: list[EndpointAttempt] = []
        self._last_request_at = 0.0
        self._feed_cache: tuple[AustriaNotice, ...] | None = None
        self._scanned_notices = 0
        self._details_visited = 0

    def _wait(self) -> None:
        remaining = self.rate_limit_seconds - (
            time.monotonic() - self._last_request_at
        )
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_feed(self) -> tuple[AustriaNotice, ...]:
        if self._feed_cache is not None:
            return self._feed_cache
        self._wait()
        response: Any | None = None
        try:
            response = self.session.get(
                self.feed_url,
                headers={"Accept": "application/json"},
                timeout=self.timeout,
                verify=self.verify_ssl,
            )
            response.raise_for_status()
            notices = parse_austria_feed(response.json())
            self.attempts.append(
                EndpointAttempt(
                    name="OeKB OAM global feed",
                    base_url="https://my.oekb.at",
                    dataset="issuer-info",
                    endpoint=self.feed_url,
                    method="GET",
                    http_status=response.status_code,
                    success=True,
                    total_count=len(notices),
                )
            )
            self._feed_cache = notices
            self._scanned_notices = len(notices)
            self.state = ConnectorState.READY
            self.last_error = None
            return notices
        except Exception as exc:
            self.state = ConnectorState.UNAVAILABLE
            self.last_error = str(exc)
            self.attempts.append(
                EndpointAttempt(
                    name="OeKB OAM global feed",
                    base_url="https://my.oekb.at",
                    dataset="issuer-info",
                    endpoint=self.feed_url,
                    method="GET",
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
        finally:
            self._last_request_at = time.monotonic()

    def _candidate(
        self,
        notice: AustriaNotice,
        file: AustriaFile,
    ) -> DocumentCandidate:
        document_type, reason, positive, negative = classify_austria_document(
            notice.meldetyp_code
        )
        dates = extract_austria_date_info(
            title=notice.title,
            filename=file.filename,
            published_raw=notice.published_raw,
            period_start_raw=notice.period_start_raw,
            period_end_raw=notice.period_end_raw,
        )
        return DocumentCandidate(
            title=f"{notice.title} - {file.filename}",
            url=f"{self.download_base_url}/{file.file_id}",
            published_date=dates["published_at"],
            document_type=document_type,
            source=self.source_name,
            source_document_id=f"{notice.record_id}:{file.file_id}",
            metadata={
                "official_source": 1,
                "issuer_name": notice.issuer_name,
                "issuer_isins": list(notice.issuer_isins),
                "issuer_lei": notice.issuer_lei,
                "issuer_country": "Austria",
                "home_member_state": "Austria",
                "pea_country_check": "eu_candidate",
                "pea_geography_status": "eu_candidate",
                "austria_oekb_oam_id": notice.issuer_id,
                "austria_oekb_oam_issuer_url": self.issuer_list_url,
                "austria_oekb_oam_detail_url": self.issuer_list_url,
                "record_id": notice.record_id,
                "file_id": file.file_id,
                "filename": file.filename,
                "file_format": file.file_format,
                "file_language": file.language,
                "notice_language": notice.language,
                "meldetyp_code": notice.meldetyp_code,
                "source_status": notice.status,
                "source_declared_size": file.declared_size,
                "parent_page_url": self.issuer_list_url,
            },
            classification=document_type,
            classification_reason=reason,
            matched_positive_terms=positive,
            matched_negative_terms=negative,
            published_at=dates["published_at"],
            period_end_date=dates["period_end_date"],
            reporting_year=dates["reporting_year"],
            source_publication_date_raw=dates[
                "source_publication_date_raw"
            ],
            source_period_date_raw=dates["source_period_date_raw"],
            date_confidence=dates["date_confidence"],
            date_extraction_reason=dates["date_extraction_reason"],
        )

    def _notice_candidates(
        self,
        notice: AustriaNotice,
    ) -> list[DocumentCandidate]:
        return [self._candidate(notice, file) for file in notice.files]

    def search_recent_documents(
        self,
        market: str,
        since: date | None = None,
        limit: int | None = None,
    ) -> list[DocumentCandidate]:
        if market.casefold() != self.market.casefold():
            return []
        cutoff = since or (date.today() - timedelta(days=self.lookback_days))
        candidates: list[DocumentCandidate] = []
        for notice in self._fetch_feed():
            published_at = _parse_source_date(notice.published_raw)
            if published_at is None or published_at < cutoff:
                continue
            candidates.extend(self._notice_candidates(notice))
            if limit is not None and len(candidates) >= limit:
                break
        return candidates[:limit] if limit is not None else candidates

    def search_documents_for_issuer(
        self,
        issuer: Issuer,
    ) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        expected_name = _normalize(issuer.name)
        for notice in self._fetch_feed():
            exact_isin = (
                bool(issuer.isin)
                and issuer.isin.upper() in notice.issuer_isins
            )
            name_match = (
                not notice.issuer_isins
                and expected_name
                and expected_name == _normalize(notice.issuer_name)
            )
            if not exact_isin and not name_match:
                continue
            candidates.extend(self._notice_candidates(notice))
        return candidates

    def search_documents(self, issuer: Issuer) -> list[DocumentCandidate]:
        return self.search_documents_for_issuer(issuer)

    def resolve_issuer(self, issuer: Issuer) -> AustriaIssuerResolution:
        try:
            notices = self._fetch_feed()
        except Exception as exc:
            return AustriaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error=str(exc),
            )

        expected_name = _normalize(issuer.name)
        best: tuple[float, AustriaNotice] | None = None
        for notice in notices:
            score = 0.0
            if issuer.isin and issuer.isin.upper() in notice.issuer_isins:
                score = 100.0
            elif expected_name:
                observed_name = _normalize(notice.issuer_name)
                if expected_name == observed_name:
                    score = 90.0
                elif expected_name in observed_name or observed_name in expected_name:
                    score = 80.0
            if score and (best is None or score > best[0]):
                best = (score, notice)
                if score == 100.0:
                    break

        if best is None:
            return AustriaIssuerResolution(
                found=False,
                attempts=tuple(self.attempts),
                error="No matching OeKB issuer found in the public feed",
            )
        score, notice = best
        issuer_url = (
            f"{self.issuer_list_url}?emittentId={notice.issuer_id}"
            if notice.issuer_id
            else self.issuer_list_url
        )
        return AustriaIssuerResolution(
            found=True,
            matched_name=notice.issuer_name,
            austria_oekb_oam_id=notice.issuer_id,
            austria_oekb_oam_issuer_url=issuer_url,
            austria_oekb_oam_detail_url=self.issuer_list_url,
            austria_home_member_state="Austria",
            austria_pea_country_check="eu_candidate",
            match_score=score,
            attempts=tuple(self.attempts),
        )

    @staticmethod
    def _query_matches(query: str, notice: AustriaNotice) -> bool:
        normalized_query = _normalize(query)
        words = set(normalized_query.split())
        code = notice.meldetyp_code.upper()
        if any(term in normalized_query for term in ("half year", "half yearly", "halfyear")):
            return code in {"EP_HJFB", "EP_HJFB_XBRL"}
        if any(term in normalized_query for term in ("annual", "jahr", "yearly")):
            return code in {"EP_JFB", "EP_JFB_XBRL"}
        if any(term in normalized_query for term in ("quarter", "quartal")):
            return code == "EP_QUARTALBER"
        if any(term in normalized_query for term in ("esef", "xbrl", "xhtml")):
            return code in {"EP_JFB_XBRL", "EP_HJFB_XBRL"}
        haystack = _normalize(
            " ".join(
                (
                    notice.title,
                    notice.description,
                    notice.issuer_name,
                    notice.meldetyp_code,
                    " ".join(file.filename for file in notice.files),
                )
            )
        )
        return bool(words) and words.issubset(set(haystack.split()))

    def discover(
        self,
        query: str,
        limit: int = 25,
    ) -> AustriaSourceDiscovery:
        try:
            matching = [
                notice
                for notice in self._fetch_feed()
                if self._query_matches(query, notice)
            ][:limit]
            candidates: list[DocumentCandidate] = []
            for notice in matching:
                candidates.extend(self._notice_candidates(notice))
                if len(candidates) >= limit:
                    break
            return AustriaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=tuple(matching),
                candidates=tuple(candidates[:limit]),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            return AustriaSourceDiscovery(
                source=self.source_name,
                query=query,
                notices=(),
                candidates=(),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def diagnose(self) -> AustriaSourceDiagnostic:
        try:
            notices = self._fetch_feed()
            categories: dict[str, int] = {}
            formats: set[str] = set()
            for notice in notices:
                categories[notice.meldetyp_code] = (
                    categories.get(notice.meldetyp_code, 0) + 1
                )
                formats.update(file.file_format for file in notice.files)
            example = None
            periodic = next(
                (
                    notice
                    for notice in notices
                    if notice.meldetyp_code in PERIODIC_TYPE_MAP
                    and notice.files
                ),
                notices[0] if notices else None,
            )
            if periodic:
                example = {
                    "record_id": periodic.record_id,
                    "issuer_id": periodic.issuer_id,
                    "issuer": periodic.issuer_name,
                    "isins": list(periodic.issuer_isins),
                    "title": periodic.title,
                    "meldetyp_code": periodic.meldetyp_code,
                    "published_at": (
                        _parse_source_date(periodic.published_raw).isoformat()
                        if _parse_source_date(periodic.published_raw)
                        else None
                    ),
                    "files": [
                        {
                            "file_id": file.file_id,
                            "filename": file.filename,
                            "format": file.file_format,
                            "download_url": (
                                f"{self.download_base_url}/{file.file_id}"
                            ),
                        }
                        for file in periodic.files
                    ],
                }
            status = next(
                (
                    attempt.http_status
                    for attempt in reversed(self.attempts)
                    if attempt.success
                ),
                None,
            )
            return AustriaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.READY if notices else ConnectorState.DEGRADED,
                called_url=self.feed_url,
                http_status=status,
                method_used="single global JSON feed with local filtering",
                total_count=len(notices),
                detected_count=len(notices),
                fields=(
                    "id",
                    "emittent",
                    "titel",
                    "meldetypCode",
                    "uploadzeitpunkt",
                    "von",
                    "bis",
                    "isinBezug",
                    "dateien",
                ),
                categories=dict(
                    sorted(
                        categories.items(),
                        key=lambda item: (-item[1], item[0]),
                    )
                ),
                formats=tuple(sorted(formats)),
                example_notice=example,
                http_calls=len(self.attempts),
                attempts=tuple(self.attempts),
            )
        except Exception as exc:
            status = self.attempts[-1].http_status if self.attempts else None
            return AustriaSourceDiagnostic(
                source=self.source_name,
                state=ConnectorState.UNAVAILABLE,
                called_url=self.feed_url,
                http_status=status,
                method_used="single global JSON feed with local filtering",
                total_count=0,
                detected_count=0,
                fields=(),
                categories={},
                formats=(),
                example_notice=None,
                http_calls=len(self.attempts),
                attempts=tuple(self.attempts),
                error=str(exc),
            )

    def estimate_recent_http_requests(
        self,
        *,
        since: date | None,
        limit: int | None,
    ) -> int:
        return 1

    def estimate_issuer_http_requests(self, issuer: Issuer) -> int:
        return 1
