import json
from datetime import date
from typing import Any

from connectors.base import ConnectorState
from connectors.france_info_financiere import (
    FranceInfoFinanciereConnector,
    detect_field_role,
)
from models import Issuer


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self) -> Any:
        return self._payload

    def close(self) -> None:
        return None


class FakeSession:
    def __init__(
        self,
        *,
        v2_payload: Any,
        v1_payload: Any | None = None,
        catalog_payload: Any | None = None,
        status_code: int = 200,
    ) -> None:
        self.v2_payload = v2_payload
        self.v1_payload = v1_payload
        self.catalog_payload = catalog_payload
        self.status_code = status_code
        self.last_url: str | None = None
        self.last_params: dict[str, Any] | None = None
        self.requests: list[tuple[str, dict[str, Any] | None]] = []

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: int,
        stream: bool = False,
    ) -> FakeResponse:
        self.last_url = url
        self.last_params = params
        self.requests.append((url, params))
        if "/exports/json" in url:
            return FakeResponse({}, status_code=self.status_code)
        if "/api/records/1.0/search/" in url:
            return FakeResponse(
                self.v1_payload if self.v1_payload is not None else {},
                status_code=self.status_code,
            )
        if url.endswith("/api/explore/v2.1/catalog/datasets"):
            return FakeResponse(
                self.catalog_payload
                if self.catalog_payload is not None
                else {"total_count": 0, "results": []},
                status_code=self.status_code,
            )
        return FakeResponse(self.v2_payload, status_code=self.status_code)


class NetworkErrorSession:
    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None,
        timeout: int,
        stream: bool = False,
    ) -> FakeResponse:
        import requests

        raise requests.Timeout("timeout")


def make_connector(session: Any) -> FranceInfoFinanciereConnector:
    return FranceInfoFinanciereConnector(
        session=session,
        base_url=(
            "https://data.economie.gouv.fr/"
            "api/explore/v2.1/catalog/datasets"
        ),
        fallback_base_urls=(),
        dataset="flux-amf-new-prod",
    )


def test_field_roles_are_detected_tolerantly() -> None:
    assert detect_field_role("ISIN de l'émetteur") == "isin"
    assert detect_field_role("Société") == "company"
    assert detect_field_role("Titre du fichier") == "title"
    assert detect_field_role("Type d'information") == "information_type"
    assert (
        detect_field_role("Sous-type d'information")
        == "information_subtype"
    )
    assert detect_field_role("url_de_recuperation") == "url"
    assert detect_field_role("Date de diffusion") == "date"
    assert detect_field_role("identificationsociete_iso_cd_isi") == "isin"
    assert detect_field_role("identificationsociete_iso_nom_soc") == "company"
    assert detect_field_role("informationdeposee_inf_tit_inf") == "title"
    assert detect_field_role("informationdeposee_inf_dat_emt") == "date"
    assert detect_field_role("uin_idt_uin") == "source_id"


def test_france_connector_parses_explore_v21_records() -> None:
    payload = {
        "total_count": 3,
        "results": [
            {
                "identifiant": "record-1",
                "filename": "Nouveau",
                "ISIN de l'émetteur": "FR0000120073",
                "Société": "Air Liquide",
                "Titre du fichier": "Rapport 2025",
                "informationdeposee_inf_tit_inf": (
                    "Rapport financier annuel 2025"
                ),
                "Type d'information": "Rapport financier annuel",
                "Sous-type d'information": "ESEF",
                "url_de_recuperation": (
                    "https://official.test/air-liquide-rfa.zip"
                ),
                "Date de publication": "2026-03-20",
            },
            {
                "ISIN": "FR0000120073",
                "Titre du fichier": "Communiqué commercial",
                "url_de_recuperation": (
                    "https://official.test/trading-update.pdf"
                ),
            },
            {
                "ISIN": "FR0000131104",
                "Type d'information": "Rapport financier annuel",
                "url_de_recuperation": "https://official.test/other.pdf",
            },
        ],
    }
    session = FakeSession(v2_payload=payload)
    connector = make_connector(session)
    issuer = Issuer(
        "Air Liquide",
        "FR0000120073",
        "AI",
        "Euronext Paris",
    )

    candidates = connector.search_documents(issuer)

    assert connector.state == ConnectorState.READY
    assert len(candidates) == 1
    assert candidates[0].document_type == "annual_financial_report"
    assert "Rapport financier annuel 2025" in candidates[0].title
    assert candidates[0].published_date.isoformat() == "2026-03-20"
    assert candidates[0].metadata["company"] == "Air Liquide"
    assert session.last_url == (
        "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
        "flux-amf-new-prod/records"
    )
    assert session.requests[0][1] == {
        "limit": 25,
        "where": (
            'search("FR0000120073") AND '
            'search("rapport financier annuel")'
        ),
    }


