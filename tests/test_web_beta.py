from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from config import Settings
from db import Database
from webapp.app import create_app
from webapp.beta_access import BetaAuthenticator, hash_password, verify_password
from webapp.jobs import _status_from_repository
from webapp.repositories import WebSearchRepository
from webapp.services.document_search import LinkSearchRequest


def _users_json(password: str = "correct horse battery") -> str:
    return json.dumps(
        {
            "alice": {
                "display_name": "Alice",
                "password_hash": hash_password(
                    password,
                    salt=b"0123456789abcdef",
                    iterations=1_000,
                ),
            }
        }
    )


def _two_users_json() -> str:
    return json.dumps(
        {
            username: {
                "display_name": username.title(),
                "password_hash": hash_password(
                    f"{username} secure password",
                    salt=(username * 16).encode("utf-8")[:16],
                    iterations=1_000,
                ),
            }
            for username in ("alice", "bob")
        }
    )


class BetaJobManager:
    def __init__(self, repository: WebSearchRepository) -> None:
        self.repository = repository
        self.sequence = 0

    def submit(
        self,
        request: LinkSearchRequest,
        owner_id: str | None = None,
    ) -> str:
        self.sequence += 1
        job_id = f"beta-job-{self.sequence}"
        self.repository.create_job(job_id, request, owner_id=owner_id)
        self.repository.finish_job(
            job_id,
            status="done",
            results_count=0,
            warnings=(),
            errors=(),
        )
        return job_id

    def get_status(self, job_id: str):
        return _status_from_repository(self.repository, job_id)

    def cancel(self, job_id: str) -> bool:
        return False

    def shutdown(self) -> None:
        return None


def _beta_client(tmp_path: Path, *, limit: int = 3) -> TestClient:
    settings = Settings(
        db_path=tmp_path / "beta.sqlite3",
        data_dir=tmp_path / "raw",
        http_timeout_seconds=10,
        http_retries=0,
        http_backoff_factor=0,
        user_agent="test",
        max_download_bytes=1024,
        amf_base_url="https://example.test",
        amf_fallback_base_urls=(),
        amf_dataset="test",
        amf_rows=10,
        web_beta_users_json=_two_users_json(),
        web_beta_session_secret="s" * 32,
        web_beta_daily_search_limit=limit,
        web_worker_token="w" * 32,
        web_contact_email="beta@example.test",
        web_legal_publisher="InfoFin Test",
        web_service_url="https://infofin.example.run.app",
    )
    database = Database(settings.db_path)
    database.initialize_web_search_schema()
    repository = WebSearchRepository(database)
    app = create_app(
        settings=settings,
        database=database,
        repository=repository,
        job_manager=BetaJobManager(repository),
    )
    return TestClient(app, base_url="https://testserver")


