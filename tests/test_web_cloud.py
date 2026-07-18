from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from config import Settings
from webapp.app import create_app
from webapp.cloud_jobs import CloudRunJobLauncher, CloudTasksJobLauncher
from webapp.firestore_repository import (
    FirestoreWebSearchRepository,
    InMemoryDocumentStore,
)
from webapp.jobs import CloudJobManager
from webapp.run_job import execute_persisted_search
from webapp.services.document_search import (
    LinkSearchDocument,
    LinkSearchRequest,
    MarketSearchSummary,
)


def _request() -> LinkSearchRequest:
    return LinkSearchRequest(
        markets=("Euronext Paris",),
        date_from=date(2026, 6, 1),
        date_to=date(2026, 6, 30),
    )


def _document(**overrides: object) -> LinkSearchDocument:
    values: dict[str, object] = {
        "market": "Euronext Paris",
        "source": "fake-oam",
        "source_document_id": "doc-1",
        "published_at": "2026-06-12",
        "period_end_date": "2025-12-31",
        "reporting_year": 2025,
        "document_type": "annual_financial_report",
        "classification": "regulated_information",
        "title": "Annual report",
        "url": "https://official.test/report.pdf",
        "issuer_name": "Issuer A",
        "issuer_isin": "FR0000000001",
        "issuer_lei": "",
        "category": "annual",
        "file_format": "pdf",
        "date_confidence": "high",
        "source_publication_date_raw": "",
    }
    values.update(overrides)
    return LinkSearchDocument(**values)  # type: ignore[arg-type]


def test_cloud_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("INFOFIN_WEB_STORAGE_BACKEND", "firestore")
    monkeypatch.setenv("INFOFIN_WEB_JOB_BACKEND", "cloud-tasks")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "infofin-test")
    monkeypatch.setenv("GOOGLE_CLOUD_REGION", "europe-west1")
    monkeypatch.setenv("INFOFIN_CLOUD_RUN_JOB", "infofin-search")
    monkeypatch.setenv("INFOFIN_CLOUD_TASKS_QUEUE", "infofin-search-queue")
    monkeypatch.setenv(
        "INFOFIN_WEB_SERVICE_URL",
        "https://infofin-web.example.run.app",
    )
    monkeypatch.setenv("INFOFIN_FIRESTORE_PREFIX", "test_infofin")
    monkeypatch.setenv("INFOFIN_WEB_ACCESS_USERNAME", "mobile-user")
    monkeypatch.setenv("INFOFIN_WEB_ACCESS_PASSWORD", "secret-value")
    monkeypatch.setenv("INFOFIN_BETA_USERS_JSON", '{"alice":"hash"}')
    monkeypatch.setenv("INFOFIN_BETA_SESSION_SECRET", "session-secret")
    monkeypatch.setenv("INFOFIN_BETA_DAILY_SEARCH_LIMIT", "5")
    monkeypatch.setenv("INFOFIN_WORKER_TOKEN", "worker-secret")
    monkeypatch.setenv("INFOFIN_CONTACT_EMAIL", "beta@example.test")
    monkeypatch.setenv("INFOFIN_LEGAL_PUBLISHER", "InfoFin Test")

    settings = Settings.from_env()

    assert settings.web_storage_backend == "firestore"
    assert settings.web_job_backend == "cloud-tasks"
    assert settings.google_cloud_project == "infofin-test"
    assert settings.google_cloud_region == "europe-west1"
    assert settings.cloud_run_search_job == "infofin-search"
    assert settings.cloud_tasks_queue == "infofin-search-queue"
    assert settings.web_service_url == "https://infofin-web.example.run.app"
    assert settings.firestore_collection_prefix == "test_infofin"
    assert settings.web_access_username == "mobile-user"
    assert settings.web_access_password == "secret-value"
    assert settings.web_beta_users_json == '{"alice":"hash"}'
    assert settings.web_beta_session_secret == "session-secret"
    assert settings.web_beta_daily_search_limit == 5
    assert settings.web_worker_token == "worker-secret"
    assert settings.web_contact_email == "beta@example.test"
    assert settings.web_legal_publisher == "InfoFin Test"


