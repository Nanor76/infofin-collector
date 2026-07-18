from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from classification import classify_document
from config import Settings
from connectors import SUPPORTED_WATCH_MARKETS, connector_for_market
from connectors.base import DocumentCandidate
from http_client import build_http_session
from load_watchlist import normalize_market

NEGATIVE_TERMS = (
    "managers transaction",
    "voting rights",
    "general meeting",
    "dividend announcement",
    "investor presentation",
    "presentation",
    "investor conference",
    "conference webinar",
    "investor webinar",
    "webinar",
    "invitation",
    "press release",
    "financial calendar",
    "webcast",
    "factsheet",
    "net asset value",
    "nav",
    "dashboard",
    "ucits",
    "kid",
    "priips",
    "announces the date of publication",
    "date of publication of the financial report",
    "financial calendar",
    "rapport consiliul de administratie",
    "ran501",
    "ran502",
    "oppdatering volum",
    "volume update",
)

PERIODIC_TYPES = frozenset(
    {
        "annual_financial_report",
        "half_year_financial_report",
        "quarterly_financial_report",
        "universal_registration_document",
        "financial_report",
        "year_end_report",
        "audit_report",
        "esef",
        "other_regulatory_announcement",
    }
)

OTHER_BATCH = (
    "Euronext Paris",
    "Euronext Amsterdam",
    "Euronext Brussels",
    "Euronext Growth Brussels",
    "Euronext Lisbon",
    "Euronext Dublin",
    "Nasdaq Copenhagen",
    "Nasdaq Helsinki",
)

REMAINING_BATCH = (
    "Oslo Børs",
    "Euronext Milan",
    "Euronext Star Milan",
    "Euronext Growth Milan",
    "Euronext MIV Milan",
    "Bolsa de Madrid",
    "Bolsa de Barcelona",
    "Bolsa de Bilbao",
    "Bolsa de Valencia",
    "BME Growth",
    "BME Scaleup",
    "Nasdaq Stockholm",
    "Nordic Growth Market",
)

ALL_MARKETS = SUPPORTED_WATCH_MARKETS


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    ascii_text = "".join(
        char for char in decomposed if not unicodedata.combining(char)
    )
    return re.sub(r"\s+", " ", ascii_text.casefold()).strip()


def _market_slug(market: str) -> str:
    slug = market.casefold().replace(" ", "_")
    return re.sub(r"[^a-z0-9_øæåäöü]", "", slug)


def _safe_metadata(metadata: dict[str, object] | None) -> dict[str, object]:
    if not metadata:
        return {}
    safe: dict[str, object] = {}
    for key, value in metadata.items():
        if str(key).startswith("_"):
            continue
        try:
            json.dumps(value)
        except TypeError:
            safe[str(key)] = str(value)
        else:
            safe[str(key)] = value
    return safe


def _category_from_candidate(candidate: DocumentCandidate) -> str:
    metadata = candidate.metadata or {}
    parts = [
        str(metadata.get("category") or ""),
        str(metadata.get("regulatory_category") or ""),
        str(metadata.get("regulatory_category_description") or ""),
        str(metadata.get("topic") or ""),
        str(metadata.get("reporting_topic") or ""),
        str(metadata.get("document_type_en") or ""),
        str(metadata.get("document_type_label") or ""),
    ]
    return " ".join(part for part in parts if part).strip()


def _guess_from_title(
    title: str,
    *,
    category: str = "",
    market: str = "",
) -> tuple[str | None, str]:
    haystack = _normalize(f"{title} {category}")
    for term in NEGATIVE_TERMS:
        normalized_term = _normalize(term)
        if (
            normalized_term == "general meeting"
            and "annual report" in haystack
        ):
            continue
        if len(normalized_term) <= 3:
            if re.search(rf"\b{re.escape(normalized_term)}\b", haystack):
                return "other_regulatory_announcement", f"negative:{term}"
        elif normalized_term in haystack:
            return "other_regulatory_announcement", f"negative:{term}"

    market_key = market.casefold()
    if market_key in {
        "euronext milan",
        "euronext star milan",
        "euronext growth milan",
        "euronext miv milan",
    }:
        from connectors.italy_emarketstorage import classify_italy_document

        guess = classify_italy_document(title, category or None, "")
        if guess:
            return guess, guess
    elif market_key in {"nasdaq stockholm", "nordic growth market"}:
        from connectors.sweden_fi import classify_sweden_document

        guess, reason, _, _ = classify_sweden_document(
            title,
            category,
            "",
        )
        return guess, reason
    elif market_key == "nasdaq copenhagen":
        from connectors.denmark_dfsa_oam import classify_denmark_document

        guess, reason, _, _ = classify_denmark_document(
            title,
            category,
            "",
        )
        return guess, reason
    elif market_key == "euronext dublin":
        from connectors.ireland_euronext_direct import _financial_type

        guess = _financial_type(category, title, "")
        if guess:
            return guess, guess
    elif market_key == "oslo børs":
        from connectors.oslo_newsweb import _attachment_type

        guess = _attachment_type(title, category, "")
        if guess:
            return guess, title

    title_only = classify_document(title, "")
    if title_only and title_only != "esef":
        return title_only, title

    for source_text, label in (
        (category, category),
        (f"{category} {title}".strip(), category or title),
    ):
        if not source_text:
            continue
        guess = classify_document(source_text, "")
        if guess and guess != "esef":
            return guess, label

    return None, ""


