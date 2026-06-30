from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

import requests

from config import Settings
from connectors import (
    SUPPORTED_WATCH_MARKETS,
    connector_for_market,
    is_supported_market,
)
from connectors.base import Connector, ConnectorState, DocumentCandidate
from db import Database
from download import DocumentDownloader, DownloadError
from http_client import (
    RequestCountingSession,
    RequestLimitExceeded,
    build_http_session,
)
from load_watchlist import normalize_market
from models import Issuer
from operations import write_notification_email

LOGGER = logging.getLogger(__name__)

WATCHED_DOCUMENT_TYPES = {
    "annual_financial_report",
    "half_year_financial_report",
    "quarterly_financial_report",
    "financial_report",
    "universal_registration_document",
    "esef",
    "periodic_financial_report",
    "interim_report",
    "quarterly_report",
    "year_end_report",
    "annual financial report",
    "annual financial report ESEF",
    "half-yearly financial report",
    "half-yearly financial report ESEF",
    "quarterly report",
}
MULTI_MARKET_SCOPE = (
    "France + Oslo + Italie + Netherlands + Belgium + Portugal + Ireland + "
    "Spain + Sweden + Denmark + Finland + Austria + Poland + Czechia + Croatia "
    "+ Slovenia + Estonia + Latvia + Lithuania + Slovakia + Romania + Bulgaria"
)
_MARKET_ORDER = {
    market.casefold(): index
    for index, market in enumerate(SUPPORTED_WATCH_MARKETS)
}


@dataclass(slots=True)
class WatchStats:
    issuers_checked: int = 0
    candidates_found: int = 0
    downloaded: int = 0
    duplicates: int = 0
    skipped_too_large: int = 0
    errors: int = 0


@dataclass(slots=True)
class SourceEfficiency:
    source: str
    market: str
    mode: str
    estimated_http_calls: int = 0
    http_calls: int = 0
    scanned_notices: int = 0
    matched_issuers: int = 0
    matched_candidates: int = 0
    details_visited: int = 0
    cache_hits: int = 0
    candidates: int = 0
    rejected_candidates: int = 0
    downloads: int = 0
    duplicates: int = 0
    skipped_too_large: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0


@dataclass(slots=True)
class IssuerCheck:
    issuer: Issuer
    candidates: int = 0
    status: str = "ok"
    detail: str = ""


@dataclass(slots=True)
class DocumentEvent:
    issuer: Issuer
    candidate: DocumentCandidate
    result: str
    path: Path | None = None
    sha256: str | None = None
    reason: str | None = None


@dataclass(slots=True)
class IssuerError:
    issuer: Issuer | None
    stage: str
    message: str


@dataclass(slots=True)
class WatchReport:
    run_id: int
    market: str
    started_at: datetime
    ended_at: datetime
    status: str
    since: date | None
    limit: int | None
    dry_run: bool
    stats: WatchStats
    max_download_bytes: int
    lookback_days: int = 7
    max_candidates_per_source: int = 1000
    max_documents_per_run: int = 100
    execution_mode: str = "issuer-fallback"
    market_stats: dict[str, WatchStats] = field(default_factory=dict)
    source_efficiency: dict[str, SourceEfficiency] = field(
        default_factory=dict
    )
    issuer_checks: list[IssuerCheck] = field(default_factory=list)
    new_documents: list[DocumentEvent] = field(default_factory=list)
    duplicates: list[DocumentEvent] = field(default_factory=list)
    skipped_too_large: list[DocumentEvent] = field(default_factory=list)
    errors: list[IssuerError] = field(default_factory=list)
    degraded_sources: dict[str, str] = field(default_factory=dict)
    ssl_disabled_sources: set[str] = field(default_factory=set)
    rejected_documents: list[DocumentEvent] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class WatchOutcome:
    run_id: int
    status: str
    stats: WatchStats
    market_stats: dict[str, WatchStats]
    report_path: Path
    source_efficiency: dict[str, SourceEfficiency] = field(
        default_factory=dict
    )
    notification_path: Path | None = None

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "success" else 1


def _filter_candidates(
    candidates: list[DocumentCandidate],
    *,
    since: date | None,
    watched_types: set[str] = WATCHED_DOCUMENT_TYPES,
) -> tuple[list[DocumentCandidate], list[tuple[DocumentCandidate, str]]]:
    accepted = []
    rejected = []
    for candidate in candidates:
        pub_at = candidate.published_at
        comp_date = pub_at or candidate.period_end_date
        if since is not None and (
            comp_date is None
            or comp_date < since
        ):
            pub_date_str = pub_at.isoformat() if pub_at else "inconnue"
            rejected.append((candidate, f"Date de publication réelle ({pub_date_str}) antérieure au {since}"))
            continue
            
        if candidate.document_type not in watched_types:
            reason = candidate.classification_reason or f"Type de document '{candidate.document_type}' non surveillé"
            rejected.append((candidate, reason))
            continue
            
        accepted.append(candidate)
        
    filtered = sorted(
        accepted,
        key=lambda candidate: (
            candidate.published_at or date.min,
            candidate.title.casefold(),
            candidate.url,
        ),
        reverse=True,
    )
    return filtered, rejected


def _markdown_text(
    value: object,
    *,
    max_length: int | None = None,
) -> str:
    text = str(value or "").replace("|", r"\|").replace("\r", " ").replace(
        "\n",
        " ",
    )
    if max_length is not None and len(text) > max_length:
        return text[: max_length - 3].rstrip() + "..."
    return text


def _document_table(events: list[DocumentEvent]) -> list[str]:
    if not events:
        return ["Aucun.", ""]
    lines = [
        (
            "| Marché | Société | ISIN | Date Pub. | Clôture | Type | Titre | "
            "Raison classification | Termes + | Termes - | Résultat | SHA256 | Fichier | Source |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for event in events:
        candidate = event.candidate
        result = event.result
        if event.reason:
            result = f"{result} ({event.reason})"
            
        pub_at_str = candidate.published_at.isoformat() if candidate.published_at else "inconnue"
        if getattr(candidate, "date_confidence", None) == "low":
            pub_at_str += " ⚠️"
            
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_text(event.issuer.market),
                    _markdown_text(event.issuer.name),
                    event.issuer.isin,
                    pub_at_str,
                    candidate.period_end_date.isoformat() if candidate.period_end_date else "",
                    _markdown_text(candidate.document_type),
                    _markdown_text(candidate.title, max_length=160),
                    _markdown_text(candidate.classification_reason or ""),
                    _markdown_text(", ".join(candidate.matched_positive_terms or [])),
                    _markdown_text(", ".join(candidate.matched_negative_terms or [])),
                    _markdown_text(result),
                    _markdown_text(event.sha256 or ""),
                    _markdown_text(event.path or ""),
                    f"<{candidate.url}>",
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def _rejected_document_table(events: list[DocumentEvent]) -> list[str]:
    if not events:
        return ["Aucun.", ""]
    lines = [
        "| Marché | Société | ISIN | Date Pub. | Clôture | Classification | Titre | Raison du rejet | Termes + | Termes - | Source |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for event in events:
        candidate = event.candidate
        pub_at_str = candidate.published_at.isoformat() if candidate.published_at else "inconnue"
        if getattr(candidate, "date_confidence", None) == "low":
            pub_at_str += " ⚠️"
            
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_text(event.issuer.market),
                    _markdown_text(event.issuer.name),
                    event.issuer.isin,
                    pub_at_str,
                    candidate.period_end_date.isoformat() if candidate.period_end_date else "",
                    _markdown_text(candidate.document_type),
                    _markdown_text(candidate.title),
                    _markdown_text(event.reason or ""),
                    _markdown_text(", ".join(candidate.matched_positive_terms or [])),
                    _markdown_text(", ".join(candidate.matched_negative_terms or [])),
                    f"<{candidate.url}>",
                )
            )
            + " |"
        )
    lines.append("")
    return lines