def test_firestore_repository_persists_filters_and_purges() -> None:
    store = InMemoryDocumentStore()
    repository = FirestoreWebSearchRepository(store=store, prefix="test_infofin")
    repository.create_job("job-1", _request())
    repository.mark_job_running("job-1")
    repository.upsert_market_run(
        "job-1",
        MarketSearchSummary(
            market="Euronext Paris",
            source="fake-oam",
            status="ok",
            candidates_returned=2,
            documents_count=2,
        ),
    )
    repository.replace_results(
        "job-1",
        (
            _document(title="B report", published_at="2026-06-10"),
            _document(
                title="A report",
                published_at="2026-06-12",
                document_type="half_year_financial_report",
                url="https://official.test/half-year.pdf",
            ),
        ),
    )
    repository.finish_job(
        "job-1",
        status="done",
        results_count=2,
        warnings=("warning",),
        errors=(),
    )

    job = repository.get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["request"].markets == ("Euronext Paris",)
    assert repository.count_results("job-1") == 2
    assert repository.list_market_runs("job-1")[0]["results_count"] == 2
    results, total = repository.list_results(
        "job-1",
        document_type="half_year_financial_report",
        q="issuer a",
        sort="title",
    )
    assert total == 1
    assert results[0]["title"] == "A report"

    repository.add_feedback(
        feedback_id="feedback-1",
        owner_id="alice",
        category="usability",
        message="Utile",
        job_id="job-1",
        created_at="2026-01-01T00:00:00+00:00",
    )
    assert repository.purge_feedback_older_than(
        "2026-02-01T00:00:00+00:00"
    ) == 1

    assert repository.purge_jobs_older_than("9999-01-01T00:00:00+00:00") == 1
    assert repository.get_job("job-1") is None
    assert store.paths() == ()


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None


class _FakeAuthorizedSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object], int]] = []

    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: int,
    ) -> _FakeResponse:
        self.calls.append((url, json, timeout))
        return _FakeResponse()


def test_cloud_run_launcher_overrides_the_search_job_id() -> None:
    session = _FakeAuthorizedSession()
    launcher = CloudRunJobLauncher(
        project="infofin-test",
        region="europe-west1",
        job_name="infofin-search",
        authorized_session=session,  # type: ignore[arg-type]
    )

    launcher.launch("job-123")

    assert session.calls == [
        (
            "https://run.googleapis.com/v2/projects/infofin-test/locations/"
            "europe-west1/jobs/infofin-search:run",
            {
                "overrides": {
                    "containerOverrides": [
                        {"env": [{"name": "INFOFIN_WEB_JOB_ID", "value": "job-123"}]}
                    ]
                }
            },
            30,
        )
    ]


def test_cloud_tasks_launcher_targets_the_warm_service() -> None:
    session = _FakeAuthorizedSession()
    launcher = CloudTasksJobLauncher(
        project="infofin-test",
        region="europe-west1",
        queue_name="infofin-search-queue",
        service_url="https://infofin-web.example.run.app/",
        username="infofin",
        password="secret-value",
        authorized_session=session,  # type: ignore[arg-type]
    )

    launcher.launch("job-123")

    assert len(session.calls) == 1
    url, payload, timeout = session.calls[0]
    assert url == (
        "https://cloudtasks.googleapis.com/v2/projects/infofin-test/locations/"
        "europe-west1/queues/infofin-search-queue/tasks"
    )
    assert timeout == 30
    task = payload["task"]
    assert isinstance(task, dict)
    assert task["name"].endswith("/tasks/job-123")
    assert task["dispatchDeadline"] == "1800s"
    request = task["httpRequest"]
    assert isinstance(request, dict)
    assert request["httpMethod"] == "POST"
    assert request["url"] == (
        "https://infofin-web.example.run.app/internal/search-worker"
    )
    expected_basic = base64.b64encode(b"infofin:secret-value").decode("ascii")
    assert request["headers"] == {
        "Authorization": f"Basic {expected_basic}",
        "Content-Type": "application/json",
    }
    assert json.loads(base64.b64decode(request["body"])) == {"job_id": "job-123"}