def _audit_status(document_type: str, guess: str | None) -> str:
    if guess is None:
        return "NO_TITLE_SIGNAL"
    if guess == document_type:
        return "MATCH"
    return "CONFLICT"


@dataclass(slots=True)
class AuditRow:
    market: str
    source: str
    published_at: str
    document_type: str
    title_guess: str
    guess_reason: str
    title: str
    category: str
    classification_reason: str
    url: str
    source_document_id: str
    metadata: str
    status: str


def _candidate_row(
    market: str,
    candidate: DocumentCandidate,
) -> AuditRow:
    category = _category_from_candidate(candidate)
    guess, reason = _guess_from_title(
        candidate.title,
        category=category,
        market=market,
    )
    publication = candidate.published_at or candidate.published_date
    return AuditRow(
        market=market,
        source=candidate.source,
        published_at=publication.isoformat() if publication else "",
        document_type=candidate.document_type,
        title_guess=guess or "",
        guess_reason=reason,
        title=candidate.title,
        category=category,
        classification_reason=candidate.classification_reason or "",
        url=candidate.url,
        source_document_id=candidate.source_document_id or "",
        metadata=json.dumps(
            _safe_metadata(candidate.metadata),
            ensure_ascii=False,
        ),
        status=_audit_status(candidate.document_type, guess),
    )


def audit_market(
    settings: Settings,
    market: str,
    *,
    since: date,
    limit: int,
    batch: str,
    output_dir: Path,
) -> dict[str, object]:
    normalized = normalize_market(market)
    session = build_http_session(
        retries=settings.http_retries,
        backoff_factor=settings.http_backoff_factor,
        user_agent=settings.user_agent,
        verify=settings.http_verify_ssl,
    )
    rows: list[AuditRow] = []
    error = ""
    source = ""
    try:
        connector = connector_for_market(
            normalized,
            settings=settings,
            session=session,
        )
        if connector is None:
            error = "aucun connecteur"
        elif not getattr(connector, "supports_source_first", False):
            error = "source-first non supporté"
        else:
            source = connector.source_name
            candidates = connector.search_recent_documents(
                normalized,
                since=since,
                limit=limit,
            )
            unique: dict[tuple[str, str], DocumentCandidate] = {}
            for candidate in candidates:
                publication = candidate.published_at or candidate.published_date
                if publication is not None and publication < since:
                    continue
                key = (
                    candidate.source,
                    candidate.source_document_id or candidate.url,
                )
                unique.setdefault(key, candidate)
            rows = [
                _candidate_row(normalized, candidate)
                for candidate in unique.values()
            ]
    except Exception as exc:
        error = str(exc)
    finally:
        session.close()

    title_signals = sum(1 for row in rows if row.title_guess)
    conflicts = sum(1 for row in rows if row.status == "CONFLICT")
    no_title_signal = sum(
        1
        for row in rows
        if row.status == "NO_TITLE_SIGNAL"
        and row.document_type != "other_regulatory_announcement"
    )
    summary = {
        "market": normalized,
        "status": "error" if error else "ok",
        "source": source,
        "candidates": len(rows),
        "title_signals": title_signals,
        "conflicts": conflicts,
        "no_title_signal": no_title_signal,
        "error": error,
    }
    payload = {
        "since": since.isoformat(),
        "limit": limit,
        "summary": summary,
        "rows": [asdict(row) for row in rows],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    slug = _market_slug(normalized)
    stem = f"classification_audit_{batch}_{slug}"
    json_path = output_dir / f"{stem}.json"
    csv_path = output_dir / f"{stem}.csv"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(asdict(rows[0]).keys()))
            writer.writeheader()
            for row in rows:
                writer.writerow(asdict(row))
    else:
        csv_path.write_text("", encoding="utf-8")

    print(
        f"{normalized}: {summary['status']} "
        f"candidates={summary['candidates']} "
        f"signals={summary['title_signals']} "
        f"conflicts={summary['conflicts']}"
        + (f" error={error}" if error else ""),
        file=sys.stderr,
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit document_type vs title-based classification guess.",
    )
    parser.add_argument(
        "--batch",
        choices=("other", "remaining", "all"),
        default="all",
    )
    parser.add_argument("--since", type=date.fromisoformat, default="2025-01-01")
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--market", action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)

    if args.market:
        markets = tuple(normalize_market(item) for item in args.market)
        batch_name = "custom"
    elif args.batch == "other":
        markets = OTHER_BATCH
        batch_name = "other"
    elif args.batch == "remaining":
        markets = REMAINING_BATCH
        batch_name = "remaining"
    else:
        markets = ALL_MARKETS
        batch_name = "all"

    settings = Settings.from_env()
    exit_code = 0
    for market in markets:
        summary = audit_market(
            settings,
            market,
            since=args.since,
            limit=args.limit,
            batch=batch_name,
            output_dir=args.output_dir,
        )
        if summary.get("status") == "error":
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