def render_watch_report(report: WatchReport) -> str:
    lines = [
        f"# Watch Euronext - {report.started_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
        f"- Run: `{report.run_id}`",
        f"- Marché: `{_markdown_text(report.market)}`",
        f"- Début: `{report.started_at.isoformat(timespec='seconds')}`",
        f"- Fin: `{report.ended_at.isoformat(timespec='seconds')}`",
        f"- Statut: `{report.status}`",
        f"- Mode: `{'dry-run' if report.dry_run else 'download'}`",
        f"- Depuis: `{report.since.isoformat() if report.since else 'non limité'}`",
        (
            f"- Limite effective de documents traités: "
            f"`{report.max_documents_per_run}`"
        ),
        f"- Mode de requêtage: `{report.execution_mode}`",
        f"- Fenêtre quotidienne: `{report.lookback_days} jours`",
        (
            f"- Candidats maximum par source: "
            f"`{report.max_candidates_per_source}`"
        ),
        (
            f"- Taille maximale par document: "
            f"`{report.max_download_bytes / (1024 * 1024):g} MiB`"
        ),
        "",
        "## Résumé",
        "",
        (
            "| Émetteurs | Candidats | Téléchargés | Doublons | "
            "Ignorés trop gros | Erreurs |"
        ),
        "|---:|---:|---:|---:|---:|---:|",
        (
            f"| {report.stats.issuers_checked} | "
            f"{report.stats.candidates_found} | "
            f"{report.stats.downloaded} | "
            f"{report.stats.duplicates} | "
            f"{report.stats.skipped_too_large} | {report.stats.errors} |"
        ),
        "",
        "## Résumé par marché",
        "",
    ]
    
    if report.market.casefold() == "xetra":
        lines[0] = f"# Watch Germany Fallback - {report.started_at:%Y-%m-%d %H:%M:%S %Z}"
        lines.insert(1, "\n> [!IMPORTANT]\n> This is not an official OAM source. Documents require manual review.\n")
    if report.market_stats:
        lines.extend(
            [
                (
                    "| Marché | Émetteurs | Candidats | Téléchargés | "
                    "Doublons | Trop gros | Erreurs | Statut |"
                ),
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for market, stats in report.market_stats.items():
            market_status = "success" if stats.errors == 0 else "partial"
            lines.append(
                f"| {_markdown_text(market)} | {stats.issuers_checked} | "
                f"{stats.candidates_found} | {stats.downloaded} | "
                f"{stats.duplicates} | {stats.skipped_too_large} | "
                f"{stats.errors} | {market_status} |"
            )
        lines.append("")
    else:
        lines.extend(["Aucun marché supporté dans la watchlist.", ""])

    lines.extend(["## Request efficiency", ""])
    if report.source_efficiency:
        lines.extend(
            [
                (
                    "| Source | Marché | Mode | HTTP estimés | HTTP réels | "
                    "Notices scannées | Émetteurs matchés | Candidats matchés | "
                    "Détails visités | Cache dates | Candidats | Rejetés | "
                    "Téléchargements | Doublons | "
                    "Trop gros | Erreurs | Durée (s) |"
                ),
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for efficiency in report.source_efficiency.values():
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_text(efficiency.source),
                        _markdown_text(efficiency.market),
                        efficiency.mode,
                        str(efficiency.estimated_http_calls),
                        str(efficiency.http_calls),
                        str(efficiency.scanned_notices),
                        str(efficiency.matched_issuers),
                        str(efficiency.matched_candidates),
                        str(efficiency.details_visited),
                        str(efficiency.cache_hits),
                        str(efficiency.candidates),
                        str(efficiency.rejected_candidates),
                        str(efficiency.downloads),
                        str(efficiency.duplicates),
                        str(efficiency.skipped_too_large),
                        str(efficiency.errors),
                        f"{efficiency.elapsed_seconds:.3f}",
                    )
                )
                + " |"
            )
        lines.append("")
    else:
        lines.extend(["Aucune métrique de source.", ""])

    lines.extend(
        [
            "## Sociétés vérifiées",
            "",
        ]
    )
    if report.issuer_checks:
        lines.extend(
            [
                "| Marché | Société | ISIN | Candidats | Statut | Détail |",
                "|---|---|---|---:|---|---|",
            ]
        )
        for check in report.issuer_checks:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_text(check.issuer.market),
                        _markdown_text(check.issuer.name),
                        check.issuer.isin,
                        str(check.candidates),
                        check.status,
                        _markdown_text(check.detail),
                    )
                )
                + " |"
            )
        lines.append("")
    else:
        lines.extend(["Aucune société importée pour ce marché.", ""])

    lines.extend(["## Nouveaux documents", "", "Top 20 du run.", ""])
    lines.extend(_document_table(report.new_documents[:20]))
    lines.extend(["## Doublons", ""])
    lines.extend(_document_table(report.duplicates))
    lines.extend(["## Documents ignorés car trop gros", ""])
    lines.extend(_document_table(report.skipped_too_large))
    lines.extend(["## Erreurs par émetteur", ""])
    if report.errors:
        lines.extend(
            [
                "| Marché | Société | ISIN | Étape | Erreur |",
                "|---|---|---|---|---|",
            ]
        )
        for error in report.errors:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_text(
                            error.issuer.market if error.issuer else report.market
                        ),
                        _markdown_text(error.issuer.name if error.issuer else "run"),
                        error.issuer.isin if error.issuer else "",
                        _markdown_text(error.stage),
                        _markdown_text(error.message),
                    )
                )
                + " |"
            )
        lines.append("")
    else:
        lines.extend(["Aucune.", ""])

    lines.extend(["## Sources en degraded / unavailable", ""])
    if report.degraded_sources:
        lines.extend(["| Source | Erreur |", "|---|---|"])
        for source, error in sorted(report.degraded_sources.items()):
            lines.append(
                f"| {_markdown_text(source)} | {_markdown_text(error)} |"
            )
        lines.append("")
    else:
        lines.extend(["Aucune.", ""])

    if report.ssl_disabled_sources:
        lines.extend([
            "## Avertissements de sécurité TLS/SSL",
            "",
            "> [!WARNING]",
            "> La vérification TLS/SSL a été désactivée pour certaines requêtes durant ce run.",
            "> Cela présente un risque de sécurité (interception de données). Veuillez corriger la configuration de vos certificats ou du proxy local.",
            "",
            "Sources concernées :",
            ""
        ])
        for source in sorted(report.ssl_disabled_sources):
            lines.append(f"- `{source}` : Vérification SSL désactivée.")
        lines.append("")

    has_ambiguous_dates = False
    all_events = report.new_documents + report.duplicates + report.skipped_too_large + report.rejected_documents
    for event in all_events:
        if getattr(event.candidate, "date_confidence", None) == "low":
            has_ambiguous_dates = True
            break
            
    if has_ambiguous_dates:
        lines.extend([
            "## Avertissement sur les dates",
            "",
            "> [!WARNING]",
            "> Certains documents réglementaires (notamment de Suède/Finansinspektionen consultés par émetteur)",
            "> ont été importés sans date d'enregistrement/publication officielle.",
            "> Pour ces documents, la date de publication réelle n'est pas garantie et a été marquée comme `inconnue`,",
            "> tandis que la date de clôture de l'exercice a été estimée à partir de la période du rapport.",
            "",
        ])

    lines.extend(["## Candidats rejetés par filtre documentaire", ""])
    lines.extend(_rejected_document_table(report.rejected_documents))

    return "\n".join(lines)


