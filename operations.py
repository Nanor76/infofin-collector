from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from db import Database


@dataclass(frozen=True, slots=True)
class HealthcheckResult:
    source: str
    market: str
    state: str
    critical: bool
    error: str | None
    details: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HealthcheckOutcome:
    status: str
    report_path: Path
    results: tuple[HealthcheckResult, ...]

    @property
    def exit_code(self) -> int:
        return 1 if any(
            result.critical and result.state == "unavailable"
            for result in self.results
        ) else 0


def _markdown(value: object) -> str:
    return (
        str(value or "")
        .replace("|", r"\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def format_bytes(size: int) -> str:
    value = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{size} B"


def directory_size(path: str | Path) -> int:
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for item in root.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def render_status(
    database: Database,
    *,
    data_dir: str | Path,
    recent_limit: int = 10,
) -> str:
    issuer_counts = database.issuer_counts_by_market()
    document_counts = database.document_counts()
    latest_runs = database.latest_watch_runs_by_market()
    recent_documents = database.recent_documents(recent_limit)
    recent_errors = database.recent_errors(recent_limit)
    unhealthy_sources = database.unhealthy_source_states()
    raw_size = directory_size(data_dir)

    lines = [
        "# InfoFin status",
        "",
        f"- Base SQLite: `{database.path}`",
        f"- Répertoire brut: `{Path(data_dir)}`",
        f"- Taille totale data/raw: `{format_bytes(raw_size)}` ({raw_size} octets)",
        "",
        "## Émetteurs par marché",
        "",
    ]
    if issuer_counts:
        lines.extend(["| Marché | Émetteurs |", "|---|---:|"])
        lines.extend(
            f"| {_markdown(row['market'])} | {row['issuer_count']} |"
            for row in issuer_counts
        )
    else:
        lines.append("Aucun émetteur.")

    lines.extend(["", "## Documents par marché / source / type", ""])
    if document_counts:
        lines.extend(
            [
                "| Marché | Source | Type | Documents |",
                "|---|---|---|---:|",
            ]
        )
        lines.extend(
            "| "
            + " | ".join(
                (
                    _markdown(row["market"]),
                    _markdown(row["source"]),
                    _markdown(row["document_type"]),
                    str(row["document_count"]),
                )
            )
            + " |"
            for row in document_counts
        )
    else:
        lines.append("Aucun document.")

    lines.extend(["", "## Dernier watch_run par marché", ""])
    if latest_runs:
        lines.extend(
            [
                (
                    "| Marché | Début | Statut | Téléchargés | Doublons | "
                    "Trop gros | Erreurs | Rapport |"
                ),
                "|---|---|---|---:|---:|---:|---:|---|",
            ]
        )
        for row in latest_runs:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown(row["market"]),
                        _markdown(row["started_at"]),
                        _markdown(row["status"]),
                        str(row["downloaded"]),
                        str(row["duplicates"]),
                        str(row["skipped_too_large"]),
                        str(row["errors"]),
                        _markdown(row["report_path"]),
                    )
                )
                + " |"
            )
    else:
        lines.append("Aucun watch_run détaillé par marché.")

    lines.extend(["", "## Derniers téléchargements", ""])
    if recent_documents:
        lines.extend(
            [
                "| Date | Marché | Société | ISIN | Type | Fichier |",
                "|---|---|---|---|---|---|",
            ]
        )
        for row in recent_documents:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown(row["downloaded_at"]),
                        _markdown(row["market"]),
                        _markdown(row["issuer_name"]),
                        _markdown(row["isin"]),
                        _markdown(row["document_type"]),
                        _markdown(row["local_path"]),
                    )
                )
                + " |"
            )
    else:
        lines.append("Aucun téléchargement.")

    lines.extend(["", "## Dernières erreurs", ""])
    if recent_errors:
        lines.extend(
            [
                "| Date | Marché | Société | Source | Erreur |",
                "|---|---|---|---|---|",
            ]
        )
        for row in recent_errors:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown(row["created_at"]),
                        _markdown(row["market"]),
                        _markdown(row["issuer_name"] or "run"),
                        _markdown(row["source"]),
                        _markdown(row["message"]),
                    )
                )
                + " |"
            )
    else:
        lines.append("Aucune erreur enregistrée.")

    lines.extend(["", "## Sources degraded / unavailable", ""])
    if unhealthy_sources:
        lines.extend(
            [
                "| Marché | Source | État | Vérifié | Contexte | Erreur |",
                "|---|---|---|---|---|---|",
            ]
        )
        for row in unhealthy_sources:
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown(row["market"]),
                        _markdown(row["source"]),
                        _markdown(row["state"]),
                        _markdown(row["checked_at"]),
                        _markdown(row["context"]),
                        _markdown(row["error"]),
                    )
                )
                + " |"
            )
    else:
        lines.append("Aucune source en degraded/unavailable.")
    return "\n".join(lines)


