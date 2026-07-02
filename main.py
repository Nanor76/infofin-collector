from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import unicodedata
import webbrowser
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Callable, Mapping

from dotenv import load_dotenv

if sys.platform.startswith("win"):
    sys.stdout.reconfigure(encoding="utf-8")

from config import Settings
from connectors import SUPPORTED_WATCH_MARKETS, connector_for_market
from connectors.base import Connector, ConnectorState, DocumentCandidate
from connectors.belgium_fsma_stori import BelgiumFsmaStoriConnector
from connectors.france_info_financiere import FranceInfoFinanciereConnector
from connectors.ireland_euronext_direct import (
    IrelandEuronextDirectConnector,
)
from connectors.netherlands_afm import NetherlandsAfmConnector
from connectors.oslo_newsweb import OsloNewsWebConnector
from connectors.portugal_cmvm_sdi import PortugalCmvmSdiConnector
from connectors.finland_oam import FinlandOamConnector
from connectors.austria_oekb_oam import AustriaOekbOamConnector
from connectors.poland_knf_oam import PolandKnfOamConnector
from connectors.croatia_hanfa_srpi import CroatiaHanfaSrpiConnector
from connectors.slovenia_oam import SloveniaOamConnector
from connectors.estonia_oam import EstoniaOamConnector
from connectors.latvia_oam import LatviaOamConnector
from connectors.lithuania_oam import LithuaniaOamConnector
from connectors.slovakia_nbs_ceri import SlovakiaNbsCeriConnector
from connectors.bulgaria_bse_x3news import BulgariaBseX3NewsConnector
from connectors.malta_mse_oam import MaltaMseOamConnector
from connectors.romania_asf_oam import RomaniaAsfOamConnector
from db import Database
from download import DocumentDownloader, DownloadError
from http_client import build_http_session
from load_watchlist import load_watchlist, normalize_market
from operations import (
    HealthcheckOutcome,
    HealthcheckResult,
    export_latest_documents,
    render_status,
    write_healthcheck_report,
)
from watcher import run_watch

LOGGER = logging.getLogger("infofin")


@dataclass(slots=True)
class RunStats:
    issuers_checked: int = 0
    candidates_found: int = 0
    documents_downloaded: int = 0
    duplicates: int = 0
    skipped_too_large: int = 0
    errors: int = 0


@dataclass(frozen=True, slots=True)
class MarketDocumentLinksExport:
    output_path: Path
    documents_count: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


SOURCE_MARKETS = {
    "france": "Euronext Paris",
    "oslo": "Oslo Børs",
    "italy": "Euronext Milan",
    "netherlands": "Euronext Amsterdam",
    "belgium": "Euronext Brussels",
    "portugal": "Euronext Lisbon",
    "ireland": "Euronext Dublin",
    "spain": "Bolsa de Madrid",
    "sweden": "Nasdaq Stockholm",
    "denmark": "Nasdaq Copenhagen",
    "finland": "Nasdaq Helsinki",
    "austria": "Vienna Stock Exchange",
    "poland": "Warsaw Stock Exchange",
    "czechia": "Prague Stock Exchange",
    "croatia": "Zagreb Stock Exchange",
    "slovenia": "Ljubljana Stock Exchange",
    "estonia": "Tallinn Stock Exchange",
    "latvia": "Riga Stock Exchange",
    "lithuania": "Vilnius Stock Exchange",
    "slovakia": "Bratislava Stock Exchange",
    "romania": "Bucharest Stock Exchange",
    "bulgaria": "Bulgarian Stock Exchange",
    "malta": "Malta Stock Exchange",
}
SOURCE_NAMES = tuple(SOURCE_MARKETS)


def iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "date attendue au format YYYY-MM-DD"
        ) from exc


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("entier positif attendu") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("la valeur doit être supérieure à 0")
    return parsed


def _browser_url(host: str, port: int) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{browser_host}:{port}/"


