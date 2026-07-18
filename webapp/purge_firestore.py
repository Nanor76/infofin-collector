from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from dotenv import load_dotenv

from config import Settings
from webapp.firestore_repository import (
    FirestoreWebSearchRepository,
    GoogleFirestoreDocumentStore,
)


def main() -> int:
    load_dotenv()
    settings = Settings.from_env()
    if settings.web_storage_backend != "firestore":
        raise RuntimeError("La purge Cloud Run nécessite le backend Firestore")
    if not settings.google_cloud_project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT est requis")
    retention_days = max(1, int(os.getenv("INFOFIN_WEB_RETENTION_DAYS", "30")))
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(
        timespec="seconds"
    )
    repository = FirestoreWebSearchRepository(
        store=GoogleFirestoreDocumentStore(project=settings.google_cloud_project),
        prefix=settings.firestore_collection_prefix,
    )
    deleted = repository.purge_jobs_older_than(cutoff)
    feedback_cutoff = (
        datetime.now(UTC) - timedelta(days=365)
    ).isoformat(timespec="seconds")
    deleted_feedback = repository.purge_feedback_older_than(feedback_cutoff)
    print(f"{deleted} recherche(s) Firestore supprimée(s)")
    print(f"{deleted_feedback} retour(s) bêta Firestore supprimé(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