def _report_path(
    reports_dir: Path,
    started_at: datetime,
    *,
    all_markets: bool,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = started_at
    prefix = "watch_all" if all_markets else "watch"
    while True:
        candidate = reports_dir / f"{prefix}_{timestamp:%Y%m%d_%H%M%S}.md"
        if not candidate.exists():
            return candidate
        timestamp += timedelta(seconds=1)


def write_watch_report(
    report: WatchReport,
    reports_dir: str | Path = "reports",
) -> Path:
    if report.market.casefold() == "xetra":
        path = Path(reports_dir) / f"germany_issuer_website_fallback_{report.started_at:%Y%m%d}.md"
    else:
        path = _report_path(
            Path(reports_dir),
            report.started_at,
            all_markets=report.market == MULTI_MARKET_SCOPE,
        )
    path.write_text(render_watch_report(report), encoding="utf-8")
    return path


def _source_group(market: str) -> str:
    key = market.casefold()
    if key == "euronext paris":
        return "france"
    if key == "oslo børs":
        return "oslo"
    if key in {
        "euronext milan",
        "euronext star milan",
        "euronext growth milan",
        "euronext miv milan",
    }:
        return "italy"
    if key == "euronext amsterdam":
        return "netherlands"
    if key in {"euronext brussels", "euronext growth brussels"}:
        return "belgium"
    if key == "euronext lisbon":
        return "portugal"
    if key == "euronext dublin":
        return "ireland"
    if key in {
        "bolsa de madrid",
        "bolsa de barcelona",
        "bolsa de bilbao",
        "bolsa de valencia",
        "bme growth",
        "bme scaleup",
    }:
        return "spain"
    if key in {
        "nasdaq stockholm",
        "nordic growth market",
    }:
        return "sweden"
    if key == "nasdaq copenhagen":
        return "denmark"
    if key == "nasdaq helsinki":
        return "finland"
    if key == "vienna stock exchange":
        return "austria"
    if key == "warsaw stock exchange":
        return "poland"
    if key == "prague stock exchange":
        return "czechia"
    if key == "zagreb stock exchange":
        return "croatia"
    if key == "ljubljana stock exchange":
        return "slovenia"
    if key == "riga stock exchange":
        return "latvia"
    if key == "vilnius stock exchange":
        return "lithuania"
    if key == "bratislava stock exchange":
        return "slovakia"
    if key == "bucharest stock exchange":
        return "romania"
    if key == "bulgarian stock exchange":
        return "bulgaria"
    return key


def _normalized_identity(value: object) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    ascii_value = "".join(
        character
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return re.sub(
        r"[^a-z0-9\u0400-\u04ff]+",
        " ",
        ascii_value.casefold(),
    ).strip()


def _candidate_match_score(
    issuer: Issuer,
    candidate: DocumentCandidate,
) -> float:
    metadata = candidate.metadata
    raw_isins = metadata.get("issuer_isins") or metadata.get("isins") or []
    if isinstance(raw_isins, str):
        raw_isins = [raw_isins]
    isins = {str(value).strip().upper() for value in raw_isins if value}
    if issuer.isin and issuer.isin.upper() in isins:
        return 100.0
    if isins:
        return 0.0

    expected = _normalized_identity(issuer.name)
    observed = _normalized_identity(
        metadata.get("issuer_name")
        or metadata.get("company")
        or metadata.get("issuing_institution")
    )
    if expected and observed:
        if expected == observed:
            return 90.0
        if metadata.get("strict_issuer_name_match"):
            aliases = {
                _normalized_identity(value)
                for value in metadata.get("issuer_aliases", [])
                if value
            }
            return 90.0 if expected in aliases else 0.0
        if expected in observed or observed in expected:
            return 84.0
        expected_words = set(expected.split())
        observed_words = set(observed.split())
        if expected_words and observed_words:
            overlap = len(expected_words & observed_words) / len(
                expected_words | observed_words
            )
            if overlap >= 0.6:
                return 70.0 + overlap * 10.0
        similarity = SequenceMatcher(None, expected, observed).ratio()
        if similarity >= 0.82:
            return 60.0 + similarity * 20.0

    symbol = _normalized_identity(issuer.symbol).replace(" ", "")
    candidate_symbol = _normalized_identity(
        metadata.get("issuer_symbol")
    ).replace(" ", "")
    if symbol and candidate_symbol and symbol == candidate_symbol:
        return 75.0
    combined = _normalized_identity(
        f"{metadata.get('issuer_name') or ''} {candidate.title}"
    )
    if len(symbol) >= 3 and re.search(
        rf"\b{re.escape(symbol)}\b",
        combined,
    ):
        return 55.0
    return 0.0


def _best_issuer_match(
    issuers: list[Issuer],
    candidate: DocumentCandidate,
) -> Issuer | None:
    best: tuple[float, Issuer] | None = None
    for issuer in issuers:
        score = _candidate_match_score(issuer, candidate)
        if best is None or score > best[0]:
            best = (score, issuer)
    return best[1] if best and best[0] >= 55.0 else None


def _store_resolution(
    database: Database,
    issuer: Issuer,
    candidate: DocumentCandidate,
) -> None:
    metadata = candidate.metadata
    key = issuer.market.casefold()
    if key == "euronext amsterdam":
        detail_url = metadata.get("detail_url")
        issuer_url = metadata.get("afm_issuer_url")
        record_id = metadata.get("afm_record_id")
        if all(isinstance(value, str) for value in (
            detail_url,
            issuer_url,
            record_id,
        )):
            database.store_netherlands_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                issuer_url=issuer_url,
                detail_url=detail_url,
                home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                afm_record_id=record_id,
            )
    elif key in {"euronext brussels", "euronext growth brussels"}:
        stori_url = metadata.get("stori_url")
        detail_url = metadata.get("detail_url")
        record_id = metadata.get("fsma_record_id")
        if all(isinstance(value, str) for value in (
            stori_url,
            detail_url,
            record_id,
        )):
            database.store_belgium_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                stori_url=stori_url,
                detail_url=detail_url,
                home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                fsma_record_id=record_id,
            )
    elif key == "euronext lisbon":
        sdi_url = metadata.get("cmvm_sdi_url")
        detail_url = metadata.get("detail_url")
        record_id = metadata.get("cmvm_record_id")
        if all(isinstance(value, str) for value in (
            sdi_url,
            detail_url,
            record_id,
        )):
            database.store_portugal_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                sdi_url=sdi_url,
                detail_url=detail_url,
                home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                cmvm_record_id=record_id,
            )
    elif key == "euronext dublin":
        direct_url = metadata.get("ireland_euronext_direct_url")
        oam_url = metadata.get("ireland_euronext_oam_url")
        detail_url = metadata.get("detail_url")
        record_id = metadata.get("ireland_record_id")
        if all(isinstance(value, str) for value in (
            direct_url,
            oam_url,
            detail_url,
            record_id,
        )):
            database.store_ireland_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                direct_url=direct_url,
                oam_url=oam_url,
                detail_url=detail_url,
                home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                record_id=record_id,
            )
    elif key in {
        "bolsa de madrid",
        "bolsa de barcelona",
        "bolsa de bilbao",
        "bolsa de valencia",
        "bme growth",
        "bme scaleup",
    }:
        cnmv_entity_url = metadata.get("detail_url")
        cnmv_nif = metadata.get("nif")
        cnmv_record_id = metadata.get("record_id")
        bme_company_url = metadata.get("spain_bme_company_url")
        if isinstance(cnmv_entity_url, str):
            database.store_spain_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                cnmv_entity_url=cnmv_entity_url,
                cnmv_nif=cnmv_nif,
                cnmv_record_id=cnmv_record_id,
                bme_company_url=bme_company_url,
                home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                pea_country_check=(
                    str(metadata["pea_country_check"])
                    if metadata.get("pea_country_check")
                    else "eu_candidate"
                ),
            )
    elif key in {
        "nasdaq stockholm",
        "nordic growth market",
    }:
        sweden_fi_issuer_url = metadata.get("detail_url") or metadata.get("sweden_fi_issuer_url")
        sweden_fi_record_id = metadata.get("record_id") or metadata.get("sweden_fi_record_id")
        sweden_fi_detail_url = metadata.get("detail_url") or metadata.get("sweden_fi_detail_url")
        sweden_nasdaq_company_url = metadata.get("sweden_nasdaq_company_url")
        if sweden_fi_issuer_url or sweden_fi_record_id or sweden_fi_detail_url or sweden_nasdaq_company_url:
            database.store_sweden_issuer_resolution(
                name=issuer.name,
                symbol=issuer.symbol,
                sweden_fi_issuer_url=sweden_fi_issuer_url,
                sweden_fi_record_id=sweden_fi_record_id,
                sweden_fi_detail_url=sweden_fi_detail_url,
                sweden_home_member_state=(
                    str(metadata["home_member_state"])
                    if metadata.get("home_member_state")
                    else None
                ),
                sweden_nasdaq_company_url=sweden_nasdaq_company_url,
                sweden_pea_country_check=(
                    str(metadata["pea_country_check"])
                    if metadata.get("pea_country_check")
                    else "eu_candidate"
                ),
            )
    elif key == "nasdaq copenhagen":
        detail_url = metadata.get("detail_url")
        record_id = metadata.get("record_id")
        database.store_denmark_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            denmark_dfsa_issuer_url=(
                str(metadata.get("denmark_dfsa_issuer_url"))
                if metadata.get("denmark_dfsa_issuer_url")
                else None
            ),
            denmark_dfsa_record_id=str(record_id) if record_id else None,
            denmark_dfsa_detail_url=str(detail_url) if detail_url else None,
            denmark_home_member_state=(
                str(metadata["denmark_home_member_state"])
                if metadata.get("denmark_home_member_state")
                else None
            ),
            denmark_nasdaq_company_url=(
                str(metadata["denmark_nasdaq_company_url"])
                if metadata.get("denmark_nasdaq_company_url")
                else None
            ),
            denmark_pea_country_check=(
                str(metadata["denmark_pea_country_check"])
                if metadata.get("denmark_pea_country_check")
                else "eu_candidate"
            ),
        )
    elif key == "nasdaq helsinki":
        database.store_finland_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            finland_oam_company_id=metadata.get("record_id") or metadata.get("finland_oam_company_id"),
            finland_oam_issuer_url=metadata.get("detail_url") or metadata.get("finland_oam_issuer_url"),
            finland_oam_detail_url=metadata.get("detail_url") or metadata.get("finland_oam_detail_url"),
            finland_home_member_state=(
                str(metadata["home_member_state"])
                if metadata.get("home_member_state")
                else None
            ),
            finland_nasdaq_company_url=metadata.get("finland_nasdaq_company_url"),
            finland_pea_country_check=(
                str(metadata["pea_country_check"])
                if metadata.get("pea_country_check")
                else "eu_candidate"
            ),
        )
    elif key == "vienna stock exchange":
        database.store_austria_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            austria_oekb_oam_id=(
                str(metadata["austria_oekb_oam_id"])
                if metadata.get("austria_oekb_oam_id")
                else None
            ),
            austria_oekb_oam_issuer_url=(
                str(metadata["austria_oekb_oam_issuer_url"])
                if metadata.get("austria_oekb_oam_issuer_url")
                else None
            ),
            austria_oekb_oam_detail_url=(
                str(metadata["austria_oekb_oam_detail_url"])
                if metadata.get("austria_oekb_oam_detail_url")
                else None
            ),
            austria_home_member_state=(
                str(metadata["home_member_state"])
                if metadata.get("home_member_state")
                else "Austria"
            ),
            austria_pea_country_check=(
                str(metadata["pea_country_check"])
                if metadata.get("pea_country_check")
                else "eu_candidate"
            ),
        )
    elif key == "warsaw stock exchange":
        database.store_poland_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["knf_oam_issuer_url"])
                if metadata.get("knf_oam_issuer_url")
                else None
            ),
            detail_url=(
                str(metadata["knf_oam_detail_url"])
                if metadata.get("knf_oam_detail_url")
                else None
            ),
            source_record_id=(
                str(metadata["knf_oam_record_id"])
                if metadata.get("knf_oam_record_id")
                else None
            ),
            home_member_state=(
                str(metadata["home_member_state"])
                if metadata.get("home_member_state")
                else "Poland"
            ),
        )
    elif key == "zagreb stock exchange":
        database.store_croatia_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["hanfa_srpi_url"])
                if metadata.get("hanfa_srpi_url")
                else None
            ),
            detail_url=(
                str(metadata["parent_page_url"])
                if metadata.get("parent_page_url")
                else None
            ),
            source_record_id=(
                str(metadata["file_id"])
                if metadata.get("file_id")
                else None
            ),
            home_member_state="Croatia",
        )
    elif key == "ljubljana stock exchange":
        database.store_slovenia_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["slovenia_oam_url"])
                if metadata.get("slovenia_oam_url")
                else None
            ),
            detail_url=(
                str(metadata["detail_url"])
                if metadata.get("detail_url")
                else None
            ),
            source_record_id=(
                str(metadata["record_id"])
                if metadata.get("record_id")
                else None
            ),
            home_member_state="Slovenia",
        )
    elif key == "riga stock exchange":
        database.store_latvia_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["latvia_oam_url"])
                if metadata.get("latvia_oam_url")
                else None
            ),
            detail_url=(
                str(metadata["detail_url"])
                if metadata.get("detail_url")
                else None
            ),
            source_record_id=(
                str(metadata["record_id"])
                if metadata.get("record_id")
                else None
            ),
            home_member_state="Latvia",
        )
    elif key == "vilnius stock exchange":
        database.store_lithuania_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["lithuania_oam_url"])
                if metadata.get("lithuania_oam_url")
                else None
            ),
            detail_url=(
                str(metadata["detail_url"])
                if metadata.get("detail_url")
                else None
            ),
            source_record_id=(
                str(metadata["record_id"])
                if metadata.get("record_id")
                else None
            ),
            home_member_state="Lithuania",
        )
    elif key == "bratislava stock exchange":
        database.store_slovakia_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["slovakia_nbs_ceri_url"])
                if metadata.get("slovakia_nbs_ceri_url")
                else None
            ),
            detail_url=(
                str(metadata["parent_page_url"])
                if metadata.get("parent_page_url")
                else None
            ),
            source_record_id=(
                str(metadata["record_id"])
                if metadata.get("record_id")
                else None
            ),
            home_member_state="Slovakia",
        )
    elif key == "bucharest stock exchange":
        database.store_romania_issuer_resolution(
            name=issuer.name,
            symbol=issuer.symbol,
            source_name=(
                str(metadata["issuer_name"])
                if metadata.get("issuer_name")
                else None
            ),
            source_url=(
                str(metadata["romania_asf_oam_url"])
                if metadata.get("romania_asf_oam_url")
                else None
            ),
            detail_url=(
                str(metadata["parent_page_url"])
                if metadata.get("parent_page_url")
                else None
            ),
            source_record_id=(
                str(metadata["record_id"])
                if metadata.get("record_id")
                else None
            ),
            home_member_state="Romania",
        )