def _timestamped_path(
    directory: str | Path,
    prefix: str,
    started_at: datetime,
    suffix: str,
) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    timestamp = started_at
    while True:
        path = root / f"{prefix}_{timestamp:%Y%m%d_%H%M%S}{suffix}"
        if not path.exists():
            return path
        timestamp += timedelta(seconds=1)


def write_healthcheck_report(
    results: Sequence[HealthcheckResult],
    *,
    started_at: datetime,
    ended_at: datetime,
    status: str,
    reports_dir: str | Path = "reports",
) -> Path:
    path = _timestamped_path(
        reports_dir,
        "healthcheck",
        started_at,
        ".md",
    )
    lines = [
        f"# Healthcheck InfoFin - {started_at:%Y-%m-%d %H:%M:%S %Z}",
        "",
        f"- Début: `{started_at.isoformat(timespec='seconds')}`",
        f"- Fin: `{ended_at.isoformat(timespec='seconds')}`",
        f"- Statut: `{status}`",
        f"- Sources diagnostiquées: `{len(results)}`",
        "",
        "## Résumé consolidé",
        "",
        "| Marché | Source | État | Critique | HTTP | Enregistrements | Erreur |",
        "|---|---|---|---|---:|---:|---|",
    ]
    for result in results:
        details = result.details
        detected = details.get("detected_count")
        if detected is None:
            detected = details.get("total_count")
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown(result.market),
                    _markdown(result.source),
                    _markdown(result.state),
                    "oui" if result.critical else "non",
                    _markdown(details.get("http_status") or ""),
                    _markdown(detected if detected is not None else ""),
                    _markdown(result.error),
                )
            )
            + " |"
        )

    lines.extend(["", "## Détails", ""])
    for result in results:
        lines.extend(
            [
                f"### {_markdown(result.market)} / {_markdown(result.source)}",
                "",
                "```json",
                json.dumps(
                    dict(result.details),
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                    sort_keys=True,
                ),
                "```",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


EXPORT_FIELDS = (
    "downloaded_at",
    "company",
    "isin",
    "market",
    "source",
    "source_document_id",
    "report_number",
    "document_type",
    "title",
    "published_at",
    "period_end_date",
    "reporting_year",
    "source_url",
    "local_path",
    "sha256",
    "content_type",
    "format",
    "file_size",
    "official_source",
)


def export_latest_documents(
    database: Database,
    *,
    export_format: str,
    exports_dir: str | Path = "exports",
    now: datetime | None = None,
    since: date | None = None,
) -> Path:
    normalized_format = export_format.casefold()
    if normalized_format not in {"csv", "json"}:
        raise ValueError("format d'export attendu: csv ou json")

    if since is None:
        _, rows = database.latest_documents_for_export()
    else:
        rows = database.documents_since_for_export(since)
    exported = [
        {field: row[field] for field in EXPORT_FIELDS}
        for row in rows
    ]
    timestamp = now or datetime.now(UTC)
    output_dir = Path(exports_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / (
        f"latest_documents_{timestamp:%Y%m%d}.{normalized_format}"
    )
    if normalized_format == "csv":
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            writer.writerows(exported)
    else:
        path.write_text(
            json.dumps(exported, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return path


def write_notification_email(
    *,
    recipient: str,
    run_status: str,
    summary: Mapping[str, int],
    new_documents: Iterable[object],
    source_errors: Mapping[str, str],
    report_path: str | Path,
    generated_at: datetime | None = None,
) -> Path:
    report = Path(report_path).resolve()
    message = EmailMessage()
    message["From"] = "infofin@localhost"
    message["To"] = recipient
    message["Subject"] = f"InfoFin watch: {run_status}"
    message["Date"] = format_datetime(generated_at or datetime.now(UTC))

    lines = [
        f"Statut du run: {run_status}",
        "",
        "Résumé:",
    ]
    for name, value in summary.items():
        lines.append(f"- {name}: {value}")

    lines.extend(["", "Nouveaux documents:"])
    documents = list(new_documents)
    if documents:
        for event in documents:
            issuer = getattr(event, "issuer")
            candidate = getattr(event, "candidate")
            lines.append(
                f"- {issuer.name} | {issuer.isin} | {issuer.market} | "
                f"{candidate.document_type} | {candidate.url}"
            )
    else:
        lines.append("- Aucun")

    lines.extend(["", "Sources en erreur:"])
    if source_errors:
        for source, error in sorted(source_errors.items()):
            lines.append(f"- {source}: {error}")
    else:
        lines.append("- Aucune")

    lines.extend(
        [
            "",
            f"Rapport Markdown local: {report}",
            f"Lien local: {report.as_uri()}",
            "",
        ]
    )
    message.set_content("\n".join(lines))
    output_path = report.with_suffix(".eml")
    output_path.write_bytes(message.as_bytes(policy=SMTP))
    return output_path