def test_cloud_tasks_launcher_uses_a_dedicated_beta_worker_token() -> None:
    session = _FakeAuthorizedSession()
    launcher = CloudTasksJobLauncher(
        project="infofin-test",
        region="europe-west1",
        queue_name="infofin-search-queue",
        service_url="https://infofin-web.example.run.app/",
        worker_token="worker-secret",
        authorized_session=session,  # type: ignore[arg-type]
    )

    launcher.launch("job-beta")

    request = session.calls[0][1]["task"]["httpRequest"]
    assert request["headers"] == {
        "X-InfoFin-Worker-Token": "worker-secret",
        "Content-Type": "application/json",
    }
    assert "Authorization" not in request["headers"]


class _RecordingLauncher:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.job_ids: list[str] = []

    def launch(self, job_id: str) -> None:
        self.job_ids.append(job_id)
        if self.error:
            raise self.error


def test_cloud_job_manager_persists_before_dispatch() -> None:
    repository = FirestoreWebSearchRepository(
        store=InMemoryDocumentStore(),
        prefix="test_infofin",
    )
    launcher = _RecordingLauncher()
    manager = CloudJobManager(repository=repository, launcher=launcher)

    job_id = manager.submit(_request())

    assert launcher.job_ids == [job_id]
    status = manager.get_status(job_id)
    assert status is not None
    assert status["status"] == "queued"


def test_cloud_job_manager_marks_dispatch_failure() -> None:
    repository = FirestoreWebSearchRepository(
        store=InMemoryDocumentStore(),
        prefix="test_infofin",
    )
    manager = CloudJobManager(
        repository=repository,
        launcher=_RecordingLauncher(RuntimeError("dispatch unavailable")),
    )

    job_id = manager.submit(_request())

    status = manager.get_status(job_id)
    assert status is not None
    assert status["status"] == "failed"
    assert status["errors"] == ["Impossible de démarrer la recherche distante."]


class _WorkerSearchService:
    def __init__(self) -> None:
        self.calls = 0

    def search_links(self, request: LinkSearchRequest):
        from webapp.services.document_search import LinkSearchResultSet

        self.calls += 1

        return LinkSearchResultSet(
            request=request,
            documents=(_document(),),
            market_summaries=(
                MarketSearchSummary(
                    market="Euronext Paris",
                    source="fake-oam",
                    status="ok",
                    documents_count=1,
                ),
            ),
        )


def test_cloud_worker_executes_the_persisted_request() -> None:
    repository = FirestoreWebSearchRepository(
        store=InMemoryDocumentStore(),
        prefix="test_infofin",
    )
    repository.create_job("job-1", _request())

    execute_persisted_search(
        job_id="job-1",
        repository=repository,
        search_service=_WorkerSearchService(),  # type: ignore[arg-type]
    )

    job = repository.get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["results_count"] == 1


def test_cloud_tasks_worker_endpoint_executes_once(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "must-not-exist.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=1,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024,
        amf_base_url="https://unused.example.test",
        amf_fallback_base_urls=(),
        amf_dataset="unused",
        amf_rows=1,
        web_storage_backend="firestore",
        web_job_backend="cloud-tasks",
        google_cloud_project="infofin-test",
        cloud_tasks_queue="infofin-search-queue",
        web_service_url="https://infofin-web.example.run.app",
    )
    repository = FirestoreWebSearchRepository(
        store=InMemoryDocumentStore(),
        prefix="test_infofin",
    )
    repository.create_job("job-1", _request())
    search_service = _WorkerSearchService()
    manager = CloudJobManager(
        repository=repository,
        launcher=_RecordingLauncher(),
    )
    app = create_app(
        settings=settings,
        repository=repository,
        job_manager=manager,
        search_service=search_service,
    )

    with TestClient(app) as client:
        first = client.post("/internal/search-worker", json={"job_id": "job-1"})
        second = client.post("/internal/search-worker", json={"job_id": "job-1"})

    assert first.status_code == 204
    assert second.status_code == 204
    assert search_service.calls == 1
    job = repository.get_job("job-1")
    assert job is not None
    assert job["status"] == "done"
    assert job["results_count"] == 1


