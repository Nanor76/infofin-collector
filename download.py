from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlparse

import requests

from classification import supported_extension
from connectors.base import DocumentCandidate
from db import Database
from models import Issuer

LOGGER = logging.getLogger(__name__)


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DownloadResult:
    status: str
    path: Path | None
    sha256: str | None
    file_size: int
    message: str | None = None


def safe_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_value = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_value).strip("._-")
    return cleaned.casefold() or "unknown"


def _content_disposition_filename(header: str) -> str | None:
    if not header:
        return None
    utf8_match = re.search(r"filename\*=UTF-8''([^;]+)", header, re.IGNORECASE)
    if utf8_match:
        return unquote(utf8_match.group(1).strip())
    basic_match = re.search(r'filename="?([^";]+)"?', header, re.IGNORECASE)
    return basic_match.group(1).strip() if basic_match else None


class DocumentDownloader:
    def __init__(
        self,
        *,
        database: Database,
        session: requests.Session,
        data_dir: str | Path,
        timeout: int,
        max_download_bytes: int,
    ) -> None:
        self.database = database
        self.session = session
        self.data_dir = Path(data_dir)
        self.timeout = timeout
        self.max_download_bytes = max_download_bytes

    def download(
        self,
        issuer: Issuer,
        candidate: DocumentCandidate,
    ) -> DownloadResult:
        if issuer.id is None:
            raise DownloadError(f"Émetteur non persisté: {issuer.isin}")

        existing_url = self.database.get_document_by_source_url(candidate.url)
        if existing_url:
            return DownloadResult(
                status="duplicate",
                path=Path(existing_url["local_path"]),
                sha256=existing_url["sha256"],
                file_size=existing_url["file_size"],
            )

        try:
            if (
                candidate.source == "cmvm_sdi"
                and candidate.metadata.get("cmvm_download_kind")
            ):
                from connectors.portugal_cmvm_sdi import fetch_cmvm_download

                response = fetch_cmvm_download(
                    self.session,
                    candidate,
                    timeout=self.timeout,
                )
            else:
                referer = ""
                if candidate.source == "romania_asf_oam":
                    referer = str(
                        candidate.metadata.get("parent_page_url")
                        or candidate.metadata.get("romania_asf_oam_url")
                        or ""
                    ).strip()
                session_headers = getattr(self.session, "headers", None)
                if referer and session_headers is not None:
                    prior_referer = session_headers.get("Referer")
                    session_headers["Referer"] = referer
                    try:
                        response = self.session.get(
                            candidate.url,
                            stream=True,
                            timeout=self.timeout,
                        )
                    finally:
                        if prior_referer is None:
                            session_headers.pop("Referer", None)
                        else:
                            session_headers["Referer"] = prior_referer
                else:
                    response = self.session.get(
                        candidate.url,
                        stream=True,
                        timeout=self.timeout,
                    )
                response.raise_for_status()
        except (requests.RequestException, RuntimeError) as exc:
            raise DownloadError(
                f"Téléchargement impossible pour {candidate.url}: {exc}"
            ) from exc

        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        disposition_name = _content_disposition_filename(
            response.headers.get("Content-Disposition", "")
        )
        extension = self._detect_extension(
            candidate.url,
            content_type,
            disposition_name
            or str(candidate.metadata.get("filename") or "")
            or None,
        )
        if candidate.source in {
            "oekb_oam",
            "slovenia_oam",
            "estonia_oam",
            "latvia_oam",
            "lithuania_oam",
            "slovakia_nbs_ceri",
            "romania_asf_oam",
            "bulgaria_bse_x3news",
            "malta_mse_oam",
        }:
            original_name = (
                disposition_name
                or str(candidate.metadata.get("filename") or "")
            )
            original_extension = (
                PurePosixPath(original_name).suffix.casefold().lstrip(".")
            )
            if original_extension in {
                "pdf",
                "zip",
                "xhtml",
                "xht",
                "xbri",
                "xbrl",
            }:
                extension = original_extension
        if not extension:
            response.close()
            raise DownloadError(
                f"Format non pris en charge pour {candidate.url} "
                f"(Content-Type: {content_type or 'inconnu'})"
            )

        declared_size = response.headers.get("Content-Length")
        if declared_size:
            try:
                declared_size_int = int(declared_size)
            except ValueError:
                LOGGER.warning(
                    "Content-Length invalide ignoré pour %s: %r",
                    candidate.url,
                    declared_size,
                )
            else:
                if declared_size_int > self.max_download_bytes:
                    response.close()
                    return DownloadResult(
                        status="skipped_too_large",
                        path=None,
                        sha256=None,
                        file_size=declared_size_int,
                        message=(
                            f"Document trop volumineux "
                            f"({declared_size_int} octets, limite "
                            f"{self.max_download_bytes} octets)"
                        ),
                    )

        temp_dir = self.data_dir.parent / ".tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        file_size = 0
        temp_path: Path | None = None
        too_large = False

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=temp_dir,
                prefix="infofin-",
                suffix=".part",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    file_size += len(chunk)
                    if file_size > self.max_download_bytes:
                        too_large = True
                        break
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            raise
        finally:
            response.close()

        if too_large:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            return DownloadResult(
                status="skipped_too_large",
                path=None,
                sha256=None,
                file_size=file_size,
                message=(
                    f"Document trop volumineux ({file_size} octets observés, "
                    f"limite {self.max_download_bytes} octets)"
                ),
            )

        sha256 = digest.hexdigest()
        existing_sha = self.database.get_document_by_sha256(sha256)
        if existing_sha:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            self.database.add_document_url_alias(
                source_url=candidate.url,
                sha256=sha256,
            )
            return DownloadResult(
                status="duplicate",
                path=Path(existing_sha["local_path"]),
                sha256=sha256,
                file_size=file_size,
            )

        published = candidate.published_date or date.today()
        if candidate.source == "issuer_website_fallback":
            market_directory = "germany_issuer_website"
        else:
            market_directory = (
                "italy"
                if issuer.market.casefold()
                in {
                    "euronext milan",
                    "euronext star milan",
                    "euronext growth milan",
                    "euronext miv milan",
                }
                else "netherlands"
                if issuer.market.casefold() == "euronext amsterdam"
                else "belgium"
                if issuer.market.casefold()
                in {"euronext brussels", "euronext growth brussels"}
                else "portugal"
                if issuer.market.casefold() == "euronext lisbon"
                else "ireland"
                if issuer.market.casefold() == "euronext dublin"
                else "spain"
                if issuer.market.casefold() in {
                    "bolsa de madrid",
                    "bolsa de barcelona",
                    "bolsa de bilbao",
                    "bolsa de valencia",
                    "bme growth",
                    "bme scaleup",
                }
                else "sweden"
                if issuer.market.casefold() in {
                    "nasdaq stockholm",
                    "nordic growth market",
                }
                else "denmark"
                if issuer.market.casefold() == "nasdaq copenhagen"
                else "finland"
                if issuer.market.casefold() == "nasdaq helsinki"
                else "austria"
                if issuer.market.casefold() == "vienna stock exchange"
                else "poland"
                if issuer.market.casefold() == "warsaw stock exchange"
                else "czechia"
                if issuer.market.casefold() == "prague stock exchange"
                else "croatia"
                if issuer.market.casefold() == "zagreb stock exchange"
                else "slovenia"
                if issuer.market.casefold() == "ljubljana stock exchange"
                else "estonia"
                if issuer.market.casefold() == "tallinn stock exchange"
                else "latvia"
                if issuer.market.casefold() == "riga stock exchange"
                else "lithuania"
                if issuer.market.casefold() == "vilnius stock exchange"
                else "slovakia"
                if issuer.market.casefold() == "bratislava stock exchange"
                else "romania"
                if issuer.market.casefold() == "bucharest stock exchange"
                else "bulgaria"
                if issuer.market.casefold() == "bulgarian stock exchange"
                else "malta"
                if issuer.market.casefold() == "malta stock exchange"
                else safe_component(issuer.market)
            )
        target_dir = (
            self.data_dir
            / market_directory
            / issuer.isin
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        if candidate.source in {
            "oekb_oam",
            "knf_oam",
            "czechia_cnb_curi",
            "croatia_hanfa_srpi",
            "slovenia_oam",
            "estonia_oam",
            "latvia_oam",
            "lithuania_oam",
            "slovakia_nbs_ceri",
            "romania_asf_oam",
            "bulgaria_bse_x3news",
            "malta_mse_oam",
        }:
            original_stem = safe_component(
                PurePosixPath(
                    str(candidate.metadata.get("filename") or "document")
                ).stem
            )[:100]
            file_id = safe_component(
                str(candidate.metadata.get("file_id") or sha256[:8])
            )
            filename = (
                f"{published.isoformat()}_"
                f"{safe_component(candidate.document_type)}_"
                f"{original_stem}_{file_id}.{extension}"
            )
        else:
            filename = (
                f"{published.isoformat()}_"
                f"{safe_component(candidate.document_type)}_"
                f"{sha256[:8]}.{extension}"
            )
        target_path = target_dir / filename

        if temp_path is None:
            raise DownloadError("Aucun fichier temporaire n'a été créé")
        os.replace(temp_path, target_path)

        inserted = self.database.add_document(
            issuer_id=issuer.id,
            candidate=candidate,
            local_path=str(target_path),
            sha256=sha256,
            content_type=content_type or None,
            file_size=file_size,
        )
        if not inserted:
            target_path.unlink(missing_ok=True)
            existing = self.database.get_document_by_sha256(sha256)
            if existing:
                self.database.add_document_url_alias(
                    source_url=candidate.url,
                    sha256=sha256,
                )
            return DownloadResult(
                status="duplicate",
                path=Path(existing["local_path"]) if existing else None,
                sha256=sha256,
                file_size=file_size,
            )

        LOGGER.info("Document enregistré: %s", target_path)
        return DownloadResult(
            status="downloaded",
            path=target_path,
            sha256=sha256,
            file_size=file_size,
        )

    @staticmethod
    def _detect_extension(
        url: str,
        content_type: str,
        disposition_name: str | None,
    ) -> str | None:
        if disposition_name:
            by_disposition = supported_extension(disposition_name, content_type)
            if by_disposition:
                return by_disposition
        return supported_extension(url, content_type)