def _run_watch_legacy(
    database: Database,
    settings: Settings,
    *,
    market: str | None,
    since: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    reports_dir: str | Path = "reports",
    session_factory: Callable[..., requests.Session] | None = None,
    connector_factory: Callable[..., Connector | None] | None = None,
    downloader_factory: Callable[..., DocumentDownloader] | None = None,
    now: Callable[[], datetime] | None = None,
    notify_email: str | None = None,
) -> WatchOutcome:
    if limit is not None and limit < 1:
        raise ValueError("--limit doit être supérieur ou égal à 1")

    normalized_market = normalize_market(market) if market else None
    scope = normalized_market or MULTI_MARKET_SCOPE
    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    run_id = database.create_watch_run(
        scope,
        started_at=started_at.isoformat(timespec="seconds"),
    )
    stats = WatchStats()
    market_stats: dict[str, WatchStats] = {}
    issuer_checks: list[IssuerCheck] = []
    new_documents: list[DocumentEvent] = []
    duplicates: list[DocumentEvent] = []
    skipped_too_large: list[DocumentEvent] = []
    errors: list[IssuerError] = []
    degraded_sources: dict[str, str] = {}
    session: requests.Session | None = None
    status = "running"

    try:
        make_session = session_factory or build_http_session
        session = make_session(
            retries=settings.http_retries,
            backoff_factor=settings.http_backoff_factor,
            user_agent=settings.user_agent,
            verify=settings.http_verify_ssl,
        )
        make_connector = connector_factory or connector_for_market
        make_downloader = downloader_factory or DocumentDownloader
        downloader = make_downloader(
            database=database,
            session=session,
            data_dir=settings.data_dir,
            timeout=settings.http_timeout_seconds,
            max_download_bytes=settings.max_download_bytes,
        )

        if normalized_market:
            issuers = database.list_issuers(normalized_market)
            market_stats[normalized_market] = WatchStats()
        else:
            issuers = [
                issuer
                for issuer in database.list_issuers()
                if is_supported_market(issuer.market)
            ]
            issuers.sort(
                key=lambda issuer: (
                    _MARKET_ORDER.get(
                        issuer.market.casefold(),
                        len(_MARKET_ORDER),
                    ),
                    issuer.name.casefold(),
                    issuer.isin,
                )
            )
            for issuer in issuers:
                market_stats.setdefault(issuer.market, WatchStats())

        processed_candidates = 0
        seen_urls: set[str] = set()
        connectors: dict[str, Connector | None] = {}
        connector_errors: dict[str, str] = {}

        for issuer in issuers:
            stats.issuers_checked += 1
            current_stats = market_stats.setdefault(issuer.market, WatchStats())
            current_stats.issuers_checked += 1
            check = IssuerCheck(issuer=issuer)
            issuer_checks.append(check)

            market_key = issuer.market.casefold()
            if market_key not in connectors:
                try:
                    connectors[market_key] = make_connector(
                        issuer.market,
                        settings=settings,
                        session=session,
                    )
                except Exception as exc:
                    connectors[market_key] = None
                    connector_errors[market_key] = str(exc)
                    database.set_source_state(
                        source=f"{issuer.market}:connector",
                        market=issuer.market,
                        state=ConnectorState.UNAVAILABLE.value,
                        error=str(exc),
                        context="watch",
                    )
                    LOGGER.exception(
                        "Watch: initialisation du connecteur échouée pour %s",
                        issuer.market,
                    )
            connector = connectors[market_key]
            if connector is None:
                message = connector_errors.get(
                    market_key,
                    f"Aucun connecteur disponible pour {issuer.market}",
                )
                source_label = f"{issuer.market} / connector"
                degraded_sources[source_label] = message
                check.status = "error"
                check.detail = message
                errors.append(IssuerError(issuer, "source", message))
                stats.errors += 1
                current_stats.errors += 1
                database.add_operational_event(
                    watch_run_id=run_id,
                    issuer=issuer,
                    source=f"{issuer.market}:connector",
                    event_status="error",
                    message=message,
                )
                continue

            try:
                found = connector.search_documents(issuer)
            except Exception as exc:
                message = str(exc)
                source_label = connector.source_name
                if normalized_market is None:
                    source_label = f"{issuer.market} / {source_label}"
                degraded_sources[source_label] = message
                check.status = "error"
                check.detail = message
                errors.append(IssuerError(issuer, "recherche", message))
                stats.errors += 1
                current_stats.errors += 1
                database.add_operational_event(
                    watch_run_id=run_id,
                    issuer=issuer,
                    source=connector.source_name,
                    event_status="error",
                    message=message,
                )
                database.set_source_state(
                    source=connector.source_name,
                    market=issuer.market,
                    state=ConnectorState.DEGRADED.value,
                    error=message,
                    context="watch",
                )
                LOGGER.error(
                    "Watch: recherche échouée pour %s (%s): %s",
                    issuer.name,
                    issuer.isin,
                    exc,
                )
                continue

            if connector.state != ConnectorState.READY:
                message = connector.last_error or "Source degraded"
                source_label = connector.source_name
                if normalized_market is None:
                    source_label = f"{issuer.market} / {source_label}"
                degraded_sources[source_label] = message
                check.status = connector.state.value
                check.detail = message
                errors.append(IssuerError(issuer, "source", message))
                stats.errors += 1
                current_stats.errors += 1
                database.set_source_state(
                    source=connector.source_name,
                    market=issuer.market,
                    state=connector.state.value,
                    error=message,
                    context="watch",
                )
                database.add_operational_event(
                    watch_run_id=run_id,
                    issuer=issuer,
                    source=connector.source_name,
                    event_status="error",
                    message=message,
                )
                continue

            database.set_source_state(
                source=connector.source_name,
                market=issuer.market,
                state=ConnectorState.READY.value,
                error=None,
                context="watch",
            )

            if (
                issuer.market.casefold() == "euronext amsterdam"
                and found
            ):
                metadata = found[0].metadata
                detail_url = metadata.get("detail_url")
                issuer_url = metadata.get("afm_issuer_url")
                record_id = metadata.get("afm_record_id")
                if (
                    isinstance(detail_url, str)
                    and isinstance(issuer_url, str)
                    and isinstance(record_id, str)
                ):
                    database.store_netherlands_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        issuer_url=issuer_url,
                        detail_url=detail_url,
                        home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        afm_record_id=record_id,
                    )
            if (
                issuer.market.casefold()
                in {"euronext brussels", "euronext growth brussels"}
                and found
            ):
                metadata = found[0].metadata
                stori_url = metadata.get("stori_url")
                detail_url = metadata.get("detail_url")
                record_id = metadata.get("fsma_record_id")
                if (
                    isinstance(stori_url, str)
                    and isinstance(detail_url, str)
                    and isinstance(record_id, str)
                ):
                    database.store_belgium_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        stori_url=stori_url,
                        detail_url=detail_url,
                        home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        fsma_record_id=record_id,
                    )
            if (
                issuer.market.casefold() == "euronext lisbon"
                and found
            ):
                metadata = found[0].metadata
                sdi_url = metadata.get("cmvm_sdi_url")
                detail_url = metadata.get("detail_url")
                record_id = metadata.get("cmvm_record_id")
                if (
                    isinstance(sdi_url, str)
                    and isinstance(detail_url, str)
                    and isinstance(record_id, str)
                ):
                    database.store_portugal_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        sdi_url=sdi_url,
                        detail_url=detail_url,
                        home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        cmvm_record_id=record_id,
                    )
            if (
                issuer.market.casefold() == "euronext dublin"
                and found
            ):
                metadata = found[0].metadata
                direct_url = metadata.get("ireland_euronext_direct_url")
                oam_url = metadata.get("ireland_euronext_oam_url")
                detail_url = metadata.get("detail_url")
                record_id = metadata.get("ireland_record_id")
                if (
                    isinstance(direct_url, str)
                    and isinstance(oam_url, str)
                    and isinstance(detail_url, str)
                    and isinstance(record_id, str)
                ):
                    database.store_ireland_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        direct_url=direct_url,
                        oam_url=oam_url,
                        detail_url=detail_url,
                        home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        record_id=record_id,
                    )
            if (
                issuer.market.casefold() in {
                    "bolsa de madrid",
                    "bolsa de barcelona",
                    "bolsa de bilbao",
                    "bolsa de valencia",
                    "bme growth",
                    "bme scaleup",
                }
                and found
            ):
                metadata = found[0].metadata
                cnmv_entity_url = metadata.get("detail_url")
                cnmv_nif = metadata.get("nif")
                cnmv_record_id = metadata.get("record_id")
                bme_company_url = metadata.get("spain_bme_company_url")
                if isinstance(cnmv_entity_url, str):
                    database.store_spain_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        cnmv_entity_url=cnmv_entity_url,
                        cnmv_nif=cnmv_nif,
                        cnmv_record_id=cnmv_record_id,
                        bme_company_url=bme_company_url,
                        home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        pea_country_check=(
                            str(metadata["pea_country_check"])
                            if metadata.get("pea_country_check")
                            else "eu_candidate"
                        ),
                    )
            if (
                issuer.market.casefold() in {
                    "nasdaq stockholm",
                    "nordic growth market",
                }
                and found
            ):
                metadata = found[0].metadata
                sweden_fi_issuer_url = metadata.get("detail_url") or metadata.get("sweden_fi_issuer_url")
                sweden_fi_record_id = metadata.get("record_id") or metadata.get("sweden_fi_record_id")
                sweden_fi_detail_url = metadata.get("detail_url") or metadata.get("sweden_fi_detail_url")
                sweden_nasdaq_company_url = metadata.get("sweden_nasdaq_company_url")
                if sweden_fi_issuer_url or sweden_fi_record_id or sweden_fi_detail_url or sweden_nasdaq_company_url:
                    database.store_sweden_issuer_resolution(
                        name=issuer.name,
                        symbol=issuer.symbol,
                        sweden_fi_issuer_url=sweden_fi_issuer_url,
                        sweden_fi_record_id=sweden_fi_record_id,
                        sweden_fi_detail_url=sweden_fi_detail_url,
                        sweden_home_member_state=(
                            str(metadata["home_member_state"])
                            if metadata.get("home_member_state")
                            else None
                        ),
                        sweden_nasdaq_company_url=sweden_nasdaq_company_url,
                        sweden_pea_country_check=(
                            str(metadata["pea_country_check"])
                            if metadata.get("pea_country_check")
                            else "eu_candidate"
                        ),
                    )
            if (
                issuer.market.casefold() == "nasdaq helsinki"
                and found
            ):
                metadata = found[0].metadata
                database.store_finland_issuer_resolution(
                    name=issuer.name,
                    symbol=issuer.symbol,
                    finland_oam_company_id=metadata.get("record_id") or metadata.get("finland_oam_company_id"),
                    finland_oam_issuer_url=metadata.get("detail_url") or metadata.get("finland_oam_issuer_url"),
                    finland_oam_detail_url=metadata.get("detail_url") or metadata.get("finland_oam_detail_url"),
                    finland_home_member_state=(
                        str(metadata["home_member_state"])
                        if metadata.get("home_member_state")
                        else None
                    ),
                    finland_nasdaq_company_url=metadata.get("finland_nasdaq_company_url"),
                    finland_pea_country_check=(
                        str(metadata["pea_country_check"])
                        if metadata.get("pea_country_check")
                        else "eu_candidate"
                    ),
                )

            candidates, _ = _filter_candidates(found, since=since)
            check.candidates = len(candidates)
            stats.candidates_found += len(candidates)
            current_stats.candidates_found += len(candidates)

            for candidate in candidates:
                if limit is not None and processed_candidates >= limit:
                    break
                processed_candidates += 1

                existing = database.get_document_by_source_url(candidate.url)
                if existing is not None or candidate.url in seen_urls:
                    stats.duplicates += 1
                    current_stats.duplicates += 1
                    duplicates.append(
                        DocumentEvent(
                            issuer=issuer,
                            candidate=candidate,
                            result="duplicate",
                            path=(
                                Path(existing["local_path"])
                                if existing is not None
                                else None
                            ),
                            sha256=(
                                existing["sha256"]
                                if existing is not None
                                else None
                            ),
                            reason="URL connue",
                        )
                    )
                    continue

                seen_urls.add(candidate.url)
                if dry_run:
                    new_documents.append(
                        DocumentEvent(
                            issuer=issuer,
                            candidate=candidate,
                            result="dry-run",
                            reason="non téléchargé",
                        )
                    )
                    continue

                try:
                    result = downloader.download(issuer, candidate)
                except DownloadError as exc:
                    message = str(exc)
                    stats.errors += 1
                    current_stats.errors += 1
                    check.status = "error"
                    check.detail = message
                    errors.append(
                        IssuerError(issuer, "téléchargement", message)
                    )
                    database.add_operational_event(
                        watch_run_id=run_id,
                        issuer=issuer,
                        candidate=candidate,
                        event_status="error",
                        message=message,
                    )
                    LOGGER.error("Watch: %s", message)
                    continue
                except Exception as exc:
                    message = str(exc)
                    stats.errors += 1
                    current_stats.errors += 1
                    check.status = "error"
                    check.detail = message
                    errors.append(
                        IssuerError(issuer, "téléchargement", message)
                    )
                    database.add_operational_event(
                        watch_run_id=run_id,
                        issuer=issuer,
                        candidate=candidate,
                        event_status="error",
                        message=message,
                    )
                    LOGGER.exception(
                        "Watch: erreur inattendue pour %s",
                        candidate.url,
                    )
                    continue

                if result.status == "downloaded":
                    stats.downloaded += 1
                    current_stats.downloaded += 1
                    new_documents.append(
                        DocumentEvent(
                            issuer=issuer,
                            candidate=candidate,
                            result="downloaded",
                            path=result.path,
                            sha256=result.sha256,
                        )
                    )
                elif result.status == "skipped_too_large":
                    stats.skipped_too_large += 1
                    current_stats.skipped_too_large += 1
                    event = DocumentEvent(
                        issuer=issuer,
                        candidate=candidate,
                        result="skipped_too_large",
                        reason=result.message,
                    )
                    skipped_too_large.append(event)
                    database.add_operational_event(
                        watch_run_id=run_id,
                        issuer=issuer,
                        candidate=candidate,
                        event_status="skipped_too_large",
                        file_size=result.file_size,
                        message=result.message,
                    )
                else:
                    stats.duplicates += 1
                    current_stats.duplicates += 1
                    duplicates.append(
                        DocumentEvent(
                            issuer=issuer,
                            candidate=candidate,
                            result="duplicate",
                            path=result.path,
                            sha256=result.sha256,
                            reason="SHA256 connu",
                        )
                    )

        status = "success" if stats.errors == 0 else "partial"
    except Exception as exc:
        status = "failed"
        stats.errors += 1
        errors.append(IssuerError(None, "run", str(exc)))
        database.add_operational_event(
            watch_run_id=run_id,
            event_status="error",
            source="watch",
            message=str(exc),
        )
        LOGGER.exception("Watch interrompu par une erreur globale")
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                LOGGER.exception("Impossible de fermer la session HTTP")

    ended_at = clock()
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    report = WatchReport(
        run_id=run_id,
        market=scope,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        since=since,
        limit=limit,
        dry_run=dry_run,
        stats=stats,
        max_download_bytes=settings.max_download_bytes,
        market_stats=market_stats,
        issuer_checks=issuer_checks,
        new_documents=new_documents,
        duplicates=duplicates,
        skipped_too_large=skipped_too_large,
        errors=errors,
        degraded_sources=degraded_sources,
        ssl_disabled_sources=set(getattr(session, "ssl_disabled_sources", ())) if session is not None else set(),
    )
    report_path = write_watch_report(report, reports_dir)
    database.record_watch_market_stats(run_id, market_stats)
    database.finish_watch_run(
        run_id,
        status=status,
        issuers_checked=stats.issuers_checked,
        candidates_found=stats.candidates_found,
        downloaded=stats.downloaded,
        duplicates=stats.duplicates,
        skipped_too_large=stats.skipped_too_large,
        errors=stats.errors,
        report_path=str(report_path),
        ended_at=ended_at.isoformat(timespec="seconds"),
    )
    notification_path = None
    if notify_email:
        notification_path = write_notification_email(
            recipient=notify_email,
            run_status=status,
            summary={
                "émetteurs": stats.issuers_checked,
                "candidats": stats.candidates_found,
                "téléchargés": stats.downloaded,
                "doublons": stats.duplicates,
                "ignorés car trop gros": stats.skipped_too_large,
                "erreurs": stats.errors,
            },
            new_documents=new_documents,
            source_errors=degraded_sources,
            report_path=report_path,
            generated_at=ended_at,
        )
    LOGGER.info(
        "Watch terminé: statut=%s, téléchargés=%d, doublons=%d, "
        "erreurs=%d, rapport=%s",
        status,
        stats.downloaded,
        stats.duplicates,
        stats.errors,
        report_path,
    )
    return WatchOutcome(
        run_id=run_id,
        status=status,
        stats=stats,
        market_stats=market_stats,
        report_path=report_path,
        notification_path=notification_path,
    )