def test_cloud_app_uses_shared_repository_without_sqlite(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "must-not-exist.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=1,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024,
        amf_base_url="https://unused.example.test",
        amf_fallback_base_urls=(),
        amf_dataset="unused",
        amf_rows=1,
        web_storage_backend="firestore",
        web_job_backend="cloud-run",
        google_cloud_project="infofin-test",
    )
    class CountingRepository(FirestoreWebSearchRepository):
        def __init__(self) -> None:
            super().__init__(store=InMemoryDocumentStore(), prefix="test_infofin")
            self.list_results_calls = 0

        def list_results(self, *args, **kwargs):
            self.list_results_calls += 1
            return super().list_results(*args, **kwargs)

    repository = CountingRepository()
    manager = CloudJobManager(
        repository=repository,
        launcher=_RecordingLauncher(),
    )
    app = create_app(
        settings=settings,
        repository=repository,
        job_manager=manager,
    )

    with TestClient(app) as client:
        health = client.get("/api/health")
        response = client.post(
            "/api/searches",
            json={
                "markets": ["Euronext Paris"],
                "date_from": "2026-06-01",
                "date_to": "2026-06-30",
                "document_types": ["annual_financial_report"],
            },
        )
        partial = client.get(
            f"/partials/searches/{response.json()['job_id']}/results"
        )

    assert health.json() == {
        "status": "ok",
        "storage_backend": "firestore",
        "job_backend": "cloud-run",
    }
    assert response.status_code == 200
    assert partial.status_code == 200
    assert repository.list_results_calls == 0
    assert repository.get_job(response.json()["job_id"]) is not None
    assert not settings.db_path.exists()


def test_google_cloud_deployment_assets_keep_free_tier_guards() -> None:
    root = Path(__file__).parents[1]
    script = (root / "deploy" / "google-cloud.ps1").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8")

    assert "[switch]$Public" in script
    assert "[switch]$Performance" in script
    assert "[switch]$WarmWorker" in script
    assert "[string]$AccessPasswordSecret" in script
    assert "[string]$BetaUsersSecret" in script
    assert "[string]$BetaSessionSecret" in script
    assert "[string]$WorkerTokenSecret" in script
    assert "--no-allow-unauthenticated" in script
    assert "$WebMinInstances = if ($Performance) { 1 } else { 0 }" in script
    assert '"--min-instances=$WebMinInstances"' in script
    assert "$SearchCpu = if ($Performance) { 2 } else { 1 }" in script
    assert '$SearchMemory = if ($Performance) { "1Gi" } else { "512Mi" }' in script
    assert "--cpu=$SearchCpu" in script
    assert "--memory=$SearchMemory" in script
    assert 'if ($Performance) {' in script
    assert '$WebDeployArguments += "--cpu-boost"' in script
    assert "--max-instances=1" in script
    assert "--min=0" not in script
    assert "--max=1" not in script
    assert "INFOFIN_WEB_MAX_CANDIDATES=1000" in script
    assert "POLAND_KNF_OAM_MAX_PAGES_PER_DATE=25" in script
    assert "INFOFIN_WEB_STORAGE_BACKEND=firestore" in script
    assert "cloudtasks.googleapis.com" in script
    assert "roles/cloudtasks.enqueuer" in script
    assert "INFOFIN_WEB_JOB_BACKEND=cloud-tasks" in script
    assert "INFOFIN_CLOUD_TASKS_QUEUE=" in script
    assert "INFOFIN_WEB_SERVICE_URL=" in script
    assert "--max-concurrent-dispatches=1" in script
    assert "--max-attempts=1" in script
    assert '$WebTimeout = if ($WarmWorker) { "1800s" } else { "300s" }' in script
    assert '"--timeout=$WebTimeout"' in script
    assert "INFOFIN_WEB_RETENTION_DAYS=30" in script
    assert "secretmanager.googleapis.com" in script
    assert "roles/secretmanager.secretAccessor" in script
    assert "--set-secrets=INFOFIN_WEB_ACCESS_PASSWORD=" in script
    assert "INFOFIN_BETA_USERS_JSON=" in script
    assert "INFOFIN_BETA_SESSION_SECRET=" in script
    assert "INFOFIN_WORKER_TOKEN=" in script
    assert "INFOFIN_BETA_DAILY_SEARCH_LIMIT=" in script
    assert "--edition=standard" not in script
    assert "'--args=-m,webapp.run_job'" in script
    assert "'--args=-m,webapp.purge_firestore'" in script
    assert "builds submit --ignore-file=.dockerignore --tag=$Image ." in script
    assert 'CMD ["python", "-m", "webapp.server"]' in dockerfile
    assert ".env" in dockerignore.splitlines()
    assert ".codex-remote-attachments" in dockerignore.splitlines()
