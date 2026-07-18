from __future__ import annotations

import base64
import json
from typing import Protocol


class AuthorizedSession(Protocol):
    def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: int,
    ): ...


class CloudRunJobLauncher:
    def __init__(
        self,
        *,
        project: str,
        region: str,
        job_name: str,
        authorized_session: AuthorizedSession | None = None,
    ) -> None:
        self.project = project
        self.region = region
        self.job_name = job_name
        self._authorized_session = authorized_session

    def _session(self) -> AuthorizedSession:
        if self._authorized_session is None:
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "google-auth est requis pour lancer un Cloud Run Job"
                ) from exc
            credentials, _ = google.auth.default(
                scopes=("https://www.googleapis.com/auth/cloud-platform",)
            )
            self._authorized_session = AuthorizedSession(credentials)
        return self._authorized_session

    def launch(self, job_id: str) -> None:
        url = (
            "https://run.googleapis.com/v2/projects/"
            f"{self.project}/locations/{self.region}/jobs/{self.job_name}:run"
        )
        response = self._session().post(
            url,
            json={
                "overrides": {
                    "containerOverrides": [
                        {
                            "env": [
                                {
                                    "name": "INFOFIN_WEB_JOB_ID",
                                    "value": job_id,
                                }
                            ]
                        }
                    ]
                }
            },
            timeout=30,
        )
        response.raise_for_status()


class CloudTasksJobLauncher:
    """Enqueue a search on the already-warm web service."""

    def __init__(
        self,
        *,
        project: str,
        region: str,
        queue_name: str,
        service_url: str,
        username: str,
        password: str,
        authorized_session: AuthorizedSession | None = None,
    ) -> None:
        self.project = project
        self.region = region
        self.queue_name = queue_name
        self.service_url = service_url.rstrip("/")
        self.username = username
        self.password = password
        self._authorized_session = authorized_session

    def _session(self) -> AuthorizedSession:
        if self._authorized_session is None:
            try:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession
            except ImportError as exc:  # pragma: no cover - deployment dependency
                raise RuntimeError(
                    "google-auth est requis pour lancer une Cloud Task"
                ) from exc
            credentials, _ = google.auth.default(
                scopes=("https://www.googleapis.com/auth/cloud-platform",)
            )
            self._authorized_session = AuthorizedSession(credentials)
        return self._authorized_session

    def launch(self, job_id: str) -> None:
        queue_path = (
            f"projects/{self.project}/locations/{self.region}/queues/"
            f"{self.queue_name}"
        )
        credentials = f"{self.username}:{self.password}".encode("utf-8")
        authorization = base64.b64encode(credentials).decode("ascii")
        body = base64.b64encode(
            json.dumps({"job_id": job_id}, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")
        response = self._session().post(
            f"https://cloudtasks.googleapis.com/v2/{queue_path}/tasks",
            json={
                "task": {
                    "name": f"{queue_path}/tasks/{job_id}",
                    "dispatchDeadline": "1800s",
                    "httpRequest": {
                        "httpMethod": "POST",
                        "url": f"{self.service_url}/internal/search-worker",
                        "headers": {
                            "Authorization": f"Basic {authorization}",
                            "Content-Type": "application/json",
                        },
                        "body": body,
                    },
                }
            },
            timeout=30,
        )
        response.raise_for_status()