def run_watch(
    database: Database,
    settings: Settings,
    *,
    market: str | None,
    since: date | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    reports_dir: str | Path = "reports",
    session_factory: Callable[..., requests.Session] | None = None,
    connector_factory: Callable[..., Connector | None] | None = None,
    downloader_factory: Callable[..., DocumentDownloader] | None = None,
    now: Callable[[], datetime] | None = None,
    notify_email: str | None = None,
    lookback_days: int = 7,
    max_candidates_per_source: int = 1000,
    max_documents_per_run: int = 100,
    confirm_large_run: bool = False,
    backfill: bool = False,
    issuer_mode: bool = False,
    include_regulatory_news: bool = False,
    issuer_website_fallback: bool = False,
) -> WatchOutcome:
    if limit is not None and limit < 1:
        raise ValueError("--limit doit être supérieur ou égal à 1")
    if lookback_days < 1:
        raise ValueError("--lookback-days doit être supérieur ou égal à 1")
    
    normalized_market = normalize_market(market) if market else None
    if normalized_market == "Xetra" and not issuer_website_fallback:
        raise ValueError("Le marché Xetra nécessite l'option --issuer-website-fallback")
    if max_candidates_per_source < 1:
        raise ValueError(
            "--max-candidates-per-source doit être supérieur ou égal à 1"
        )
    if max_documents_per_run < 1:
        raise ValueError(
            "--max-documents-per-run doit être supérieur ou égal à 1"
        )

    scope = normalized_market or MULTI_MARKET_SCOPE
    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    heavy_mode = backfill or issuer_mode
    effective_lookback_days = (
        max(lookback_days, settings.croatia_hanfa_srpi_lookback_days)
        if normalized_market == "Zagreb Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.slovenia_oam_lookback_days)
        if normalized_market == "Ljubljana Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.latvia_oam_lookback_days)
        if normalized_market == "Riga Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.lithuania_oam_lookback_days)
        if normalized_market == "Vilnius Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.slovakia_nbs_ceri_lookback_days)
        if normalized_market == "Bratislava Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.romania_asf_oam_lookback_days)
        if normalized_market == "Bucharest Stock Exchange" and not heavy_mode
        else max(lookback_days, settings.bulgaria_bse_x3news_lookback_days)
        if normalized_market == "Bulgarian Stock Exchange" and not heavy_mode
        else lookback_days
    )
    effective_since = (
        since
        if heavy_mode
        else since
        or (started_at.date() - timedelta(days=effective_lookback_days))
    )
    document_limit = limit or max_documents_per_run
    execution_mode = (
        "backfill" if backfill else "issuer-mode" if issuer_mode else "source-first"
    )

    run_id = database.create_watch_run(
        scope,
        started_at=started_at.isoformat(timespec="seconds"),
    )
    stats = WatchStats()
    market_stats: dict[str, WatchStats] = {}
    issuer_checks: list[IssuerCheck] = []
    new_documents: list[DocumentEvent] = []
    duplicates: list[DocumentEvent] = []
    skipped_too_large: list[DocumentEvent] = []
    rejected_documents: list[DocumentEvent] = []
    errors: list[IssuerError] = []
    degraded_sources: dict[str, str] = {}
    source_efficiency: dict[str, SourceEfficiency] = {}
    status = "running"
    tracker: RequestCountingSession | None = None
    session: requests.Session | None = None
    processed_documents = 0
    seen_urls: set[str] = set()

    if normalized_market:
        issuers = database.list_issuers(normalized_market)
    else:
        issuers = [
            issuer
            for issuer in database.list_issuers()
            if is_supported_market(issuer.market)
        ]
        if issuer_website_fallback:
            germany_issuers = database.list_issuers("Xetra")
            issuers.extend(germany_issuers)
    issuers.sort(
        key=lambda issuer: (
            _MARKET_ORDER.get(issuer.market.casefold(), len(_MARKET_ORDER)),
            issuer.name.casefold(),
            issuer.isin,
        )
    )
    groups: dict[str, list[Issuer]] = {}
    checks_by_isin: dict[str, IssuerCheck] = {}
    for issuer in issuers:
        groups.setdefault(_source_group(issuer.market), []).append(issuer)
        current_stats = market_stats.setdefault(issuer.market, WatchStats())
        current_stats.issuers_checked += 1
        stats.issuers_checked += 1
        check = IssuerCheck(issuer=issuer)
        issuer_checks.append(check)
        checks_by_isin[issuer.isin] = check
        is_spain = issuer.market.casefold() in {
            "bolsa de madrid",
            "bolsa de barcelona",
            "bolsa de bilbao",
            "bolsa de valencia",
            "bme growth",
            "bme scaleup",
        }
        if is_spain and not issuer.spain_home_member_state:
            LOGGER.warning("Warning: Société espagnole cotée mais domicile non confirmé pour %s (%s)", issuer.name, issuer.isin)
        is_sweden = issuer.market.casefold() in {
            "nasdaq stockholm",
            "nordic growth market",
        }
        if is_sweden and not issuer.sweden_home_member_state:
            LOGGER.warning("Warning: Société suédoise cotée mais domicile non confirmé pour %s (%s)", issuer.name, issuer.isin)
        if (
            issuer.market.casefold() == "nasdaq copenhagen"
            and not issuer.denmark_home_member_state
        ):
            LOGGER.warning(
                "Warning: société cotée à Copenhague mais domicile non confirmé "
                "pour %s (%s)",
                issuer.name,
                issuer.isin,
            )

    def record_candidates(
        issuer: Issuer,
        candidates: list[DocumentCandidate],
        efficiency: SourceEfficiency,
    ) -> None:
        count = len(candidates)
        stats.candidates_found += count
        market_stats[issuer.market].candidates_found += count
        checks_by_isin[issuer.isin].candidates += count
        efficiency.candidates += count

    def record_error(
        issuer: Issuer | None,
        stage: str,
        message: str,
        efficiency: SourceEfficiency,
        *,
        source_state: str = ConnectorState.DEGRADED.value,
    ) -> None:
        nonlocal status
        errors.append(IssuerError(issuer, stage, message))
        stats.errors += 1
        efficiency.errors += 1
        if issuer is not None:
            market_stats[issuer.market].errors += 1
            check = checks_by_isin[issuer.isin]
            check.status = "error"
            check.detail = message
        degraded_sources[efficiency.source] = message
        database.set_source_state(
            source=efficiency.source,
            market=efficiency.market,
            state=source_state,
            error=message,
            context="watch",
        )
        database.add_operational_event(
            watch_run_id=run_id,
            issuer=issuer,
            source=efficiency.source,
            event_status="error",
            message=message,
        )
        if source_state == ConnectorState.UNAVAILABLE.value:
            status = "partial"

    def process_candidate(
        issuer: Issuer,
        candidate: DocumentCandidate,
        efficiency: SourceEfficiency,
        downloader: DocumentDownloader,
    ) -> None:
        nonlocal processed_documents
        if processed_documents >= document_limit:
            return
        processed_documents += 1
        current_stats = market_stats[issuer.market]
        check = checks_by_isin[issuer.isin]
        existing = database.get_document_by_source_url(candidate.url)
        if existing is not None or candidate.url in seen_urls:
            stats.duplicates += 1
            current_stats.duplicates += 1
            efficiency.duplicates += 1
            duplicates.append(
                DocumentEvent(
                    issuer=issuer,
                    candidate=candidate,
                    result="duplicate",
                    path=(
                        Path(existing["local_path"])
                        if existing is not None
                        else None
                    ),
                    sha256=(
                        existing["sha256"] if existing is not None else None
                    ),
                    reason="URL connue",
                )
            )
            return

        seen_urls.add(candidate.url)
        if dry_run:
            new_documents.append(
                DocumentEvent(
                    issuer=issuer,
                    candidate=candidate,
                    result="dry-run",
                    reason="non téléchargé",
                )
            )
            return

        try:
            result = downloader.download(issuer, candidate)
        except DownloadError as exc:
            message = str(exc)
            stats.errors += 1
            current_stats.errors += 1
            efficiency.errors += 1
            check.status = "error"
            check.detail = message
            errors.append(IssuerError(issuer, "téléchargement", message))
            database.add_operational_event(
                watch_run_id=run_id,
                issuer=issuer,
                candidate=candidate,
                event_status="error",
                message=message,
            )
            return
        except Exception as exc:
            message = str(exc)
            stats.errors += 1
            current_stats.errors += 1
            efficiency.errors += 1
            check.status = "error"
            check.detail = message
            errors.append(IssuerError(issuer, "téléchargement", message))
            database.add_operational_event(
                watch_run_id=run_id,
                issuer=issuer,
                candidate=candidate,
                event_status="error",
                message=message,
            )
            LOGGER.exception(
                "Watch: erreur inattendue pour %s",
                candidate.url,
            )
            return

        if result.status == "downloaded":
            stats.downloaded += 1
            current_stats.downloaded += 1
            efficiency.downloads += 1
            new_documents.append(
                DocumentEvent(
                    issuer=issuer,
                    candidate=candidate,
                    result="downloaded",
                    path=result.path,
                    sha256=result.sha256,
                )
            )
        elif result.status == "skipped_too_large":
            stats.skipped_too_large += 1
            current_stats.skipped_too_large += 1
            efficiency.skipped_too_large += 1
            skipped_too_large.append(
                DocumentEvent(
                    issuer=issuer,
                    candidate=candidate,
                    result="skipped_too_large",
                    reason=result.message,
                )
            )
            database.add_operational_event(
                watch_run_id=run_id,
                issuer=issuer,
                candidate=candidate,
                event_status="skipped_too_large",
                file_size=result.file_size,
                message=result.message,
            )
        else:
            stats.duplicates += 1
            current_stats.duplicates += 1
            efficiency.duplicates += 1
            duplicates.append(
                DocumentEvent(
                    issuer=issuer,
                    candidate=candidate,
                    result="duplicate",
                    path=result.path,
                    sha256=result.sha256,
                    reason="SHA256 connu",
                )
            )

    try:
        make_session = session_factory or build_http_session
        raw_session = make_session(
            retries=settings.http_retries,
            backoff_factor=settings.http_backoff_factor,
            user_agent=settings.user_agent,
            verify=settings.http_verify_ssl,
        )
        tracker = RequestCountingSession(
            raw_session,
            max_requests=500,
            allow_large_run=confirm_large_run,
        )
        session = tracker
        make_connector = connector_factory or connector_for_market
        make_downloader = downloader_factory or DocumentDownloader
        downloader = make_downloader(
            database=database,
            session=tracker,
            data_dir=settings.data_dir,
            timeout=settings.http_timeout_seconds,
            max_download_bytes=settings.max_download_bytes,
        )

        connectors: dict[str, Connector] = {}
        source_candidate_limits: dict[str, int] = {}
        planned_requests = 0
        for group, group_issuers in groups.items():
            factory_market = group_issuers[0].market
            try:
                if factory_market.casefold() == "xetra":
                    from connectors.germany_fallback import GermanyIssuerWebsiteFallbackConnector
                    connector = GermanyIssuerWebsiteFallbackConnector(
                        session=tracker,
                        timeout=settings.http_timeout_seconds,
                        database=database,
                    )
                else:
                    connector = make_connector(
                        factory_market,
                        settings=settings,
                        session=tracker,
                    )
            except Exception as exc:
                efficiency = SourceEfficiency(
                    source=f"{group}:connector",
                    market=factory_market,
                    mode=execution_mode,
                )
                source_efficiency[group] = efficiency
                record_error(
                    group_issuers[0],
                    "source",
                    str(exc),
                    efficiency,
                    source_state=ConnectorState.UNAVAILABLE.value,
                )
                continue
            if connector is None:
                efficiency = SourceEfficiency(
                    source=f"{group}:connector",
                    market=factory_market,
                    mode=execution_mode,
                )
                source_efficiency[group] = efficiency
                record_error(
                    group_issuers[0],
                    "source",
                    f"Aucun connecteur disponible pour {factory_market}",
                    efficiency,
                    source_state=ConnectorState.UNAVAILABLE.value,
                )
                continue
            connectors[group] = connector
            use_source_first = (
                not heavy_mode and connector.supports_source_first
            )
            mode = (
                "source-first"
                if use_source_first
                else "backfill"
                if backfill
                else "issuer-mode"
                if issuer_mode
                else "issuer-fallback"
            )
            efficiency = SourceEfficiency(
                source=connector.source_name,
                market=factory_market,
                mode=mode,
            )
            source_candidate_limit = max_candidates_per_source
            if factory_market.casefold() == "bulgarian stock exchange":
                source_candidate_limit = min(
                    max_candidates_per_source,
                    settings.bulgaria_bse_x3news_max_candidates_per_source,
                )
            if use_source_first:
                efficiency.estimated_http_calls = (
                    connector.estimate_recent_http_requests(
                        since=effective_since,
                        limit=source_candidate_limit,
                    )
                )
                if getattr(connector, "requires_watchlist_queries", False):
                    efficiency.estimated_http_calls += sum(
                        connector.estimate_issuer_http_requests(issuer)
                        for issuer in group_issuers
                    )
            else:
                efficiency.estimated_http_calls = sum(
                    connector.estimate_issuer_http_requests(issuer)
                    for issuer in group_issuers
                )
            planned_requests += efficiency.estimated_http_calls
            source_efficiency[group] = efficiency
            source_candidate_limits[group] = source_candidate_limit

        if planned_requests > 500 and not confirm_large_run:
            raise RequestLimitExceeded(
                f"Le run prévoit environ {planned_requests} appels HTTP, "
                "au-dessus du garde-fou de 500. Réduire le périmètre ou "
                "relancer avec --confirm-large-run."
            )

        for group, group_issuers in groups.items():
            connector = connectors.get(group)
            efficiency = source_efficiency[group]
            if connector is None:
                continue
            source_started = time.monotonic()
            matched_issuers: set[str] = set()
            recorded_source_error = False
            try:
                with tracker.source(connector.source_name):
                    if efficiency.mode == "source-first":
                        recent = connector.search_recent_documents(
                            efficiency.market,
                            since=effective_since,
                            limit=source_candidate_limits.get(
                                group,
                                max_candidates_per_source,
                            ),
                        )
                        tracker.raise_if_exceeded()
                        seen_source_ids: set[str] = set()
                        for notice_candidate in recent:
                            source_id = str(
                                notice_candidate.source_document_id or ""
                            ).strip()
                            if source_id:
                                seen_source_ids.add(source_id)

                        def _process_source_candidate(
                            issuer: Issuer,
                            notice_candidate: DocumentCandidate,
                        ) -> None:
                            nonlocal processed_documents
                            matched_issuers.add(issuer.isin)
                            efficiency.matched_candidates += 1
                            if processed_documents >= document_limit:
                                return
                            materialized = connector.materialize_candidate(
                                notice_candidate,
                                issuer,
                            )
                            tracker.raise_if_exceeded()
                            watched_types = WATCHED_DOCUMENT_TYPES.copy()
                            if include_regulatory_news:
                                watched_types.add(
                                    "other_regulatory_announcement"
                                )
                            candidates, rejected = _filter_candidates(
                                materialized,
                                since=effective_since,
                                watched_types=watched_types,
                            )
                            efficiency.rejected_candidates += len(rejected)
                            for c_rej, reason_rej in rejected:
                                rejected_documents.append(
                                    DocumentEvent(
                                        issuer=issuer,
                                        candidate=c_rej,
                                        result="rejected",
                                        reason=reason_rej,
                                    )
                                )
                            record_candidates(
                                issuer,
                                candidates,
                                efficiency,
                            )
                            for candidate in candidates:
                                if processed_documents >= document_limit:
                                    break
                                _store_resolution(
                                    database,
                                    issuer,
                                    candidate,
                                )
                                process_candidate(
                                    issuer,
                                    candidate,
                                    efficiency,
                                    downloader,
                                )
                                tracker.raise_if_exceeded()

                        for notice_candidate in recent:
                            issuer = _best_issuer_match(
                                group_issuers,
                                notice_candidate,
                            )
                            if issuer is None:
                                continue
                            _process_source_candidate(
                                issuer,
                                notice_candidate,
                            )

                        if getattr(
                            connector,
                            "requires_watchlist_queries",
                            False,
                        ):
                            for issuer in group_issuers:
                                if processed_documents >= document_limit:
                                    break
                                try:
                                    issuer_candidates = (
                                        connector.search_documents_for_issuer(
                                            issuer
                                        )
                                    )
                                    tracker.raise_if_exceeded()
                                except RequestLimitExceeded:
                                    raise
                                except Exception as exc:
                                    record_error(
                                        issuer,
                                        "recherche",
                                        str(exc),
                                        efficiency,
                                    )
                                    continue
                                for notice_candidate in issuer_candidates:
                                    source_id = str(
                                        notice_candidate.source_document_id
                                        or ""
                                    ).strip()
                                    if (
                                        source_id
                                        and source_id in seen_source_ids
                                    ):
                                        continue
                                    if source_id:
                                        seen_source_ids.add(source_id)
                                    _process_source_candidate(
                                        issuer,
                                        notice_candidate,
                                    )

                        efficiency.scanned_notices = (
                            connector.scanned_notices or len(recent)
                        )
                    else:
                        for issuer in group_issuers:
                            try:
                                found = (
                                    connector.search_documents_for_issuer(
                                        issuer
                                    )
                                )
                                tracker.raise_if_exceeded()
                            except RequestLimitExceeded:
                                raise
                            except Exception as exc:
                                record_error(
                                    issuer,
                                    "recherche",
                                    str(exc),
                                    efficiency,
                                )
                                continue
                            if connector.state != ConnectorState.READY:
                                record_error(
                                    issuer,
                                    "source",
                                    connector.last_error
                                    or f"Source {connector.state.value}",
                                    efficiency,
                                    source_state=connector.state.value,
                                )
                                recorded_source_error = True
                                continue
                            watched_types = WATCHED_DOCUMENT_TYPES.copy()
                            if include_regulatory_news:
                                watched_types.add("other_regulatory_announcement")
                            candidates, rejected = _filter_candidates(
                                found,
                                since=since,
                                watched_types=watched_types,
                            )
                            efficiency.rejected_candidates += len(rejected)
                            for c_rej, reason_rej in rejected:
                                rejected_documents.append(
                                    DocumentEvent(
                                        issuer=issuer,
                                        candidate=c_rej,
                                        result="rejected",
                                        reason=reason_rej,
                                    )
                                )
                            if candidates:
                                matched_issuers.add(issuer.isin)
                            record_candidates(
                                issuer,
                                candidates,
                                efficiency,
                            )
                            for candidate in candidates:
                                if processed_documents >= document_limit:
                                    break
                                _store_resolution(
                                    database,
                                    issuer,
                                    candidate,
                                )
                                process_candidate(
                                    issuer,
                                    candidate,
                                    efficiency,
                                    downloader,
                                )
                                tracker.raise_if_exceeded()

                    if connector.state != ConnectorState.READY:
                        if not recorded_source_error:
                            record_error(
                                group_issuers[0],
                                "source",
                                connector.last_error
                                or f"Source {connector.state.value}",
                                efficiency,
                                source_state=connector.state.value,
                            )
                            recorded_source_error = True
                    else:
                        database.set_source_state(
                            source=connector.source_name,
                            market=efficiency.market,
                            state=ConnectorState.READY.value,
                            error=None,
                            context="watch",
                        )
            except RequestLimitExceeded:
                raise
            except Exception as exc:
                if not recorded_source_error:
                    record_error(
                        group_issuers[0],
                        "source",
                        str(exc),
                        efficiency,
                        source_state=ConnectorState.UNAVAILABLE.value,
                    )
            finally:
                efficiency.matched_issuers = len(matched_issuers)
                efficiency.details_visited = connector.details_visited
                efficiency.cache_hits = connector.cache_hits
                efficiency.http_calls = tracker.requests_by_source.get(
                    connector.source_name,
                    0,
                )
                efficiency.elapsed_seconds = (
                    time.monotonic() - source_started
                )

        status = "success" if stats.errors == 0 else "partial"
    except Exception as exc:
        status = "failed"
        stats.errors += 1
        errors.append(IssuerError(None, "run", str(exc)))
        database.add_operational_event(
            watch_run_id=run_id,
            event_status="error",
            source="watch",
            message=str(exc),
        )
        LOGGER.error("Watch interrompu: %s", exc)
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                LOGGER.exception("Impossible de fermer la session HTTP")

    ended_at = clock()
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    report = WatchReport(
        run_id=run_id,
        market=scope,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        since=effective_since,
        limit=limit,
        dry_run=dry_run,
        stats=stats,
        max_download_bytes=settings.max_download_bytes,
        lookback_days=effective_lookback_days,
        max_candidates_per_source=max_candidates_per_source,
        max_documents_per_run=document_limit,
        execution_mode=execution_mode,
        market_stats=market_stats,
        source_efficiency=source_efficiency,
        issuer_checks=issuer_checks,
        new_documents=new_documents,
        duplicates=duplicates,
        skipped_too_large=skipped_too_large,
        errors=errors,
        degraded_sources=degraded_sources,
        ssl_disabled_sources=set(getattr(session, "ssl_disabled_sources", ())) if session is not None else set(),
        rejected_documents=rejected_documents,
    )
    report_path = write_watch_report(report, reports_dir)
    database.record_watch_market_stats(run_id, market_stats)
    database.finish_watch_run(
        run_id,
        status=status,
        issuers_checked=stats.issuers_checked,
        candidates_found=stats.candidates_found,
        downloaded=stats.downloaded,
        duplicates=stats.duplicates,
        skipped_too_large=stats.skipped_too_large,
        errors=stats.errors,
        report_path=str(report_path),
        ended_at=ended_at.isoformat(timespec="seconds"),
    )
    notification_path = None
    if notify_email:
        notification_path = write_notification_email(
            recipient=notify_email,
            run_status=status,
            summary={
                "émetteurs": stats.issuers_checked,
                "candidats": stats.candidates_found,
                "téléchargés": stats.downloaded,
                "doublons": stats.duplicates,
                "ignorés car trop gros": stats.skipped_too_large,
                "erreurs": stats.errors,
                "appels HTTP": (
                    tracker.total_requests if tracker is not None else 0
                ),
            },
            new_documents=new_documents,
            source_errors=degraded_sources,
            report_path=report_path,
            generated_at=ended_at,
        )
    LOGGER.info(
        "Watch terminé: statut=%s, téléchargés=%d, doublons=%d, "
        "appels_http=%d, erreurs=%d, rapport=%s",
        status,
        stats.downloaded,
        stats.duplicates,
        tracker.total_requests if tracker is not None else 0,
        stats.errors,
        report_path,
    )
    return WatchOutcome(
        run_id=run_id,
        status=status,
        stats=stats,
        market_stats=market_stats,
        source_efficiency=source_efficiency,
        report_path=report_path,
        notification_path=notification_path,
    )
