import hashlib
from datetime import date
from pathlib import Path
from typing import Iterator

from connectors.base import DocumentCandidate
from db import Database
from download import DocumentDownloader
from models import Issuer


class FakeDownloadResponse:
    status_code = 200
    headers = {
        "Content-Type": "application/pdf",
        "Content-Length": "14",
    }

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield b"%PDF-test-data"

    def close(self) -> None:
        return None


class FakeDownloadSession:
    def get(
        self,
        url: str,
        *,
        stream: bool,
        timeout: int,
    ) -> FakeDownloadResponse:
        return FakeDownloadResponse()


class OversizedResponse(FakeDownloadResponse):
    headers = {"Content-Type": "application/pdf"}

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        yield b"123456"
        yield b"789012"


class OversizedSession(FakeDownloadSession):
    def get(
        self,
        url: str,
        *,
        stream: bool,
        timeout: int,
    ) -> OversizedResponse:
        return OversizedResponse()


def test_download_stores_expected_path_and_deduplicates(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    issuer = database.list_issuers()[0]
    candidate = DocumentCandidate(
        title="Rapport financier annuel 2025",
        url="https://official.test/report.pdf",
        published_date=date(2025, 12, 31),
        document_type="annual_financial_report",
        source="test",
    )
    downloader = DocumentDownloader(
        database=database,
        session=FakeDownloadSession(),  # type: ignore[arg-type]
        data_dir=tmp_path / "raw",
        timeout=10,
        max_download_bytes=1024,
    )

    first = downloader.download(issuer, candidate)
    second = downloader.download(issuer, candidate)

    expected_hash = hashlib.sha256(b"%PDF-test-data").hexdigest()
    assert first.status == "downloaded"
    assert first.path == (
        tmp_path
        / "raw"
        / "euronext_paris"
        / "FR0000120073"
        / f"2025-12-31_annual_financial_report_{expected_hash[:8]}.pdf"
    )
    assert first.path.is_file()
    assert second.status == "duplicate"
    assert second.sha256 == expected_hash


def test_oversized_download_removes_temporary_file(tmp_path: Path) -> None:
    database = Database(tmp_path / "infofin.sqlite3")
    database.initialize()
    database.upsert_issuers(
        [Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")]
    )
    issuer = database.list_issuers()[0]
    candidate = DocumentCandidate(
        title="Rapport financier annuel 2025",
        url="https://official.test/report.pdf",
        published_date=date(2025, 12, 31),
        document_type="annual_financial_report",
        source="test",
    )
    downloader = DocumentDownloader(
        database=database,
        session=OversizedSession(),  # type: ignore[arg-type]
        data_dir=tmp_path / "raw",
        timeout=10,
        max_download_bytes=10,
    )

    result = downloader.download(issuer, candidate)

    assert result.status == "skipped_too_large"
    assert result.file_size == 12
    assert result.path is None
    temp_dir = tmp_path / ".tmp"
    assert not temp_dir.exists() or list(temp_dir.iterdir()) == []
