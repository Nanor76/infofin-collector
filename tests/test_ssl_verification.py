from __future__ import annotations

import os
import re
import logging
import pytest
import requests

from config import Settings
from http_client import build_http_session, InfoFinSession
from watcher import WatchReport, render_watch_report, WatchStats


def test_ssl_verify_true_by_default() -> None:
    # 1. verify=True by default in InfoFinSession
    session = InfoFinSession()
    assert session.verify is True

    # 2. verify=True by default in build_http_session
    session = build_http_session(
        retries=1,
        backoff_factor=0.1,
        user_agent="TestAgent",
    )
    assert isinstance(session, InfoFinSession)
    assert session.verify is True


def test_ssl_override_explicite() -> None:
    # 1. Override explicitly via parameter to build_http_session
    session = build_http_session(
        retries=1,
        backoff_factor=0.1,
        user_agent="TestAgent",
        verify=False,
    )
    assert session.verify is False


def test_ssl_warning_reported_if_false(caplog: pytest.LogCaptureFixture) -> None:
    # 1. Setup session with verify=False
    session = build_http_session(
        retries=1,
        backoff_factor=0.1,
        user_agent="TestAgent",
        verify=False,
    )

    # Make a dummy request (will fail/mocked, but we just check the log/warnings)
    # Let's mock the actual HTTP call so it doesn't hit the network
    class FakeAdapter(requests.adapters.BaseAdapter):
        def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
            resp = requests.Response()
            resp.status_code = 200
            return resp
        def close(self):
            pass

    session.mount("https://", FakeAdapter())
    session.mount("http://", FakeAdapter())

    # Trigger request inside a source context
    with caplog.at_level(logging.WARNING, logger="http_client"):
        with session.source("test_source_one"):
            session.get("https://dummy.url")

    # Check warning is logged
    assert len(caplog.records) == 1
    assert "La vérification SSL/TLS est désactivée" in caplog.text
    assert "test_source_one" in caplog.text

    # Check ssl_disabled_sources contains the source
    assert "test_source_one" in session.ssl_disabled_sources

    # 2. Check that WatchReport registers and renders the warnings
    report = WatchReport(
        run_id=123,
        market="Test Market",
        started_at=pytest.importorskip("datetime").datetime.now(),
        ended_at=pytest.importorskip("datetime").datetime.now(),
        status="success",
        since=None,
        limit=None,
        dry_run=True,
        stats=WatchStats(),
        max_download_bytes=1000,
        ssl_disabled_sources={"test_source_one", "test_source_two"},
    )

    markdown = render_watch_report(report)
    assert "## Avertissements de sécurité TLS/SSL" in markdown
    assert "- `test_source_one` : Vérification SSL désactivée." in markdown
    assert "- `test_source_two` : Vérification SSL désactivée." in markdown
    assert "[!WARNING]" in markdown


def test_no_connector_forces_false_hardcoded() -> None:
    # Scan all python files in connectors directory
    connectors_dir = os.path.join(os.path.dirname(__file__), "..", "connectors")
    for root, dirs, files in os.walk(connectors_dir):
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                path = os.path.join(root, file)
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()

                    # Find all verify=False (ignoring spaces)
                    verify_false_matches = re.findall(r"\bverify\s*=\s*False\b", content, re.IGNORECASE)
                    assert not verify_false_matches, f"Hardcoded verify=False found in {file}: {verify_false_matches}"

                    verify_ssl_false_matches = re.findall(r"\bverify_ssl\s*=\s*False\b", content, re.IGNORECASE)
                    assert not verify_ssl_false_matches, f"Hardcoded verify_ssl=False found in {file}: {verify_ssl_false_matches}"