def _login(client: TestClient, username: str = "alice"):
    response = client.post(
        "/login",
        data={
            "username": username,
            "password": f"{username} secure password",
            "next": "/",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def test_beta_passwords_are_hashed_and_verified() -> None:
    encoded = hash_password(
        "correct horse battery",
        salt=b"0123456789abcdef",
        iterations=1_000,
    )

    assert "correct horse battery" not in encoded
    assert verify_password("correct horse battery", encoded)
    assert not verify_password("wrong password", encoded)


def test_beta_session_is_signed_and_expires() -> None:
    authenticator = BetaAuthenticator(
        users_json=_users_json(),
        session_secret="s" * 32,
        session_hours=1,
    )
    user = authenticator.authenticate("ALICE", "correct horse battery")
    assert user is not None

    token = authenticator.create_session(user, now=1_000)

    assert authenticator.read_session(token, now=4_599) == user
    assert authenticator.read_session(token, now=4_601) is None
    assert authenticator.read_session(f"{token}corrompu", now=1_001) is None


def test_beta_authentication_rejects_unknown_or_invalid_users() -> None:
    authenticator = BetaAuthenticator(
        users_json=_users_json(),
        session_secret="s" * 32,
    )

    assert authenticator.authenticate("bob", "correct horse battery") is None
    assert authenticator.authenticate("alice", "incorrect password") is None


def test_beta_login_protects_the_app_but_keeps_legal_pages_public(
    tmp_path: Path,
) -> None:
    client = _beta_client(tmp_path)

    anonymous_page = client.get("/", follow_redirects=False)
    anonymous_api = client.get("/api/health")
    login_page = client.get("/login")
    mentions = client.get("/legal/mentions")
    invalid = client.post(
        "/login",
        data={"username": "alice", "password": "wrong", "next": "/"},
        follow_redirects=False,
    )

    assert anonymous_page.status_code == 303
    assert anonymous_page.headers["location"] == "/login?next=%2F"
    assert anonymous_api.status_code == 401
    assert login_page.status_code == 200
    assert 'data-testid="login-form"' in login_page.text
    assert mentions.status_code == 200
    assert "InfoFin Test" in mentions.text
    assert invalid.status_code == 303
    assert "error=1" in invalid.headers["location"]

    logged_in = _login(client)
    session_cookie = logged_in.headers["set-cookie"]
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "SameSite=lax" in session_cookie
    home = client.get("/")
    assert home.status_code == 200
    assert 'data-testid="layout-beta-user">Alice<' in home.text
    assert 'data-testid="search-beta-quota-state"' in home.text


def test_beta_quota_limits_searches_per_user_over_24_hours(
    tmp_path: Path,
) -> None:
    client = _beta_client(tmp_path, limit=2)
    _login(client)
    payload = {
        "markets": ["Euronext Paris"],
        "date_from": "2026-07-01",
        "date_to": "2026-07-18",
    }

    first = client.post("/api/searches", json=payload)
    second = client.post("/api/searches", json=payload)
    refused = client.post("/api/searches", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert refused.status_code == 429
    assert refused.headers["retry-after"] == "3600"
    assert "2 recherches sur 24 heures" in refused.json()["detail"]


def test_beta_users_cannot_read_another_users_search(tmp_path: Path) -> None:
    client = _beta_client(tmp_path)
    _login(client, "alice")
    created = client.post(
        "/api/searches",
        json={
            "markets": ["Euronext Paris"],
            "date_from": "2026-07-01",
            "date_to": "2026-07-18",
        },
    ).json()
    client.post("/logout")
    _login(client, "bob")

    assert client.get(f"/api/searches/{created['job_id']}").status_code == 404
    assert client.get(f"/searches/{created['job_id']}").status_code == 404


def test_beta_feedback_is_attributed_to_the_authenticated_user(
    tmp_path: Path,
) -> None:
    client = _beta_client(tmp_path)
    _login(client)
    created = client.post(
        "/api/searches",
        json={
            "markets": ["Euronext Paris"],
            "date_from": "2026-07-01",
            "date_to": "2026-07-18",
        },
    ).json()

    response = client.post(
        "/api/feedback",
        json={
            "category": "missing",
            "message": "Le rapport attendu est absent.",
            "job_id": created["job_id"],
        },
    )

    assert response.status_code == 201
    with client.app.state.database.connect() as connection:
        feedback = connection.execute(
            "SELECT * FROM web_beta_feedback"
        ).fetchone()
    assert feedback["owner_id"] == "alice"
    assert feedback["job_id"] == created["job_id"]
    assert feedback["category"] == "missing"
    assert feedback["message"] == "Le rapport attendu est absent."


def test_beta_worker_requires_its_dedicated_token(tmp_path: Path) -> None:
    client = _beta_client(tmp_path)

    missing = client.post(
        "/internal/search-worker", json={"job_id": "unknown"}
    )
    wrong = client.post(
        "/internal/search-worker",
        json={"job_id": "unknown"},
        headers={"X-InfoFin-Worker-Token": "wrong"},
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