def test_france_recent_search_is_global_and_date_bounded() -> None:
    payload = {
        "total_count": 1,
        "results": [
            {
                "identificationsociete_iso_cd_isi": "FR0000120073",
                "identificationsociete_iso_nom_soc": "Air Liquide",
                "informationdeposee_inf_tit_inf": (
                    "Rapport financier annuel 2025"
                ),
                "informationdeposee_inf_dat_emt": "2026-03-20",
                "url_de_recuperation": (
                    "https://official.test/air-liquide-rfa.pdf"
                ),
            }
        ],
    }
    session = FakeSession(v2_payload=payload)
    connector = make_connector(session)

    candidates = connector.search_recent_documents(
        "Euronext Paris",
        since=date(2026, 3, 1),
        limit=10,
    )

    assert connector.supports_source_first
    assert len(candidates) == 1
    assert candidates[0].metadata["issuer_isins"] == ["FR0000120073"]
    params = session.requests[0][1] or {}
    assert params["where"] == (
        "informationdeposee_inf_dat_emt >= date'2026-03-01'"
    )
    assert "search(" not in params["where"]


def test_diagnostic_reports_count_fields_and_example() -> None:
    payload = {
        "total_count": 42,
        "results": [
            {"ISIN": "FR0000120073", "Société": "Air Liquide"},
            {"Titre du fichier": "Rapport financier annuel"},
        ],
    }
    connector = make_connector(FakeSession(v2_payload=payload))

    diagnostic = connector.diagnose()

    assert diagnostic.state == ConnectorState.READY
    assert diagnostic.total_count == 42
    assert diagnostic.fields == (
        "ISIN",
        "Société",
        "Titre du fichier",
    )
    assert diagnostic.example_record == payload["results"][0]
    assert [attempt.name for attempt in diagnostic.attempts] == [
        "explore_v2_records",
        "explore_v2_export_json",
        "records_v1_search",
        "catalog_search",
    ]
    assert diagnostic.attempts[0].http_status == 200
    assert diagnostic.attempts[0].total_count == 42


def test_http_failure_marks_connector_degraded_without_raising() -> None:
    connector = make_connector(
        FakeSession(v2_payload={}, status_code=503)
    )
    issuer = Issuer(
        "Air Liquide",
        "FR0000120073",
        "AI",
        "Euronext Paris",
    )

    candidates = connector.search_documents(issuer)

    assert candidates == []
    assert connector.state == ConnectorState.DEGRADED
    assert "HTTP 503" in (connector.last_error or "")


def test_network_failure_is_logged_as_network_degradation() -> None:
    connector = make_connector(NetworkErrorSession())

    diagnostic = connector.diagnose()

    assert diagnostic.state == ConnectorState.DEGRADED
    assert "réseau" in (diagnostic.error or "")


def test_invalid_payload_is_logged_as_parsing_degradation() -> None:
    connector = make_connector(
        FakeSession(v2_payload={"unexpected": []})
    )

    diagnostic = connector.diagnose()

    assert diagnostic.state == ConnectorState.DEGRADED
    assert "parsing" in (diagnostic.error or "")


def test_search_falls_back_to_records_v1() -> None:
    session = FakeSession(
        v2_payload={"unexpected": []},
        v1_payload={
            "nhits": 1,
            "records": [
                {
                    "recordid": "v1-record",
                    "fields": {
                        "identificationsociete_iso_cd_isi": "FR0000120073",
                        "informationdeposee_inf_tit_inf": (
                            "Rapport financier annuel 2025"
                        ),
                        "url_de_recuperation": "https://official.test/rfa.pdf",
                        "informationdeposee_inf_dat_emt": "2026-03-20",
                    },
                }
            ],
        },
    )
    connector = make_connector(session)
    issuer = Issuer("Air Liquide", "FR0000120073", "AI", "Euronext Paris")

    candidates = connector.search_documents(issuer)

    assert len(candidates) == 1
    assert candidates[0].source_document_id == "v1-record:1"
    assert connector.state == ConnectorState.READY


def test_discovery_lists_dataset_candidates() -> None:
    session = FakeSession(
        v2_payload={},
        catalog_payload={
            "total_count": 1,
            "results": [
                {
                    "dataset_id": "flux-amf-new-prod",
                    "records_count": 530669,
                    "metas": {
                        "default": {"title": "Flux AMF (NEW PROD)"}
                    },
                }
            ],
        },
    )
    connector = make_connector(session)

    discovery = connector.discover("flux-amf")

    assert len(discovery.candidates) == 1
    assert discovery.candidates[0].dataset_id == "flux-amf-new-prod"
    assert discovery.candidates[0].title == "Flux AMF (NEW PROD)"
    assert discovery.candidates[0].records_count == 530669


def test_technical_sentinel_date_is_ignored() -> None:
    connector = make_connector(
        FakeSession(v2_payload={"total_count": 0, "results": []})
    )

    published = connector._extract_date(
        [
            "8888-01-01T00:00:00+00:00",
            "2009-08-05T08:30:00+00:00",
        ]
    )

    assert published is not None
    assert published.isoformat() == "2009-08-05"
