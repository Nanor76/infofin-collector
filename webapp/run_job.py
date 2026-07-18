from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from config import Settings
from webapp.firestore_repository import (
    FirestoreWebSearchRepository,
    GoogleFirestoreDocumentStore,
)
from webapp.jobs import run_stored_search
from webapp.services.document_search import DocumentSearchService


def execute_persisted_search(
    *,
    job_id: str,
    repository,
    search_service: DocumentSearchService,
) -> None:
    job = repository.get_job(job_id)
    if job is None:
        raise ValueError(f"Recherche inconnue: {job_id}")
    run_stored_search(
        repository=repository,
        search_service=search_service,
        job_id=job_id,
        request=job["request"],
    )


def main() -> int:
    load_dotenv()
    settings = Settings.from_env()
    if settings.web_storage_backend != "firestore":
        raise RuntimeError("Le worker Cloud Run nécessite le backend Firestore")
    if not settings.google_cloud_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT est requis")
    job_id = os.getenv("INFOFIN_WEB_JOB_ID", "").strip()
    if not job_id:
        raise RuntimeError("INFOFIN_WEB_JOB_ID est requis")
    repository = FirestoreWebSearchRepository(
        store=GoogleFirestoreDocumentStore(project=settings.google_cloud_project),
        prefix=settings.firestore_collection_prefix,
    )
    execute_persisted_search(
        job_id=job_id,
        repository=repository,
        search_service=DocumentSearchService(settings),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