def _open_chrome(url: str) -> None:
    chrome_paths = (
        Path(os.environ.get("PROGRAMFILES", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", ""))
        / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Google/Chrome/Application/chrome.exe",
    )
    for chrome_path in chrome_paths:
        if chrome_path.is_file():
            subprocess.Popen(
                [str(chrome_path), url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
    webbrowser.open(url)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _add_runtime_options(
    parser: argparse.ArgumentParser,
    *,
    suppress_defaults: bool = False,
) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    parser.add_argument(
        "--max-download-mb",
        type=positive_int,
        default=default,
        help="Taille maximale d'un document en MiB (défaut: 100).",
    )
    parser.add_argument(
        "--notify-email",
        default=default,
        help="Génère un fichier .eml prêt à envoyer, sans SMTP.",
    )
    parser.add_argument(
        "--lookback-days",
        type=positive_int,
        default=(argparse.SUPPRESS if suppress_defaults else 7),
        help="Fenêtre quotidienne source-first en jours (défaut: 7).",
    )
    parser.add_argument(
        "--max-candidates-per-source",
        type=positive_int,
        default=(argparse.SUPPRESS if suppress_defaults else 1000),
        help="Plafond de notices/candidats chargés par source.",
    )
    parser.add_argument(
        "--max-documents-per-run",
        type=positive_int,
        default=(argparse.SUPPRESS if suppress_defaults else 100),
        help="Plafond de documents traités par exécution.",
    )
    parser.add_argument(
        "--confirm-large-run",
        action="store_true",
        default=(argparse.SUPPRESS if suppress_defaults else False),
        help="Autorise explicitement un run pouvant dépasser 500 appels HTTP.",
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        default=(argparse.SUPPRESS if suppress_defaults else False),
        help="Active le mode historique émetteur par émetteur.",
    )
    parser.add_argument(
        "--issuer-mode",
        action="store_true",
        default=(argparse.SUPPRESS if suppress_defaults else False),
        help="Force le mode ciblé émetteur par émetteur.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Surveille les publications financières officielles Euronext."
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    _add_runtime_options(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import-csv",
        help=(
            "Importe une watchlist multi-marchés séparée par des "
            "points-virgules."
        ),
    )
    import_parser.add_argument("path")

    import_euronext_parser = subparsers.add_parser(
        "import-euronext",
        help="Télécharge et met à jour la liste complète des sociétés cotées sur Euronext.",
    )
    import_euronext_parser.add_argument(
        "--url",
        help="URL alternative pour le téléchargement du CSV de la liste réglementée.",
    )

    sync_issuer_lists_parser = subparsers.add_parser(
        "sync-issuer-lists",
        help=(
            "Met à jour un CSV par place boursière avec les sociétés "
            "cotées disponibles via les sources officielles."
        ),
    )
    sync_issuer_lists_parser.add_argument(
        "--output-dir",
        default="watchlists",
        help="Répertoire de sortie des CSV (défaut: watchlists).",
    )
    sync_issuer_lists_parser.add_argument(
        "--market",
        help="Limiter la synchronisation à un marché.",
    )
    sync_issuer_lists_parser.add_argument(
        "--import",
        dest="import_db",
        action="store_true",
        help="Importer en base les lignes disposant d'un ISIN valide.",
    )

    status_parser = subparsers.add_parser(
        "status",
        help="Affiche l'état opérationnel de la base et des sources.",
    )

    healthcheck_parser = subparsers.add_parser(
        "healthcheck",
        help="Diagnostique toutes les sources et génère un rapport consolidé.",
    )

    export_parser = subparsers.add_parser(
        "export-latest",
        help="Exporte les derniers documents quotidiens.",
    )
    export_parser.add_argument(
        "--format",
        required=True,
        choices=("csv", "json"),
    )
    export_parser.add_argument(
        "--since",
        type=iso_date,
        help=(
            "Exporte les documents dont downloaded_at est superieur ou egal "
            "a cette date locale (YYYY-MM-DD). Par defaut, seule la derniere "
            "date de telechargement globale est exportee."
        ),
    )

    market_links_parser = subparsers.add_parser(
        "discover-market-documents",
        help=(
            "Récupère les liens des documents financiers récemment publiés "
            "depuis les sources officielles, sans watchlist."
        ),
    )
    market_links_scope = market_links_parser.add_mutually_exclusive_group(
        required=True
    )
    market_links_scope.add_argument("--market")
    market_links_scope.add_argument("--all", action="store_true")
    market_links_parser.add_argument("--date-from", type=iso_date, required=True)
    market_links_parser.add_argument("--date-to", type=iso_date, required=True)
    market_links_parser.add_argument(
        "--format",
        choices=("csv", "json"),
        default="csv",
    )
    market_links_parser.add_argument(
        "--output-dir",
        default="exports",
        help="Répertoire de sortie (défaut: exports).",
    )
    market_links_parser.add_argument(
        "--max-candidates",
        type=positive_int,
        default=100000,
        help=(
            "Plafond technique par source officielle (défaut: 100000). "
            "Augmenter si une place dépasse ce volume sur la période."
        ),
    )
    market_links_parser.add_argument(
        "--dedupe-url",
        action="store_true",
        help=(
            "Déduplique globalement les URLs et agrège les places dans la "
            "colonne market."
        ),
    )

    serve_parser = subparsers.add_parser(
        "serve",
        help="Lance la webapp locale de recherche de documents.",
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help="Adresse d'écoute (défaut: INFOFIN_WEB_HOST ou 127.0.0.1).",
    )
    serve_parser.add_argument(
        "--port",
        type=positive_int,
        default=None,
        help="Port d'écoute (défaut: INFOFIN_WEB_PORT ou 8765).",
    )
    serve_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Ne pas ouvrir Chrome automatiquement au démarrage.",
    )

    purge_web_parser = subparsers.add_parser(
        "purge-web-searches",
        help="Supprime les recherches web plus anciennes qu'un seuil.",
    )
    purge_web_parser.add_argument(
        "--older-than-days",
        type=positive_int,
        required=True,
        help="Supprime les jobs créés avant ce nombre de jours.",
    )

    resolve_leis_parser = subparsers.add_parser(
        "resolve-leis",
        help="Interroge la base de données GLEIF pour enrichir tous les émetteurs avec leur code LEI.",
    )

    check_parser = subparsers.add_parser(
        "check",
        help="Recherche et télécharge les nouveaux rapports officiels.",
    )
    scope = check_parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--market")
    scope.add_argument("--all", action="store_true")

    watch_parser = subparsers.add_parser(
        "watch",
        help="Exécute la veille quotidienne et génère un rapport Markdown.",
    )
    watch_scope = watch_parser.add_mutually_exclusive_group(required=True)
    watch_scope.add_argument("--market")
    watch_scope.add_argument(
        "--all",
        action="store_true",
        help=(
            "Surveille France, Oslo, Italie, Netherlands, Belgique "
            "Portugal, Ireland, Espagne, Suède, Danemark, Finlande "
            "Autriche, Pologne, République tchèque, Croatie, Slovénie "
            "Estonie et Lettonie."
        ),
    )
    watch_parser.add_argument("--since", type=iso_date)
    watch_parser.add_argument(
        "--limit",
        type=positive_int,
        help="Nombre maximal de documents traités pendant le run.",
    )
    watch_parser.add_argument("--dry-run", action="store_true")
    watch_parser.add_argument(
        "--include-regulatory-news",
        action="store_true",
        help="Autorise le téléchargement d'annonces réglementaires non périodiques.",
    )
    watch_parser.add_argument(
        "--issuer-website-fallback",
        action="store_true",
        help="Active le mode dégradé expérimental pour l'Allemagne basé sur les sites web investisseurs.",
    )

    diagnose_parser = subparsers.add_parser(
        "diagnose-source",
        help="Teste une source officielle et affiche son schéma observé.",
    )
    diagnose_parser.add_argument(
        "source",
        choices=SOURCE_NAMES,
    )
    diagnose_parser.add_argument(
        "--dataset",
        help="Dataset à tester à la place de AMF_ODS_DATASET.",
    )

    discover_parser = subparsers.add_parser(
        "discover-source",
        help="Recherche les datasets candidats dans le catalogue officiel.",
    )
    discover_parser.add_argument(
        "source",
        choices=SOURCE_NAMES,
    )
    discover_parser.add_argument("--query", default="flux-amf")

    issuer_parser = subparsers.add_parser(
        "discover-issuer",
        help="Résout et persiste les identifiants d'un émetteur officiel.",
    )
    issuer_parser.add_argument(
        "source",
        choices=(
            "oslo",
            "italy",
            "netherlands",
            "belgium",
            "portugal",
            "ireland",
            "spain",
            "sweden",
            "denmark",
            "finland",
            "austria",
            "poland",
            "czechia",
            "croatia",
            "slovenia",
        ),
    )
    issuer_parser.add_argument("--symbol", required=True)
    issuer_parser.add_argument("--name", required=True)
    issuer_parser.add_argument("--isin")

    screen_higgons_parser = subparsers.add_parser(
        "screen-higgons",
        help="Exécute un screener de présélection inspiré de la stratégie William Higgons.",
    )
    screen_higgons_parser.add_argument(
        "--market",
        required=True,
        help="Place ou univers cible (ex: paris, brussels, amsterdam, milan, oslo, lisbon, dublin).",
    )
    screen_higgons_parser.add_argument(
        "--exchange-code",
        help="Code EODHD explicite de la place financière (ex: XPAR, XOSL).",
    )
    screen_higgons_parser.add_argument(
        "--as-of-date",
        type=iso_date,
        help="Date d'analyse au format YYYY-MM-DD (défaut: aujourd'hui).",
    )
    screen_higgons_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignorer le cache et forcer le rechargement depuis EODHD.",
    )
    screen_higgons_parser.add_argument(
        "--limit",
        type=positive_int,
        help="Limiter le nombre de sociétés analysées pour test.",
    )
    screen_higgons_parser.add_argument(
        "--output",
        default="data/screeners/higgons_candidates.csv",
        help="Chemin du fichier CSV de sortie pour les candidats.",
    )
    screen_higgons_parser.add_argument(
        "--json-output",
        help="Chemin facultatif du fichier JSON de sortie structurée.",
    )
    screen_higgons_parser.add_argument(
        "--explain-rejections",
        action="store_true",
        help="Produire également le fichier des sociétés rejetées.",
    )
    screen_higgons_parser.add_argument(
        "--min-daily-traded-eur",
        type=float,
        help="Seuil de liquidité quotidienne minimale en euros (défaut: 50 000).",
    )
    screen_higgons_parser.add_argument(
        "--index-symbol",
        help="Ticker de l'indice de référence EODHD pour calculer la performance relative (ex: FCHI.INDX).",
    )
    screen_higgons_parser.add_argument(
        "--eodhd-backend",
        choices=["rest", "mcp", "auto"],
        default="auto",
        help="Backend de données EODHD à utiliser: 'rest', 'mcp' ou 'auto' (défaut: auto).",
    )

    diagnose_eodhd_parser = subparsers.add_parser(
        "diagnose-eodhd",
        help="Exécute un diagnostic complet de la connexion EODHD (REST, OpenDNS, MCP).",
    )

    prefilter_higgons_parser = subparsers.add_parser(
        "prefilter-higgons",
        help="Exécute un préfiltrage minimaliste pour la stratégie Higgons.",
    )
    prefilter_higgons_parser.add_argument(
        "--market",
        required=True,
        help="Place ou univers cible (ex: paris, brussels, amsterdam, milan, oslo, lisbon, dublin).",
    )
    prefilter_higgons_parser.add_argument(
        "--exchange-code",
        help="Code EODHD explicite de la place financière (ex: XPAR, XOSL).",
    )
    prefilter_higgons_parser.add_argument(
        "--as-of-date",
        type=iso_date,
        help="Date d'analyse au format YYYY-MM-DD (défaut: aujourd'hui).",
    )
    prefilter_higgons_parser.add_argument(
        "--force",
        action="store_true",
        help="Ignorer le cache et forcer le rechargement depuis EODHD.",
    )
    prefilter_higgons_parser.add_argument(
        "--limit",
        type=positive_int,
        help="Limiter le nombre de sociétés analysées pour test.",
    )
    prefilter_higgons_parser.add_argument(
        "--output",
        help="Chemin du fichier CSV de sortie pour les candidats (défaut: généré d'après le marché et la date).",
    )
    prefilter_higgons_parser.add_argument(
        "--json-output",
        help="Chemin facultatif du fichier JSON de sortie structurée.",
    )
    prefilter_higgons_parser.add_argument(
        "--explain-rejections",
        action="store_true",
        help="Produire également le fichier des sociétés rejetées.",
    )
    prefilter_higgons_parser.add_argument(
        "--min-daily-traded-eur",
        type=float,
        default=50000.0,
        help="Seuil de liquidité quotidienne minimale en euros (défaut: 50 000).",
    )
    prefilter_higgons_parser.add_argument(
        "--max-market-cap-eur",
        type=float,
        default=12000000000.0,
        help="Capitalisation maximale autorisée en euros (défaut: 12 000 000 000).",
    )
    prefilter_higgons_parser.add_argument(
        "--index-symbol",
        help="Ticker de l'indice de référence EODHD pour calculer la performance relative (ex: FCHI.INDX).",
    )
    prefilter_higgons_parser.add_argument(
        "--eodhd-backend",
        choices=["rest", "mcp", "auto"],
        default="auto",
        help="Backend de données EODHD à utiliser: 'rest', 'mcp' ou 'auto' (défaut: auto).",
    )

    discover_website_parser = subparsers.add_parser(
        "discover-issuer-website",
        help="Enregistre manuellement l'URL investisseurs/rapports pour l'Allemagne.",
    )
    discover_website_parser.add_argument("market")
    discover_website_parser.add_argument("--name", required=True)
    discover_website_parser.add_argument("--isin", required=True)
    discover_website_parser.add_argument("--url", required=True)

    for command_parser in (
        import_parser,
        import_euronext_parser,
        sync_issuer_lists_parser,
        status_parser,
        healthcheck_parser,
        export_parser,
        market_links_parser,
        check_parser,
        watch_parser,
        diagnose_parser,
        discover_parser,
        issuer_parser,
        screen_higgons_parser,
        prefilter_higgons_parser,
        diagnose_eodhd_parser,
        discover_website_parser,
    ):
        _add_runtime_options(command_parser, suppress_defaults=True)
    return parser


def import_csv(database: Database, path: str) -> int:
    result = load_watchlist(path)
    imported = database.upsert_issuers(result.issuers)
    LOGGER.info(
        "Import terminé: %d émetteurs, %d lignes invalides ignorées, "
        "%d doublons CSV, %d lignes lues.",
        imported,
        result.invalid_rows,
        result.duplicate_rows,
        result.rows_read,
    )
    return 0


def import_euronext(
    database: Database,
    settings: Settings,
    *,
    url: str | None = None,
) -> int:
    import tempfile

    download_url = url or settings.euronext_regulated_list_url
    LOGGER.info(
        "Téléchargement de la liste complète des sociétés cotées depuis Euronext: %s",
        download_url,
    )

    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )

    try:
        response = session.get(download_url, timeout=settings.http_timeout_seconds)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(response.content)

        try:
            result = load_watchlist(temp_path)
            imported = database.upsert_issuers(result.issuers)
            LOGGER.info(
                "Mise à jour Euronext terminée: %d émetteurs importés/mis à jour, "
                "%d lignes invalides ignorées, %d doublons CSV, %d lignes lues.",
                imported,
                result.invalid_rows,
                result.duplicate_rows,
                result.rows_read,
            )
            return 0
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
    except Exception as exc:
        LOGGER.error(
            "Échec de la mise à jour de la liste des sociétés Euronext: %s",
            exc,
        )
        return 1
    finally:
        session.close()


def _market_output_slug(value: str) -> str:
    ascii_value = (
        unicodedata.normalize("NFKD", normalize_market(value))
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    return "".join(
        character.lower() if character.isalnum() else "_"
        for character in ascii_value
    ).strip("_")


def _document_publication_date(candidate: DocumentCandidate) -> date | None:
    return candidate.published_at or candidate.published_date


def _join_metadata_value(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item is not None)
    return "" if value is None else str(value)


def _document_link_row(
    market: str,
    candidate: DocumentCandidate,
) -> dict[str, object]:
    metadata = candidate.metadata or {}
    publication_date = _document_publication_date(candidate)
    return {
        "market": market,
        "source": candidate.source,
        "source_document_id": candidate.source_document_id or "",
        "published_at": (
            publication_date.isoformat() if publication_date else ""
        ),
        "period_end_date": (
            candidate.period_end_date.isoformat()
            if candidate.period_end_date
            else ""
        ),
        "reporting_year": candidate.reporting_year or "",
        "document_type": candidate.document_type,
        "classification": candidate.classification or "",
        "title": candidate.title,
        "url": candidate.url,
        "issuer_name": _join_metadata_value(
            metadata.get("issuer_name")
            or metadata.get("issuer")
            or metadata.get("company_name")
        ),
        "issuer_isin": _join_metadata_value(
            metadata.get("issuer_isin")
            or metadata.get("issuer_isins")
            or metadata.get("isin")
        ),
        "issuer_lei": _join_metadata_value(
            metadata.get("issuer_lei") or metadata.get("lei")
        ),
        "category": _join_metadata_value(metadata.get("category")),
        "date_confidence": candidate.date_confidence or "",
        "source_publication_date_raw": (
            candidate.source_publication_date_raw or ""
        ),
    }


def _dedupe_document_link_rows_by_url(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    deduped: dict[str, dict[str, object]] = {}
    markets_by_url: dict[str, list[str]] = {}
    for row in rows:
        url = str(row.get("url") or "")
        if not url:
            continue
        market = str(row.get("market") or "")
        if url not in deduped:
            deduped[url] = dict(row)
            markets_by_url[url] = []
        if market and market not in markets_by_url[url]:
            markets_by_url[url].append(market)

    result: list[dict[str, object]] = []
    for url, row in deduped.items():
        row["market"] = ", ".join(markets_by_url[url])
        result.append(row)
    return result


def discover_market_document_links(
    settings: Settings,
    *,
    markets: tuple[str, ...],
    date_from: date,
    date_to: date,
    output_format: str = "csv",
    output_dir: str | Path = "exports",
    max_candidates: int = 100000,
    dedupe_url: bool = False,
    session_factory: Callable[..., object] = build_http_session,
    connector_factory: Callable[..., Connector | None] = connector_for_market,
) -> MarketDocumentLinksExport:
    from webapp.services.document_search import (
        DocumentSearchService,
        LinkSearchRequest,
    )
    from webapp.services.exports import write_search_export

    if output_format not in {"csv", "json"}:
        raise ValueError("format attendu: csv ou json")

    request = LinkSearchRequest(
        markets=markets,
        date_from=date_from,
        date_to=date_to,
        max_candidates=max_candidates,
        dedupe_url=dedupe_url,
    )
    result_set = DocumentSearchService(
        settings,
        session_factory=session_factory,
        connector_factory=connector_factory,
    ).search_links(request)
    target = write_search_export(
        result_set,
        output_format=output_format,
        output_dir=output_dir,
    )
    return MarketDocumentLinksExport(
        output_path=target,
        documents_count=len(result_set.documents),
        errors=result_set.errors,
        warnings=result_set.warnings,
    )


def check_documents(
    database: Database,
    settings: Settings,
    *,
    market: str | None,
) -> int:
    normalized_market = normalize_market(market) if market else None
    issuers = database.list_issuers(normalized_market)
    scope = normalized_market or "all"
    run_id = database.create_run(scope)
    stats = RunStats()
    degraded_markets: set[str] = set()
    unsupported_markets: set[str] = set()

    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    downloader = DocumentDownloader(
        database=database,
        session=session,
        data_dir=settings.data_dir,
        timeout=settings.http_timeout_seconds,
        max_download_bytes=settings.max_download_bytes,
    )
    connectors: dict[str, Connector] = {}

    try:
        if not issuers:
            LOGGER.warning("Aucun émetteur trouvé pour le périmètre %s", scope)

        for issuer in issuers:
            stats.issuers_checked += 1
            market_key = issuer.market.casefold()
            if market_key in degraded_markets:
                continue

            connector = connectors.get(market_key)
            if connector is None:
                connector = connector_for_market(
                    issuer.market,
                    settings=settings,
                    session=session,
                )
                if connector is not None:
                    connectors[market_key] = connector
            if connector is None:
                if market_key not in unsupported_markets:
                    LOGGER.warning(
                        "Marché sans connecteur MVP, émetteurs ignorés: %s",
                        issuer.market,
                    )
                    unsupported_markets.add(market_key)
                continue

            try:
                candidates = connector.search_documents(issuer)
            except Exception as exc:
                stats.errors += 1
                LOGGER.error(
                    "Recherche échouée pour %s (%s): %s",
                    issuer.name,
                    issuer.isin,
                    exc,
                )
                continue

            if connector.state != ConnectorState.READY:
                stats.errors += 1
                degraded_markets.add(market_key)
                LOGGER.error(
                    "Connecteur %s %s; les autres marchés restent actifs. %s",
                    connector.source_name,
                    connector.state.value,
                    connector.last_error or "",
                )
                continue

            stats.candidates_found += len(candidates)
            for candidate in candidates:
                try:
                    result = downloader.download(issuer, candidate)
                except DownloadError as exc:
                    stats.errors += 1
                    LOGGER.error("%s", exc)
                    continue
                if result.status == "downloaded":
                    stats.documents_downloaded += 1
                elif result.status == "skipped_too_large":
                    stats.skipped_too_large += 1
                    database.add_operational_event(
                        issuer=issuer,
                        candidate=candidate,
                        event_status="skipped_too_large",
                        file_size=result.file_size,
                        message=result.message,
                    )
                else:
                    stats.duplicates += 1

        status = "success" if stats.errors == 0 else "partial"
        database.finish_run(
            run_id,
            status=status,
            issuers_checked=stats.issuers_checked,
            candidates_found=stats.candidates_found,
            documents_downloaded=stats.documents_downloaded,
            duplicates=stats.duplicates,
            errors=stats.errors,
        )
    except Exception as exc:
        database.finish_run(
            run_id,
            status="failed",
            issuers_checked=stats.issuers_checked,
            candidates_found=stats.candidates_found,
            documents_downloaded=stats.documents_downloaded,
            duplicates=stats.duplicates,
            errors=stats.errors + 1,
            message=str(exc),
        )
        raise
    finally:
        session.close()

    LOGGER.info(
        "Run terminé: %d émetteurs, %d candidats, %d téléchargements, "
        "%d doublons, %d ignorés car trop gros, %d erreurs.",
        stats.issuers_checked,
        stats.candidates_found,
        stats.documents_downloaded,
        stats.duplicates,
        stats.skipped_too_large,
        stats.errors,
    )
    return 1 if stats.errors else 0


def _france_connector(
    settings: Settings,
    session: object,
    *,
    dataset: str | None = None,
) -> FranceInfoFinanciereConnector:
    return FranceInfoFinanciereConnector(
        session=session,
        base_url=settings.amf_base_url,
        fallback_base_urls=settings.amf_fallback_base_urls,
        dataset=dataset or settings.amf_dataset,
        rows=settings.amf_rows,
        timeout=settings.http_timeout_seconds,
    )


def _oslo_connector(
    settings: Settings,
    session: object,
) -> OsloNewsWebConnector:
    return OsloNewsWebConnector(
        session=session,
        euronext_news_url=settings.oslo_euronext_news_url,
        newsweb_base_url=settings.oslo_newsweb_base_url,
        rate_limit_seconds=settings.oslo_rate_limit_seconds,
        lookback_days=settings.oslo_lookback_days,
        timeout=settings.http_timeout_seconds,
    )


def _italy_connector(
    settings: Settings,
    session: object,
) -> ItalyEmarketStorageConnector:
    from connectors.italy_emarketstorage import ItalyEmarketStorageConnector
    return ItalyEmarketStorageConnector(
        session=session,
        home_url=settings.italy_home_url,
        press_releases_url=settings.italy_press_releases_url,
        documents_url=settings.italy_documents_url,
        oneinfo_url=settings.italy_1info_url,
        borsa_company_base_url=settings.italy_borsa_company_base_url,
        rate_limit_seconds=settings.italy_rate_limit_seconds,
        lookback_days=settings.italy_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.italy_verify_ssl,
        max_pages=settings.italy_max_pages,
    )


def _netherlands_connector(
    settings: Settings,
    session: object,
) -> NetherlandsAfmConnector:
    return NetherlandsAfmConnector(
        session=session,
        register_url=settings.netherlands_afm_register_url,
        export_type=settings.netherlands_afm_export_type,
        home_member_state_url=settings.netherlands_home_member_state_url,
        home_member_state_export_type=(
            settings.netherlands_home_member_state_export_type
        ),
        rate_limit_seconds=settings.netherlands_rate_limit_seconds,
        lookback_days=settings.netherlands_lookback_days,
        timeout=settings.http_timeout_seconds,
    )


def _belgium_connector(
    settings: Settings,
    session: object,
) -> BelgiumFsmaStoriConnector:
    return BelgiumFsmaStoriConnector(
        session=session,
        base_url=settings.belgium_fsma_stori_base_url,
        rate_limit_seconds=settings.belgium_rate_limit_seconds,
        lookback_days=settings.belgium_lookback_days,
        timeout=settings.http_timeout_seconds,
    )


def _portugal_connector(
    settings: Settings,
    session: object,
) -> PortugalCmvmSdiConnector:
    return PortugalCmvmSdiConnector(
        session=session,
        base_url=settings.portugal_cmvm_base_url,
        sdi_url=settings.portugal_cmvm_sdi_url,
        rate_limit_seconds=settings.portugal_rate_limit_seconds,
        lookback_days=settings.portugal_lookback_days,
        timeout=settings.http_timeout_seconds,
    )


def _ireland_connector(
    settings: Settings,
    session: object,
) -> IrelandEuronextDirectConnector:
    return IrelandEuronextDirectConnector(
        session=session,
        base_url=settings.ireland_euronext_direct_base_url,
        dublin_url=settings.ireland_euronext_dublin_url,
        rate_limit_seconds=settings.ireland_rate_limit_seconds,
        lookback_days=settings.ireland_lookback_days,
        timeout=settings.http_timeout_seconds,
    )


def _denmark_connector(settings: Settings, session: object):
    from connectors.denmark_dfsa_oam import DenmarkDfsaOamConnector

    return DenmarkDfsaOamConnector(
        session=session,
        base_url=settings.denmark_dfsa_base_url,
        nasdaq_listed_companies_url=(
            settings.denmark_nasdaq_listed_companies_url
        ),
        rate_limit_seconds=settings.denmark_rate_limit_seconds,
        lookback_days=settings.denmark_dfsa_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.denmark_verify_ssl,
    )


def _finland_connector(settings: Settings, session: object) -> FinlandOamConnector:
    return FinlandOamConnector(
        session=session,
        base_url=settings.finland_oam_base_url,
        rate_limit_seconds=settings.finland_rate_limit_seconds,
        lookback_days=settings.finland_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.finland_verify_ssl,
    )


def _austria_connector(
    settings: Settings,
    session: object,
) -> AustriaOekbOamConnector:
    return AustriaOekbOamConnector(
        session=session,
        feed_url=settings.austria_oekb_feed_url,
        download_base_url=settings.austria_oekb_download_base_url,
        issuer_list_url=settings.austria_oekb_issuer_list_url,
        rate_limit_seconds=settings.austria_oekb_rate_limit_seconds,
        lookback_days=settings.austria_oekb_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.austria_oekb_verify_ssl,
    )


def _poland_connector(
    settings: Settings,
    session: object,
) -> PolandKnfOamConnector:
    return PolandKnfOamConnector(
        session=session,
        base_url=settings.poland_knf_oam_base_url,
        rate_limit_seconds=settings.poland_knf_oam_rate_limit_seconds,
        lookback_days=settings.poland_knf_oam_lookback_days,
        timeout=max(settings.http_timeout_seconds, 45),
        verify_ssl=settings.poland_knf_oam_verify_ssl,
        max_pages_per_date=settings.poland_knf_oam_max_pages_per_date,
        cache_path=(
            settings.data_dir.parent / "cache" / "poland_knf_oam.json"
        ),
    )


def _czechia_connector(
    settings: Settings,
    session: object,
) -> CzechiaCnbCuriConnector:
    from connectors.czechia_cnb_curi import CzechiaCnbCuriConnector
    return CzechiaCnbCuriConnector(
        session=session,
        start_url=settings.czechia_cnb_oam_start_url,
        search_url=settings.czechia_cnb_oam_search_url,
        download_base_url=settings.czechia_cnb_oam_download_base_url,
        rate_limit_seconds=settings.czechia_cnb_oam_rate_limit_seconds,
        lookback_days=settings.czechia_cnb_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.czechia_cnb_oam_verify_ssl,
    )


def _croatia_connector(
    settings: Settings,
    session: object,
) -> CroatiaHanfaSrpiConnector:
    return CroatiaHanfaSrpiConnector(
        session=session,
        base_url=settings.croatia_hanfa_srpi_base_url,
        rate_limit_seconds=settings.croatia_hanfa_srpi_rate_limit_seconds,
        lookback_days=settings.croatia_hanfa_srpi_lookback_days,
        timeout=max(settings.http_timeout_seconds, 45),
        verify_ssl=settings.croatia_hanfa_srpi_verify_ssl,
    )


def _slovenia_connector(
    settings: Settings,
    session: object,
) -> SloveniaOamConnector:
    return SloveniaOamConnector(
        session=session,
        base_url=settings.slovenia_oam_base_url,
        rate_limit_seconds=settings.slovenia_oam_rate_limit_seconds,
        lookback_days=settings.slovenia_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.slovenia_oam_verify_ssl,
        max_pages=settings.slovenia_oam_max_pages,
    )


def _estonia_connector(
    settings: Settings,
    session: object,
) -> EstoniaOamConnector:
    return EstoniaOamConnector(
        session=session,
        base_url=settings.estonia_oam_base_url,
        rate_limit_seconds=settings.estonia_oam_rate_limit_seconds,
        lookback_days=settings.estonia_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.estonia_oam_verify_ssl,
        max_pages=settings.estonia_oam_max_pages,
    )


def _latvia_connector(
    settings: Settings,
    session: object,
) -> LatviaOamConnector:
    return LatviaOamConnector(
        session=session,
        base_url=settings.latvia_oam_base_url,
        rate_limit_seconds=settings.latvia_oam_rate_limit_seconds,
        lookback_days=settings.latvia_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.latvia_oam_verify_ssl,
        max_pages=settings.latvia_oam_max_pages,
    )


def _lithuania_connector(
    settings: Settings,
    session: object,
) -> LithuaniaOamConnector:
    return LithuaniaOamConnector(
        session=session,
        base_url=settings.lithuania_oam_base_url,
        rate_limit_seconds=settings.lithuania_oam_rate_limit_seconds,
        lookback_days=settings.lithuania_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.lithuania_oam_verify_ssl,
        max_pages=settings.lithuania_oam_max_pages,
    )


def _slovakia_connector(
    settings: Settings,
    session: object,
) -> SlovakiaNbsCeriConnector:
    return SlovakiaNbsCeriConnector(
        session=session,
        base_url=settings.slovakia_nbs_ceri_base_url,
        rate_limit_seconds=settings.slovakia_nbs_ceri_rate_limit_seconds,
        lookback_days=settings.slovakia_nbs_ceri_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.slovakia_nbs_ceri_verify_ssl,
    )


def _romania_connector(
    settings: Settings,
    session: object,
) -> RomaniaAsfOamConnector:
    return RomaniaAsfOamConnector(
        session=session,
        base_url=settings.romania_asf_oam_base_url,
        rate_limit_seconds=settings.romania_asf_oam_rate_limit_seconds,
        lookback_days=settings.romania_asf_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.romania_asf_oam_verify_ssl,
        max_pages=settings.romania_asf_oam_max_pages,
    )


def _bulgaria_connector(
    settings: Settings,
    session: object,
) -> BulgariaBseX3NewsConnector:
    return BulgariaBseX3NewsConnector(
        session=session,
        base_url=settings.bulgaria_bse_x3news_base_url,
        rate_limit_seconds=settings.bulgaria_bse_x3news_rate_limit_seconds,
        lookback_days=settings.bulgaria_bse_x3news_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.bulgaria_bse_x3news_verify_ssl,
        max_active_buckets=settings.bulgaria_bse_x3news_max_active_buckets,
        max_issuer_scans=settings.bulgaria_bse_x3news_max_issuer_scans,
        max_candidates_per_source=(
            settings.bulgaria_bse_x3news_max_candidates_per_source
        ),
    )


def _malta_connector(
    settings: Settings,
    session: object,
) -> MaltaMseOamConnector:
    return MaltaMseOamConnector(
        session=session,
        base_url=settings.malta_mse_oam_base_url,
        rate_limit_seconds=settings.malta_mse_oam_rate_limit_seconds,
        lookback_days=settings.malta_mse_oam_lookback_days,
        timeout=settings.http_timeout_seconds,
        verify_ssl=settings.malta_mse_oam_verify_ssl,
    )


def _attempt_output(attempt: object) -> dict[str, object]:
    return {
        "name": getattr(attempt, "name", None),
        "base_url": getattr(attempt, "base_url", None),
        "dataset": getattr(attempt, "dataset", None),
        "endpoint": getattr(attempt, "endpoint", getattr(attempt, "url", None)),
        "method": getattr(attempt, "method", None),
        "http_status": getattr(attempt, "http_status", getattr(attempt, "status_code", None)),
        "success": getattr(
            attempt,
            "success",
            (
                getattr(attempt, "status_code", 500) is not None
                and getattr(attempt, "status_code", 500) < 400
            ),
        ),
        "total_count": getattr(attempt, "total_count", None),
        "response_excerpt": getattr(attempt, "response_excerpt", None),
        "error": getattr(attempt, "error", getattr(attempt, "note", None)),
    }


def diagnose_source(
    settings: Settings,
    source: str,
    *,
    dataset: str | None = None,
) -> int:
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    try:
        if source == "france":
            connector = _france_connector(settings, session, dataset=dataset)
            diagnostic = connector.diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "base_url": diagnostic.base_url,
                "dataset": diagnostic.dataset,
                "selected_endpoint": diagnostic.selected_endpoint,
                "total_count": diagnostic.total_count,
                "fields": list(diagnostic.fields),
                "example_record": diagnostic.example_record,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "oslo":
            diagnostic = _oslo_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "topics": list(diagnostic.topics),
                "example_notice": diagnostic.example_notice,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "italy":
            diagnostic = _italy_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "categories": list(diagnostic.categories),
                "example_document": diagnostic.example_document,
                "checks": diagnostic.checks,
                "fallback_sources": diagnostic.fallback_sources,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "netherlands":
            diagnostic = _netherlands_connector(
                settings,
                session,
            ).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "example_notice": diagnostic.example_notice,
                "checks": diagnostic.checks,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "belgium":
            diagnostic = _belgium_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "api_url": diagnostic.api_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "checks": diagnostic.checks,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "portugal":
            diagnostic = _portugal_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "api_url": diagnostic.api_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "checks": diagnostic.checks,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "ireland":
            diagnostic = _ireland_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "oam_url": diagnostic.oam_url,
                "ris_url": diagnostic.ris_url,
                "dublin_url": diagnostic.dublin_url,
                "http_status": diagnostic.http_status,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "checks": diagnostic.checks,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "spain":
            from connectors.spain_cnmv import SpainCnmvConnector
            diagnostic = SpainCnmvConnector(
                session=session,
                base_url=settings.spain_cnmv_base_url,
                bme_listed_companies_url=settings.spain_bme_listed_companies_url,
                rate_limit_seconds=settings.spain_rate_limit_seconds,
                lookback_days=settings.spain_cnmv_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.spain_verify_ssl,
            ).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "fields": list(diagnostic.fields),
                "example_notice": diagnostic.example_notice,
                "formats": list(diagnostic.formats),
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "sweden":
            from connectors.sweden_fi import SwedenFiConnector
            diagnostic = SwedenFiConnector(
                session=session,
                base_url=settings.sweden_fi_base_url,
                nasdaq_listed_companies_url=settings.sweden_nasdaq_listed_companies_url,
                rate_limit_seconds=settings.sweden_rate_limit_seconds,
                lookback_days=settings.sweden_fi_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.sweden_verify_ssl,
            ).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "fields": list(diagnostic.fields),
                "example_notice": diagnostic.example_notice,
                "formats": list(diagnostic.formats),
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "denmark":
            diagnostic = _denmark_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "fields": list(diagnostic.fields),
                "example_notice": diagnostic.example_notice,
                "formats": list(diagnostic.formats),
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "finland":
            diagnostic = _finland_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "base_url": diagnostic.base_url,
                "dataset": diagnostic.dataset,
                "selected_endpoint": diagnostic.selected_endpoint,
                "total_count": diagnostic.total_count,
                "fields": list(diagnostic.fields),
                "example_record": diagnostic.example_record,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "austria":
            diagnostic = _austria_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "poland":
            diagnostic = _poland_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "czechia":
            diagnostic = _czechia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "croatia":
            diagnostic = _croatia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "slovenia":
            diagnostic = _slovenia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "estonia":
            diagnostic = _estonia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "latvia":
            diagnostic = _latvia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "lithuania":
            diagnostic = _lithuania_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "slovakia":
            diagnostic = _slovakia_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "romania":
            diagnostic = _romania_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "bulgaria":
            diagnostic = _bulgaria_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        elif source == "malta":
            diagnostic = _malta_connector(settings, session).diagnose()
            output = {
                "source": diagnostic.source,
                "state": diagnostic.state.value,
                "called_url": diagnostic.called_url,
                "http_status": diagnostic.http_status,
                "method_used": diagnostic.method_used,
                "total_count": diagnostic.total_count,
                "detected_count": diagnostic.detected_count,
                "attachment_count": diagnostic.attachment_count,
                "fields": list(diagnostic.fields),
                "categories": diagnostic.categories,
                "formats": list(diagnostic.formats),
                "example_notice": diagnostic.example_notice,
                "http_calls": diagnostic.http_calls,
                "request_efficiency": diagnostic.request_efficiency,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in diagnostic.attempts
                ],
                "error": diagnostic.error,
            }
        else:
            raise ValueError(f"Source inconnue: {source}")
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        exploitable = (
            diagnostic.state
            in {ConnectorState.READY, ConnectorState.DEGRADED}
            and (
                getattr(diagnostic, "detected_count", 0) > 0
                or getattr(diagnostic, "example_record", None) is not None
                or getattr(diagnostic, "example_document", None) is not None
                or getattr(diagnostic, "example_notice", None) is not None
            )
        )
        return 0 if exploitable else 1
    finally:
        session.close()


def _healthcheck_diagnostic(
    settings: Settings,
    source: str,
    session: object,
) -> dict[str, object]:
    if source == "france":
        diagnostic = _france_connector(settings, session).diagnose()
    elif source == "oslo":
        diagnostic = _oslo_connector(settings, session).diagnose()
    elif source == "italy":
        diagnostic = _italy_connector(settings, session).diagnose()
    elif source == "netherlands":
        diagnostic = _netherlands_connector(settings, session).diagnose()
    elif source == "belgium":
        diagnostic = _belgium_connector(settings, session).diagnose()
    elif source == "portugal":
        diagnostic = _portugal_connector(settings, session).diagnose()
    elif source == "ireland":
        diagnostic = _ireland_connector(settings, session).diagnose()
    elif source == "spain":
        from connectors.spain_cnmv import SpainCnmvConnector
        diagnostic = SpainCnmvConnector(
            session=session,
            base_url=settings.spain_cnmv_base_url,
            bme_listed_companies_url=settings.spain_bme_listed_companies_url,
            rate_limit_seconds=settings.spain_rate_limit_seconds,
            lookback_days=settings.spain_cnmv_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.spain_verify_ssl,
        ).diagnose()
    elif source == "sweden":
        from connectors.sweden_fi import SwedenFiConnector
        diagnostic = SwedenFiConnector(
            session=session,
            base_url=settings.sweden_fi_base_url,
            nasdaq_listed_companies_url=settings.sweden_nasdaq_listed_companies_url,
            rate_limit_seconds=settings.sweden_rate_limit_seconds,
            lookback_days=settings.sweden_fi_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.sweden_verify_ssl,
        ).diagnose()
    elif source == "denmark":
        diagnostic = _denmark_connector(settings, session).diagnose()
    elif source == "finland":
        diagnostic = _finland_connector(settings, session).diagnose()
    elif source == "austria":
        diagnostic = _austria_connector(settings, session).diagnose()
    elif source == "poland":
        diagnostic = _poland_connector(settings, session).diagnose()
    elif source == "czechia":
        diagnostic = _czechia_connector(settings, session).diagnose()
    elif source == "croatia":
        diagnostic = _croatia_connector(settings, session).diagnose()
    elif source == "slovenia":
        diagnostic = _slovenia_connector(settings, session).diagnose()
    elif source == "estonia":
        diagnostic = _estonia_connector(settings, session).diagnose()
    elif source == "latvia":
        diagnostic = _latvia_connector(settings, session).diagnose()
    elif source == "lithuania":
        diagnostic = _lithuania_connector(settings, session).diagnose()
    elif source == "slovakia":
        diagnostic = _slovakia_connector(settings, session).diagnose()
    elif source == "romania":
        diagnostic = _romania_connector(settings, session).diagnose()
    elif source == "bulgaria":
        diagnostic = _bulgaria_connector(settings, session).diagnose()
    elif source == "malta":
        diagnostic = _malta_connector(settings, session).diagnose()
    else:
        raise ValueError(f"Source inconnue: {source}")
    output = asdict(diagnostic)
    output["state"] = diagnostic.state.value
    return output


def run_healthcheck(
    database: Database,
    settings: Settings,
    *,
    reports_dir: str | Path = "reports",
    now: Callable[[], datetime] | None = None,
    diagnostic_provider: (
        Callable[[str], Mapping[str, object]] | None
    ) = None,
) -> HealthcheckOutcome:
    clock = now or (lambda: datetime.now(UTC))
    started_at = clock()
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    run_id = database.create_healthcheck_run(
        started_at=started_at.isoformat(timespec="seconds"),
    )
    results: list[HealthcheckResult] = []
    session = None
    if diagnostic_provider is None:
        session = build_http_session(
            retries=settings.http_retries,
            backoff_factor=settings.http_backoff_factor,
            user_agent=settings.user_agent,
            verify=settings.http_verify_ssl,
        )

    try:
        for source, market in SOURCE_MARKETS.items():
            checked_at = clock()
            if checked_at.tzinfo is None:
                checked_at = checked_at.replace(tzinfo=UTC)
            try:
                if diagnostic_provider is None:
                    details = _healthcheck_diagnostic(
                        settings,
                        source,
                        session,
                    )
                else:
                    details = dict(diagnostic_provider(source))
                state = str(details.get("state") or "unavailable")
                error_value = details.get("error")
                error = str(error_value) if error_value else None
            except Exception as exc:
                state = ConnectorState.UNAVAILABLE.value
                error = str(exc)
                details = {
                    "source": source,
                    "state": state,
                    "error": error,
                }
                LOGGER.exception(
                    "Healthcheck: diagnostic échoué pour %s",
                    source,
                )

            result = HealthcheckResult(
                source=source,
                market=market,
                state=state,
                critical=True,
                error=error,
                details=details,
            )
            results.append(result)
            database.add_source_health_check(
                healthcheck_run_id=run_id,
                checked_at=checked_at.isoformat(timespec="seconds"),
                source=source,
                market=market,
                state=state,
                critical=True,
                error=error,
                details=details,
            )
            if state in {
                ConnectorState.DEGRADED.value,
                ConnectorState.UNAVAILABLE.value,
            }:
                database.add_operational_event(
                    event_status="error",
                    market=market,
                    source=source,
                    message=error or f"Source {state}",
                    created_at=checked_at.isoformat(timespec="seconds"),
                )
    finally:
        if session is not None:
            session.close()

    ended_at = clock()
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)
    if any(result.state == ConnectorState.UNAVAILABLE.value for result in results):
        status = "failed"
    elif any(result.state == ConnectorState.DEGRADED.value for result in results):
        status = "degraded"
    else:
        status = "success"
    report_path = write_healthcheck_report(
        results,
        started_at=started_at,
        ended_at=ended_at,
        status=status,
        reports_dir=reports_dir,
    )
    database.finish_healthcheck_run(
        run_id,
        status=status,
        report_path=str(report_path),
        ended_at=ended_at.isoformat(timespec="seconds"),
    )
    return HealthcheckOutcome(
        status=status,
        report_path=report_path,
        results=tuple(results),
    )


def discover_source(settings: Settings, source: str, *, query: str) -> int:
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    try:
        if source == "france":
            discovery = _france_connector(settings, session).discover(query)
            notices: list[dict[str, object]] = []
            candidates = [
                {
                    "dataset_id": candidate.dataset_id,
                    "title": candidate.title,
                    "records_count": candidate.records_count,
                    "base_url": candidate.base_url,
                }
                for candidate in discovery.candidates
            ]
        elif source == "oslo":
            discovery = _oslo_connector(settings, session).discover(query)
            notices = []
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "http_status": candidate.http_status,
                }
                for candidate in discovery.candidates
            ]
        elif source == "italy":
            discovery = _italy_connector(settings, session).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "company": notice.company,
                    "title": notice.title,
                    "url": notice.document_url,
                    "protocol": notice.protocol,
                    "category": notice.category,
                }
                for notice in discovery.notices
            ]
        elif source == "netherlands":
            discovery = _netherlands_connector(
                settings,
                session,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "filing_date": (
                        notice.filing_date.isoformat()
                        if notice.filing_date
                        else None
                    ),
                    "issuing_institution": (
                        notice.issuing_institution
                    ),
                    "reporting_year": notice.reporting_year,
                    "document_type": notice.document_type,
                    "filename": notice.filename,
                    "detail_url": notice.detail_url,
                    "download_url": notice.download_url,
                }
                for notice in discovery.notices
            ]
        elif source == "belgium":
            discovery = _belgium_connector(
                settings,
                session,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "issuer": notice.company_name,
                    "title": notice.document_title,
                    "document_type": notice.reporting_topic,
                    "detail_url": notice.detail_url,
                    "isins": list(notice.isin_codes),
                    "markets": list(notice.markets),
                    "files": [
                        {
                            "file_data_id": item.file_data_id,
                            "filename": item.original_filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "portugal":
            discovery = _portugal_connector(
                settings,
                session,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "document_type": notice.document_type,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "ireland":
            discovery = _ireland_connector(
                settings,
                session,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "issuer": notice.company_name,
                    "title": notice.headline,
                    "regulatory_category": (
                        notice.regulatory_category_description
                        or notice.regulatory_category
                    ),
                    "detail_url": notice.detail_url,
                    "source_kind": notice.source_kind,
                    "isins": list(notice.isin_codes),
                    "symbol": notice.symbol,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "spain":
            from connectors.spain_cnmv import SpainCnmvConnector
            discovery = SpainCnmvConnector(
                session=session,
                base_url=settings.spain_cnmv_base_url,
                bme_listed_companies_url=settings.spain_bme_listed_companies_url,
                rate_limit_seconds=settings.spain_rate_limit_seconds,
                lookback_days=settings.spain_cnmv_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.spain_verify_ssl,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "document_type": notice.document_type,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "sweden":
            from connectors.sweden_fi import SwedenFiConnector
            discovery = SwedenFiConnector(
                session=session,
                base_url=settings.sweden_fi_base_url,
                nasdaq_listed_companies_url=settings.sweden_nasdaq_listed_companies_url,
                rate_limit_seconds=settings.sweden_rate_limit_seconds,
                lookback_days=settings.sweden_fi_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.sweden_verify_ssl,
            ).discover(query)
            candidates = [
                {
                    "url": candidate.url,
                    "role": candidate.role,
                    "format": candidate.format,
                    "pagination": candidate.pagination,
                    "fields": list(candidate.fields),
                    "verified": candidate.verified,
                    "state": candidate.state.value,
                    "http_status": candidate.http_status,
                    "records_count": candidate.records_count,
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": (
                        notice.published_date.isoformat()
                        if notice.published_date
                        else None
                    ),
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "document_type": notice.document_type,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "denmark":
            discovery = _denmark_connector(settings, session).discover(query)
            candidates = [
                {
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_type"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "document_type": notice.category,
                    "detail_url": notice.detail_url,
                    "isins": list(notice.issuer_isins),
                    "symbol": notice.issuer_symbol,
                    "files": [
                        {
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "finland":
            discovery = _finland_connector(settings, session).discover(query)
            candidates = [
                {
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_type"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "document_type": notice.category,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "filename": item.filename,
                            "format": item.file_type,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "austria":
            discovery = _austria_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_format"),
                    "issuer": candidate.metadata.get("issuer_name"),
                    "isins": candidate.metadata.get("issuer_isins"),
                    "meldetyp_code": candidate.metadata.get("meldetyp_code"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer_id": notice.issuer_id,
                    "issuer": notice.issuer_name,
                    "isins": list(notice.issuer_isins),
                    "title": notice.title,
                    "meldetyp_code": notice.meldetyp_code,
                    "files": [
                        {
                            "file_id": item.file_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": (
                                f"{settings.austria_oekb_download_base_url}/"
                                f"{item.file_id}"
                            ),
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "poland":
            discovery = _poland_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_format"),
                    "issuer": candidate.metadata.get("issuer_name"),
                    "report_code": candidate.metadata.get("report_code"),
                    "detail_url": candidate.metadata.get("detail_url"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_date,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "report_code": notice.report_code,
                    "detail_url": notice.detail_url,
                    "filename": notice.filename,
                    "format": (
                        Path(notice.filename).suffix.lstrip(".").casefold()
                    ),
                    "download_url": notice.package_url,
                }
                for notice in discovery.notices
            ]
        elif source == "czechia":
            discovery = _czechia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_format"),
                    "issuer": candidate.metadata.get("issuer_name"),
                    "ico": candidate.metadata.get("issuer_ico"),
                    "lei": candidate.metadata.get("issuer_lei"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "form_id": notice.form_id,
                    "published_at": notice.published_at,
                    "published_raw": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "ico": notice.ico,
                    "lei": notice.lei,
                    "report_type": notice.report_type,
                    "files": [
                        {
                            "filename": f.filename,
                            "file_id": f.file_id,
                            "type": f.file_type,
                            "lang": f.language,
                        }
                        for f in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "croatia":
            discovery = _croatia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "format": candidate.metadata.get("file_format"),
                    "issuer": candidate.metadata.get("issuer_name"),
                    "category_id": candidate.metadata.get("category_id"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "published_at": notice.published_at,
                    "published_raw": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "category_id": notice.category_id,
                    "category": notice.category,
                    "reporting_year": notice.reporting_year,
                    "quarter": notice.quarter,
                    "superseded": notice.superseded,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "slovenia":
            discovery = _slovenia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "lei": candidate.metadata.get("issuer_lei"),
                    "category": candidate.metadata.get("category"),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "received_at": notice.received_raw,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "lei": notice.issuer_lei,
                    "country": notice.country,
                    "title": notice.title,
                    "category": notice.category,
                    "language": notice.language,
                    "report_number": notice.report_number,
                    "detail_url": notice.detail_url,
                }
                for notice in discovery.notices
            ]
        elif source == "estonia":
            discovery = _estonia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "category": candidate.metadata.get("category"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "category": notice.category,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "latvia":
            discovery = _latvia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "category": candidate.metadata.get("category"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "category": notice.category,
                    "language": notice.language,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "lithuania":
            discovery = _lithuania_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "category": candidate.metadata.get("category"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "category": notice.category,
                    "detail_url": notice.detail_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "slovakia":
            discovery = _slovakia_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "category": candidate.metadata.get("category"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "title": notice.title,
                    "category": notice.category,
                    "category_code": notice.category_code,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "romania":
            discovery = _romania_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "isin": candidate.metadata.get("issuer_isin"),
                    "period_type": candidate.metadata.get("period_type"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "isin": notice.isin,
                    "cui": notice.cui,
                    "title": notice.title,
                    "period_type": notice.period_type,
                    "refdate_raw": notice.refdate_raw,
                    "listing_url": notice.listing_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        elif source == "bulgaria":
            discovery = _bulgaria_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "bucket_name": candidate.metadata.get("bucket_name"),
                    "filename": candidate.metadata.get("filename"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "source_document_id": filing.source_document_id,
                    "issuer": filing.issuer_name,
                    "bucket_name": filing.bucket_name,
                    "filename": filing.filename,
                    "last_modified": filing.last_modified,
                    "download_url": filing.download_url,
                    "file_format": filing.file_format,
                }
                for filing in discovery.filings
            ]
        elif source == "malta":
            discovery = _malta_connector(settings, session).discover(query)
            candidates = [
                {
                    "source_document_id": candidate.source_document_id,
                    "title": candidate.title,
                    "url": candidate.url,
                    "classification": candidate.classification,
                    "classification_reason": candidate.classification_reason,
                    "matched_positive_terms": candidate.matched_positive_terms,
                    "matched_negative_terms": candidate.matched_negative_terms,
                    "published_at": candidate.published_at,
                    "period_end_date": candidate.period_end_date,
                    "reporting_year": candidate.reporting_year,
                    "issuer": candidate.metadata.get("issuer_name"),
                    "lei": candidate.metadata.get("issuer_lei"),
                    "market_segment": candidate.metadata.get("market_segment"),
                    "pea_geography_status": candidate.metadata.get(
                        "pea_geography_status"
                    ),
                }
                for candidate in discovery.candidates
            ]
            notices = [
                {
                    "record_id": notice.record_id,
                    "publication_date": notice.published_raw,
                    "issuer": notice.issuer_name,
                    "lei": notice.issuer_lei,
                    "title": notice.title,
                    "market": notice.market,
                    "listing_url": notice.listing_url,
                    "files": [
                        {
                            "attachment_id": item.attachment_id,
                            "filename": item.filename,
                            "format": item.file_format,
                            "download_url": item.download_url,
                        }
                        for item in notice.files
                    ],
                }
                for notice in discovery.notices
            ]
        else:
            raise ValueError(f"Source inconnue: {source}")
        output = {
            "source": discovery.source,
            "query": discovery.query,
            "candidates": candidates,
            "notices": notices,
            "attempts": [
                _attempt_output(attempt) for attempt in discovery.attempts
            ],
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0 if discovery.candidates else 1
    finally:
        session.close()


def discover_issuer(
    database: Database,
    settings: Settings,
    source: str,
    *,
    symbol: str,
    name: str,
    isin: str | None = None,
) -> int:
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    try:
        if source == "oslo":
            resolution = _oslo_connector(settings, session).resolve_issuer(
                symbol=symbol,
                name=name,
            )
            stored = None
            if (
                resolution.found
                and resolution.isin
                and resolution.euronext_company_url
            ):
                stored = database.store_oslo_issuer_resolution(
                    name=resolution.name,
                    symbol=symbol,
                    isin=resolution.isin,
                    oslo_issuer_id=resolution.oslo_issuer_id,
                    newsweb_url=resolution.newsweb_url,
                    euronext_company_url=resolution.euronext_company_url,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.name,
                "isin": resolution.isin,
                "oslo_issuer_id": resolution.oslo_issuer_id,
                "newsweb_url": resolution.newsweb_url,
                "euronext_company_url": resolution.euronext_company_url,
                "issuer_listing_url": resolution.issuer_listing_url,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": resolution.error,
            }
        elif source == "italy":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers()
                    if issuer.market.casefold().startswith("euronext")
                    and "milan" in issuer.market.casefold()
                    and (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            resolution = _italy_connector(
                settings,
                session,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=existing.isin if existing else None,
            )
            stored = None
            if resolution.found and resolution.emarket_url:
                stored = database.store_italy_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    storage_provider=resolution.storage_provider,
                    emarket_url=resolution.emarket_url,
                    oneinfo_url=resolution.oneinfo_url,
                    borsa_italiana_company_url=(
                        resolution.borsa_italiana_company_url
                    ),
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": existing.isin if existing else None,
                "italy_storage_provider": resolution.storage_provider,
                "italy_emarket_url": resolution.emarket_url,
                "italy_1info_url": resolution.oneinfo_url,
                "borsa_italiana_company_url": (
                    resolution.borsa_italiana_company_url
                ),
                "emarket_issuer_id": resolution.emarket_issuer_id,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "netherlands":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Euronext Amsterdam"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            resolution = _netherlands_connector(
                settings,
                session,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=existing.isin if existing else None,
            )
            stored = None
            if (
                resolution.found
                and resolution.afm_issuer_url
                and resolution.afm_detail_url
                and resolution.afm_record_id
            ):
                stored = database.store_netherlands_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    issuer_url=resolution.afm_issuer_url,
                    detail_url=resolution.afm_detail_url,
                    home_member_state=resolution.home_member_state,
                    afm_record_id=resolution.afm_record_id,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": existing.isin if existing else None,
                "netherlands_afm_issuer_url": (
                    resolution.afm_issuer_url
                ),
                "netherlands_afm_detail_url": (
                    resolution.afm_detail_url
                ),
                "netherlands_home_member_state": (
                    resolution.home_member_state
                ),
                "netherlands_afm_record_id": (
                    resolution.afm_record_id
                ),
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "belgium":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers()
                    if issuer.market.casefold()
                    in {
                        "euronext brussels",
                        "euronext growth brussels",
                    }
                    and (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            resolution = _belgium_connector(
                settings,
                session,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=existing.isin if existing else None,
            )
            stored = None
            if (
                resolution.found
                and resolution.stori_url
                and resolution.detail_url
                and resolution.fsma_record_id
            ):
                stored = database.store_belgium_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    stori_url=resolution.stori_url,
                    detail_url=resolution.detail_url,
                    home_member_state=resolution.home_member_state,
                    fsma_record_id=resolution.fsma_record_id,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": resolution.isin,
                "company_id": resolution.company_id,
                "belgium_fsma_stori_url": resolution.stori_url,
                "belgium_fsma_detail_url": resolution.detail_url,
                "belgium_home_member_state": (
                    resolution.home_member_state
                ),
                "belgium_fsma_record_id": resolution.fsma_record_id,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "portugal":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Euronext Lisbon"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            resolution = _portugal_connector(
                settings,
                session,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=existing.isin if existing else None,
            )
            stored = None
            if (
                resolution.found
                and resolution.sdi_url
                and resolution.detail_url
                and resolution.record_id
            ):
                stored = database.store_portugal_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    sdi_url=resolution.sdi_url,
                    detail_url=resolution.detail_url,
                    home_member_state=resolution.home_member_state,
                    cmvm_record_id=resolution.record_id,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": existing.isin if existing else None,
                "portugal_cmvm_sdi_url": resolution.sdi_url,
                "portugal_cmvm_detail_url": resolution.detail_url,
                "portugal_cmvm_record_id": resolution.record_id,
                "portugal_home_member_state": (
                    resolution.home_member_state
                ),
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "ireland":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Euronext Dublin"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            resolution = _ireland_connector(
                settings,
                session,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=existing.isin if existing else None,
            )
            stored = None
            if (
                resolution.found
                and resolution.direct_url
                and resolution.oam_url
                and resolution.detail_url
                and resolution.record_id
            ):
                stored = database.store_ireland_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    direct_url=resolution.direct_url,
                    oam_url=resolution.oam_url,
                    detail_url=resolution.detail_url,
                    home_member_state=resolution.home_member_state,
                    record_id=resolution.record_id,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": existing.isin if existing else None,
                "ireland_euronext_direct_url": resolution.direct_url,
                "ireland_euronext_oam_url": resolution.oam_url,
                "ireland_detail_url": resolution.detail_url,
                "ireland_record_id": resolution.record_id,
                "ireland_home_member_state": (
                    resolution.home_member_state
                ),
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "spain":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers()
                    if issuer.market.casefold() in {
                        "bolsa de madrid",
                        "bolsa de barcelona",
                        "bolsa de bilbao",
                        "bolsa de valencia",
                        "bme growth",
                        "bme scaleup",
                    }
                    and (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            from connectors.spain_cnmv import SpainCnmvConnector
            resolution = SpainCnmvConnector(
                session=session,
                base_url=settings.spain_cnmv_base_url,
                bme_listed_companies_url=settings.spain_bme_listed_companies_url,
                rate_limit_seconds=settings.spain_rate_limit_seconds,
                lookback_days=settings.spain_cnmv_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.spain_verify_ssl,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=isin or (existing.isin if existing else None),
            )
            stored = None
            if (
                resolution.found
                and resolution.cnmv_entity_url
            ):
                stored = database.store_spain_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    cnmv_entity_url=resolution.cnmv_entity_url,
                    cnmv_nif=resolution.cnmv_nif,
                    cnmv_record_id=resolution.cnmv_record_id,
                    bme_company_url=resolution.bme_company_url,
                    home_member_state=resolution.home_member_state,
                    pea_country_check=resolution.pea_country_check,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": isin or (existing.isin if existing else None),
                "spain_cnmv_entity_url": resolution.cnmv_entity_url,
                "spain_cnmv_nif": resolution.cnmv_nif,
                "spain_cnmv_record_id": resolution.cnmv_record_id,
                "spain_bme_company_url": resolution.bme_company_url,
                "spain_home_member_state": (
                    resolution.home_member_state
                ),
                "spain_pea_country_check": resolution.pea_country_check,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "sweden":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers()
                    if issuer.market.casefold() in {
                        "nasdaq stockholm",
                        "stockholm",
                        "omx stockholm",
                        "nasdaq omx stockholm",
                        "swedish stock exchange",
                        "ngm",
                        "nordic growth market",
                    }
                    and (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            from connectors.sweden_fi import SwedenFiConnector
            resolution = SwedenFiConnector(
                session=session,
                base_url=settings.sweden_fi_base_url,
                nasdaq_listed_companies_url=settings.sweden_nasdaq_listed_companies_url,
                rate_limit_seconds=settings.sweden_rate_limit_seconds,
                lookback_days=settings.sweden_fi_lookback_days,
                timeout=settings.http_timeout_seconds,
                verify_ssl=settings.sweden_verify_ssl,
            ).resolve_issuer(
                symbol=symbol,
                name=name,
                isin=isin or (existing.isin if existing else None),
            )
            stored = None
            if resolution.found:
                stored = database.store_sweden_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    sweden_fi_issuer_url=resolution.sweden_fi_issuer_url,
                    sweden_fi_record_id=resolution.sweden_fi_record_id,
                    sweden_fi_detail_url=resolution.sweden_fi_detail_url,
                    sweden_home_member_state=resolution.sweden_home_member_state,
                    sweden_nasdaq_company_url=resolution.sweden_nasdaq_company_url,
                    sweden_pea_country_check=resolution.sweden_pea_country_check,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": isin or (existing.isin if existing else None),
                "sweden_fi_issuer_url": resolution.sweden_fi_issuer_url,
                "sweden_fi_record_id": resolution.sweden_fi_record_id,
                "sweden_fi_detail_url": resolution.sweden_fi_detail_url,
                "sweden_home_member_state": resolution.sweden_home_member_state,
                "sweden_nasdaq_company_url": resolution.sweden_nasdaq_company_url,
                "sweden_pea_country_check": resolution.sweden_pea_country_check,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "denmark":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers("Nasdaq Copenhagen")
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Nasdaq Copenhagen",
            )
            resolution = _denmark_connector(
                settings, session
            ).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_denmark_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    denmark_dfsa_issuer_url=(
                        resolution.denmark_dfsa_issuer_url
                    ),
                    denmark_dfsa_record_id=(
                        resolution.denmark_dfsa_record_id
                    ),
                    denmark_dfsa_detail_url=(
                        resolution.denmark_dfsa_detail_url
                    ),
                    denmark_home_member_state=(
                        resolution.denmark_home_member_state
                    ),
                    denmark_nasdaq_company_url=(
                        resolution.denmark_nasdaq_company_url
                    ),
                    denmark_pea_country_check=(
                        resolution.denmark_pea_country_check
                    ),
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "denmark_dfsa_issuer_url": resolution.denmark_dfsa_issuer_url,
                "denmark_dfsa_record_id": resolution.denmark_dfsa_record_id,
                "denmark_dfsa_detail_url": resolution.denmark_dfsa_detail_url,
                "denmark_home_member_state": (
                    resolution.denmark_home_member_state
                ),
                "denmark_nasdaq_company_url": (
                    resolution.denmark_nasdaq_company_url
                ),
                "denmark_pea_country_check": (
                    resolution.denmark_pea_country_check
                ),
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "warning": (
                    None
                    if resolution.denmark_home_member_state
                    else "Cotation à Copenhague: domicile de la société non confirmé"
                ),
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "finland":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers("Nasdaq Helsinki")
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Nasdaq Helsinki",
            )
            resolution = _finland_connector(settings, session).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_finland_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    finland_oam_company_id=resolution.finland_oam_company_id,
                    finland_oam_issuer_url=resolution.finland_oam_issuer_url,
                    finland_oam_detail_url=resolution.finland_oam_detail_url,
                    finland_home_member_state=resolution.finland_home_member_state,
                    finland_nasdaq_company_url=resolution.finland_nasdaq_company_url,
                    finland_pea_country_check=resolution.finland_pea_country_check,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "finland_oam_company_id": resolution.finland_oam_company_id,
                "finland_oam_issuer_url": resolution.finland_oam_issuer_url,
                "finland_oam_detail_url": resolution.finland_oam_detail_url,
                "finland_home_member_state": resolution.finland_home_member_state,
                "finland_nasdaq_company_url": resolution.finland_nasdaq_company_url,
                "finland_pea_country_check": resolution.finland_pea_country_check,
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "warning": (
                    None
                    if resolution.finland_home_member_state
                    else "Cotation à Helsinki: domicile de la société non confirmé"
                ),
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "austria":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Vienna Stock Exchange"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                        or (
                            isin
                            and issuer.isin.casefold() == isin.casefold()
                        )
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Vienna Stock Exchange",
            )
            resolution = _austria_connector(
                settings, session
            ).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_austria_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    austria_oekb_oam_id=resolution.austria_oekb_oam_id,
                    austria_oekb_oam_issuer_url=(
                        resolution.austria_oekb_oam_issuer_url
                    ),
                    austria_oekb_oam_detail_url=(
                        resolution.austria_oekb_oam_detail_url
                    ),
                    austria_home_member_state=(
                        resolution.austria_home_member_state
                    ),
                    austria_pea_country_check=(
                        resolution.austria_pea_country_check
                    ),
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "austria_oekb_oam_id": resolution.austria_oekb_oam_id,
                "austria_oekb_oam_issuer_url": (
                    resolution.austria_oekb_oam_issuer_url
                ),
                "austria_oekb_oam_detail_url": (
                    resolution.austria_oekb_oam_detail_url
                ),
                "austria_home_member_state": (
                    resolution.austria_home_member_state
                ),
                "austria_pea_country_check": (
                    resolution.austria_pea_country_check
                ),
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "poland":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Warsaw Stock Exchange"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                        or (
                            isin
                            and issuer.isin.casefold() == isin.casefold()
                        )
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Warsaw Stock Exchange",
                pea_geography_status="eu_candidate",
            )
            resolution = _poland_connector(
                settings, session
            ).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_poland_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    source_name=resolution.knf_oam_name,
                    source_url=resolution.knf_oam_issuer_url,
                    detail_url=resolution.knf_oam_detail_url,
                    source_record_id=resolution.knf_oam_record_id,
                    home_member_state=resolution.home_member_state,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "poland_knf_oam_name": resolution.knf_oam_name,
                "poland_knf_oam_issuer_url": (
                    resolution.knf_oam_issuer_url
                ),
                "poland_knf_oam_detail_url": (
                    resolution.knf_oam_detail_url
                ),
                "poland_knf_oam_record_id": (
                    resolution.knf_oam_record_id
                ),
                "poland_home_member_state": (
                    resolution.home_member_state
                ),
                "poland_pea_country_check": (
                    resolution.pea_country_check
                ),
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "czechia":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Prague Stock Exchange"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                        or (
                            isin
                            and issuer.isin.casefold() == isin.casefold()
                        )
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Prague Stock Exchange",
                pea_geography_status="eu_candidate",
            )
            resolution = _czechia_connector(
                settings, session
            ).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_czechia_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    source_name=resolution.czechia_cnb_curi_name,
                    source_url=resolution.czechia_cnb_curi_issuer_url,
                    detail_url=resolution.czechia_cnb_curi_detail_url,
                    source_record_id=resolution.czechia_cnb_curi_record_id,
                    home_member_state=resolution.home_member_state,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "czechia_cnb_curi_name": resolution.czechia_cnb_curi_name,
                "czechia_cnb_curi_issuer_url": (
                    resolution.czechia_cnb_curi_issuer_url
                ),
                "czechia_cnb_curi_detail_url": (
                    resolution.czechia_cnb_curi_detail_url
                ),
                "czechia_cnb_curi_record_id": (
                    resolution.czechia_cnb_curi_record_id
                ),
                "czechia_home_member_state": (
                    resolution.home_member_state
                ),
                "czechia_pea_country_check": (
                    "eu_candidate" if resolution.found else None
                ),
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        elif source == "croatia":
            existing = next(
                (
                    issuer
                    for issuer in database.list_issuers(
                        "Zagreb Stock Exchange"
                    )
                    if (
                        issuer.symbol.casefold() == symbol.casefold()
                        or issuer.name.casefold() == name.casefold()
                        or (
                            isin
                            and issuer.isin.casefold() == isin.casefold()
                        )
                    )
                ),
                None,
            )
            from models import Issuer

            target = Issuer(
                name=name,
                symbol=symbol,
                isin=isin or (existing.isin if existing else ""),
                market="Zagreb Stock Exchange",
                pea_geography_status="eu_candidate",
            )
            resolution = _croatia_connector(
                settings, session
            ).resolve_issuer(target)
            stored = None
            if resolution.found:
                stored = database.store_croatia_issuer_resolution(
                    name=name,
                    symbol=symbol,
                    source_name=resolution.matched_name,
                    source_url=resolution.source_url,
                    detail_url=resolution.detail_url,
                    source_record_id=resolution.source_record_id,
                    home_member_state=resolution.home_member_state,
                )
            output = {
                "source": source,
                "found": resolution.found,
                "symbol": symbol,
                "name": resolution.matched_name or name,
                "isin": target.isin or None,
                "croatia_hanfa_srpi_name": resolution.matched_name,
                "croatia_hanfa_srpi_url": resolution.source_url,
                "croatia_hanfa_srpi_detail_url": resolution.detail_url,
                "croatia_hanfa_srpi_record_id": resolution.source_record_id,
                "croatia_home_member_state": resolution.home_member_state,
                "croatia_pea_country_check": (
                    "eu_candidate" if resolution.found else None
                ),
                "pea_geography_status": "eu_candidate",
                "pea_eligible": False,
                "match_score": resolution.match_score,
                "stored_issuer_id": stored.id if stored else None,
                "attempts": [
                    _attempt_output(attempt)
                    for attempt in resolution.attempts
                ],
                "error": (
                    resolution.error
                    or (
                        "émetteur à importer dans la watchlist avant persistance"
                        if resolution.found and stored is None
                        else None
                    )
                ),
            }
        else:
            raise ValueError(f"Source inconnue: {source}")
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
        return 0 if resolution.found and stored else 1
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.log_level)

    try:
        settings = Settings.from_env()
        max_download_mb = getattr(args, "max_download_mb", None)
        if max_download_mb is not None:
            settings = replace(
                settings,
                max_download_bytes=max_download_mb * 1024 * 1024,
            )
        if args.command == "diagnose-source":
            return diagnose_source(
                settings,
                args.source,
                dataset=args.dataset,
            )
        if args.command == "discover-source":
            return discover_source(settings, args.source, query=args.query)
        if args.command == "discover-market-documents":
            markets = (
                tuple(SUPPORTED_WATCH_MARKETS)
                if args.all
                else (normalize_market(args.market),)
            )
            export = discover_market_document_links(
                settings,
                markets=markets,
                date_from=args.date_from,
                date_to=args.date_to,
                output_format=args.format,
                output_dir=args.output_dir,
                max_candidates=args.max_candidates,
                dedupe_url=args.dedupe_url,
            )
            print(export.output_path)
            print(f"{export.documents_count} documents")
            for warning in export.warnings:
                print(f"ATTENTION {warning}", file=sys.stderr)
            for error in export.errors:
                print(f"ERREUR {error}", file=sys.stderr)
            return 1 if export.errors else 0
        if args.command == "serve":
            import uvicorn

            host = args.host or settings.web_host
            port = args.port or settings.web_port
            url = _browser_url(host, port)
            print(f"Webapp InfoFin: {url}", file=sys.stderr)
            if not args.no_open:
                threading.Timer(1.0, _open_chrome, args=(url,)).start()
            try:
                uvicorn.run(
                    "webapp.app:create_app",
                    factory=True,
                    host=host,
                    port=port,
                )
            except OSError as exc:
                if getattr(exc, "winerror", None) == 10048 or exc.errno in {
                    48,
                    98,
                    10048,
                }:
                    print(
                        f"ERREUR: le port {port} est déjà utilisé par une autre "
                        f"application.\n"
                        f"Relancez avec un autre port, par exemple:\n"
                        f"  python main.py serve --port 8766",
                        file=sys.stderr,
                    )
                    return 1
                raise
            return 0
        if args.command == "purge-web-searches":
            from datetime import timedelta

            from webapp.repositories import WebSearchRepository

            database = Database(settings.db_path)
            database.initialize_web_search_schema()
            cutoff = (
                datetime.now(UTC) - timedelta(days=args.older_than_days)
            ).isoformat(timespec="seconds")
            deleted = WebSearchRepository(database).purge_jobs_older_than(cutoff)
            print(f"{deleted} recherche(s) web supprimée(s)")
            return 0

        database = Database(settings.db_path)
        database.initialize()
        if args.command == "status":
            print(render_status(database, data_dir=settings.data_dir))
            return 0
        if args.command == "healthcheck":
            outcome = run_healthcheck(database, settings)
            print(outcome.report_path)
            return outcome.exit_code
        if args.command == "export-latest":
            export_path = export_latest_documents(
                database,
                export_format=args.format,
                since=args.since,
            )
            print(export_path)
            return 0
        if args.command == "discover-issuer":
            return discover_issuer(
                database,
                settings,
                args.source,
                symbol=args.symbol,
                name=args.name,
                isin=getattr(args, "isin", None),
            )
        if args.command == "discover-issuer-website":
            stored = database.store_issuer_website(
                isin=args.isin,
                name=args.name,
                market=args.market,
                url=args.url
            )
            output = {
                "source": "issuer_website_fallback",
                "found": stored is not None,
                "name": stored.name if stored else args.name,
                "isin": stored.isin if stored else args.isin,
                "market": stored.market if stored else "Xetra",
                "investor_relations_url": stored.investor_relations_url if stored else args.url,
                "stored_issuer_id": stored.id if stored else None,
            }
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            return 0 if stored else 1
        if args.command == "import-csv":
            return import_csv(database, args.path)
        if args.command == "import-euronext":
            return import_euronext(database, settings, url=args.url)
        if args.command == "sync-issuer-lists":
            from issuer_list_sync import sync_issuer_lists

            results = sync_issuer_lists(
                settings,
                output_dir=args.output_dir,
                market=args.market,
                import_to_db=args.import_db,
                database=database,
            )
            if args.import_db:
                import lei_resolver
                from http_client import build_http_session
                session = build_http_session(
                    retries=settings.http_retries,
                    backoff_factor=settings.http_backoff_factor,
                    user_agent=settings.user_agent,
                    verify=settings.http_verify_ssl,
                )
                try:
                    lei_resolver.sync_database_leis(database, session)
                except Exception as e:
                    LOGGER.warning("Auto LEI resolution failed after sync: %s", e)
                finally:
                    session.close()

            exit_code = 0
            for result in results:
                if result.error:
                    print(
                        f"{result.market}: ERREUR ({result.source}) — "
                        f"{result.error}"
                    )
                    exit_code = 1
                else:
                    print(
                        f"{result.market}: {result.total_rows} lignes "
                        f"({result.importable_rows} importables) "
                        f"-> {result.path}"
                    )
            return exit_code
        if args.command == "check":
            return check_documents(
                database,
                settings,
                market=args.market if not args.all else None,
            )
        if args.command == "watch":
            import lei_resolver
            from http_client import build_http_session
            session = build_http_session(
                retries=settings.http_retries,
                backoff_factor=settings.http_backoff_factor,
                user_agent=settings.user_agent,
                verify=settings.http_verify_ssl,
            )
            try:
                lei_resolver.sync_database_leis(database, session)
            except Exception as e:
                LOGGER.warning("Auto LEI resolution failed before watch: %s", e)
            finally:
                session.close()

            outcome = run_watch(
                database,
                settings,
                market=args.market if not args.all else None,
                since=args.since,
                limit=args.limit,
                dry_run=args.dry_run,
                notify_email=getattr(args, "notify_email", None),
                lookback_days=args.lookback_days,
                max_candidates_per_source=args.max_candidates_per_source,
                max_documents_per_run=args.max_documents_per_run,
                confirm_large_run=args.confirm_large_run,
                backfill=args.backfill,
                issuer_mode=args.issuer_mode,
                include_regulatory_news=args.include_regulatory_news,
                issuer_website_fallback=getattr(args, "issuer_website_fallback", False),
            )
            print(outcome.report_path)
            if outcome.notification_path:
                print(outcome.notification_path)
            return outcome.exit_code
        if args.command == "screen-higgons":
            from screener.cli import run_screen_higgons
            return run_screen_higgons(
                database,
                settings,
                market_arg=args.market,
                exchange_code_arg=args.exchange_code,
                as_of_date_arg=args.as_of_date,
                force=args.force,
                limit=args.limit,
                output_csv=args.output,
                output_json=args.json_output,
                explain_rejections=args.explain_rejections,
                min_daily_traded_eur=args.min_daily_traded_eur,
                index_symbol=args.index_symbol,
                eodhd_backend=args.eodhd_backend,
            )
        if args.command == "prefilter-higgons":
            from screener.cli import run_prefilter_higgons
            return run_prefilter_higgons(
                database,
                settings,
                market_arg=args.market,
                exchange_code_arg=args.exchange_code,
                as_of_date_arg=args.as_of_date,
                force=args.force,
                limit=args.limit,
                output_csv=args.output,
                output_json=args.json_output,
                explain_rejections=args.explain_rejections,
                min_daily_traded_eur=args.min_daily_traded_eur,
                max_market_cap_eur=args.max_market_cap_eur,
                index_symbol=args.index_symbol,
                eodhd_backend=args.eodhd_backend,
            )
        if args.command == "diagnose-eodhd":
            from screener.cli import run_diagnose_eodhd
            return run_diagnose_eodhd(settings)
        if args.command == "resolve-leis":
            import lei_resolver
            from http_client import build_http_session
            session = build_http_session(
                retries=settings.http_retries,
                backoff_factor=settings.http_backoff_factor,
                user_agent=settings.user_agent,
                verify=settings.http_verify_ssl,
            )
            try:
                updated_count = lei_resolver.sync_database_leis(database, session)
                print(f"Mise à jour terminée. {updated_count} LEIs résolus.")
                return 0
            finally:
                session.close()
        parser.error(f"Commande inconnue: {args.command}")
    except (OSError, sqlite3.Error, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        LOGGER.warning("Interrompu par l'utilisateur")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
