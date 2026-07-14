from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} doit être un entier, reçu: {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(
            f"{name} doit être un nombre, reçu: {value!r}"
        ) from exc


def _env_urls(name: str, default: str) -> tuple[str, ...]:
    value = os.getenv(name, default)
    return tuple(
        item.strip().rstrip("/")
        for item in value.replace(",", ";").split(";")
        if item.strip()
    )


@dataclass(frozen=True, slots=True)
class Settings:
    db_path: Path
    data_dir: Path
    http_timeout_seconds: int
    http_retries: int
    http_backoff_factor: float
    user_agent: str
    max_download_bytes: int
    amf_base_url: str
    amf_fallback_base_urls: tuple[str, ...]
    amf_dataset: str
    amf_rows: int
    http_verify_ssl: bool = True
    euronext_regulated_list_url: str = (
        "https://live.euronext.com/en/product_directory/data/"
        "stocks-euronext-regulated/download?mics=MTAA%2CXAMS%2CXBRU%2CXLDN%2C"
        "XLIS%2CXMSM%2CXOSL%2CXPAR"
    )
    oslo_euronext_news_url: str = (
        "https://live.euronext.com/en/markets/oslo/equities/company-news"
    )
    oslo_newsweb_base_url: str = "https://newsweb.oslobors.no"
    oslo_rate_limit_seconds: float = 0.5
    oslo_lookback_days: int = 400
    italy_home_url: str = "https://www.emarketstorage.it/"
    italy_press_releases_url: str = (
        "https://www.emarketstorage.it/it/comunicati-finanziari"
    )
    italy_documents_url: str = "https://www.emarketstorage.it/it/documenti"
    italy_1info_url: str = "https://www.1info.it/PORTALE1INFO"
    italy_borsa_company_base_url: str = (
        "https://www.borsaitaliana.it/borsa/azioni/scheda"
    )
    italy_rate_limit_seconds: float = 0.5
    italy_lookback_days: int = 400
    italy_verify_ssl: bool = True
    italy_max_pages: int = 2
    netherlands_afm_register_url: str = (
        "https://www.afm.nl/en/sector/registers/meldingenregisters/"
        "financiele-verslaggeving"
    )
    netherlands_afm_export_type: str = (
        "e8825b05-4004-4301-b736-651e8c61053d"
    )
    netherlands_home_member_state_url: str = (
        "https://www.afm.nl/en/sector/registers/meldingenregisters/"
        "home-member-state"
    )
    netherlands_home_member_state_export_type: str = (
        "6b365727-6220-452f-83b1-86a179d70d12"
    )
    netherlands_rate_limit_seconds: float = 0.2
    netherlands_lookback_days: int = 900
    belgium_fsma_stori_base_url: str = "https://www.fsma.be/en/stori"
    belgium_rate_limit_seconds: float = 0.2
    belgium_lookback_days: int = 900
    portugal_cmvm_base_url: str = "https://www.cmvm.pt/PInstitucional"
    portugal_cmvm_sdi_url: str = (
        "https://www.cmvm.pt/PInstitucional/Content?"
        "Input=BD77C8DEEB2702712300D99098915461C2A4F65FE4368A561E6AB83D1E580C4D"
    )
    portugal_rate_limit_seconds: float = 0.5
    portugal_lookback_days: int = 900
    ireland_euronext_direct_base_url: str = "https://direct.euronext.com"
    ireland_euronext_dublin_url: str = (
        "https://www.euronext.com/en/about-euronext/markets/dublin"
    )
    ireland_rate_limit_seconds: float = 0.5
    ireland_lookback_days: int = 900
    spain_cnmv_base_url: str = "https://www.cnmv.es"
    spain_cnmv_lookback_days: int = 30
    spain_rate_limit_seconds: float = 0.5
    spain_verify_ssl: bool = True
    spain_bme_listed_companies_url: str = "https://www.bolsasymercados.es"
    sweden_fi_base_url: str = "https://finanscentralen.fi.se"
    sweden_fi_lookback_days: int = 30
    sweden_rate_limit_seconds: float = 0.5
    sweden_verify_ssl: bool = True
    sweden_nasdaq_listed_companies_url: str = "https://www.nasdaqomxnordic.com"
    denmark_dfsa_base_url: str = (
        "https://www.dfsa.dk/financial-themes/capital-market/company-announcements"
    )
    denmark_dfsa_lookback_days: int = 30
    denmark_rate_limit_seconds: float = 0.5
    denmark_verify_ssl: bool = True
    denmark_nasdaq_listed_companies_url: str = "https://www.nasdaqomxnordic.com"
    finland_oam_base_url: str = "https://www.oam.fi"
    finland_oam_lookback_days: int = 30
    finland_rate_limit_seconds: float = 0.5
    finland_verify_ssl: bool = True
    austria_oekb_feed_url: str = (
        "https://my.oekb.at/issuer-info/rest/public/meldedaten/iic"
    )
    austria_oekb_download_base_url: str = (
        "https://my.oekb.at/issuer-info/rest/public/meldedaten/download"
    )
    austria_oekb_issuer_list_url: str = (
        "https://my.oekb.at/kapitalmarkt-services/kms-output/oamn/iic/list"
    )
    austria_oekb_lookback_days: int = 30
    austria_oekb_rate_limit_seconds: float = 0.2
    austria_oekb_verify_ssl: bool = True
    poland_knf_oam_base_url: str = "https://moam.knf.gov.pl/moam.nsf"
    poland_knf_oam_lookback_days: int = 30
    poland_knf_oam_rate_limit_seconds: float = 0.2
    poland_knf_oam_verify_ssl: bool = True
    poland_knf_oam_max_pages_per_date: int = 10
    czechia_cnb_oam_start_url: str = (
        "https://oam.cnb.cz/sipresextdad/SIPRESWEB.WEB21.START_INPUT_OAM?p_lang=en"
    )
    czechia_cnb_oam_search_url: str = (
        "https://oam.cnb.cz/xmlpserver/OAM_CNB_CZ/R1_RES.xdo?par_lang=en_US&_xf=xml"
    )
    czechia_cnb_oam_download_base_url: str = (
        "https://oam.cnb.cz/sipresextdad/SIPRESWEB.BIP00.DWNL_FILE"
    )
    czechia_cnb_oam_lookback_days: int = 30
    czechia_cnb_oam_rate_limit_seconds: float = 0.5
    czechia_cnb_oam_verify_ssl: bool = True
    croatia_hanfa_srpi_base_url: str = "https://www.hanfa.hr"
    croatia_hanfa_srpi_lookback_days: int = 30
    croatia_hanfa_srpi_rate_limit_seconds: float = 0.5
    croatia_hanfa_srpi_verify_ssl: bool = True
    slovenia_oam_base_url: str = "https://www.oam.si"
    slovenia_oam_lookback_days: int = 30
    slovenia_oam_rate_limit_seconds: float = 0.5
    slovenia_oam_verify_ssl: bool = True
    slovenia_oam_max_pages: int = 10
    estonia_oam_base_url: str = "https://oam.fi.ee"
    estonia_oam_lookback_days: int = 90
    estonia_oam_rate_limit_seconds: float = 0.5
    estonia_oam_verify_ssl: bool = True
    estonia_oam_max_pages: int = 10
    latvia_oam_base_url: str = "https://csri.investinfo.lv"
    latvia_oam_lookback_days: int = 90
    latvia_oam_rate_limit_seconds: float = 0.5
    latvia_oam_verify_ssl: bool = True
    latvia_oam_max_pages: int = 10
    lithuania_oam_base_url: str = "https://www.oam.lt"
    lithuania_oam_lookback_days: int = 90
    lithuania_oam_rate_limit_seconds: float = 0.5
    lithuania_oam_verify_ssl: bool = True
    lithuania_oam_max_pages: int = 10
    slovakia_nbs_ceri_base_url: str = "https://ceri.nbs.sk"
    slovakia_nbs_ceri_lookback_days: int = 120
    slovakia_nbs_ceri_rate_limit_seconds: float = 0.5
    slovakia_nbs_ceri_verify_ssl: bool = True
    romania_asf_oam_base_url: str = "https://oam.asfromania.ro"
    romania_asf_oam_lookback_days: int = 365
    romania_asf_oam_rate_limit_seconds: float = 0.5
    romania_asf_oam_verify_ssl: bool = True
    romania_asf_oam_max_pages: int = 100
    bulgaria_bse_x3news_base_url: str = "https://download.bse-sofia.bg"
    bulgaria_bse_x3news_lookback_days: int = 365
    bulgaria_bse_x3news_rate_limit_seconds: float = 0.5
    bulgaria_bse_x3news_verify_ssl: bool = True
    bulgaria_bse_x3news_max_active_buckets: int = 3
    bulgaria_bse_x3news_max_issuer_scans: int = 30
    bulgaria_bse_x3news_max_candidates_per_source: int = 40
    malta_mse_oam_base_url: str = "https://www.borzamalta.com.mt"
    malta_mse_oam_lookback_days: int = 365
    malta_mse_oam_rate_limit_seconds: float = 0.5
    malta_mse_oam_verify_ssl: bool = True
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    web_workers: int = 2
    web_max_period_days: int = 370
    web_max_candidates: int = 100000

    @classmethod
    def from_env(cls) -> "Settings":
        max_download_mb = _env_int("MAX_DOWNLOAD_MB", 100)
        http_verify_ssl = os.getenv("HTTP_VERIFY_SSL", "true").lower() != "false"
        italy_verify_env = os.getenv("ITALY_VERIFY_SSL")
        if italy_verify_env is None:
            italy_verify_env = os.getenv("ITALY_EMARKET_VERIFY_SSL")
        if italy_verify_env is not None:
            italy_verify_ssl = italy_verify_env.lower() != "false"
        else:
            italy_verify_ssl = http_verify_ssl

        return cls(
            db_path=Path(os.getenv("INFOFIN_DB_PATH", "data/infofin.sqlite3")),
            data_dir=Path(os.getenv("INFOFIN_DATA_DIR", "data/raw")),
            http_timeout_seconds=_env_int("HTTP_TIMEOUT_SECONDS", 30),
            http_retries=_env_int("HTTP_RETRIES", 3),
            http_backoff_factor=float(os.getenv("HTTP_BACKOFF_FACTOR", "0.8")),
            user_agent=os.getenv(
                "HTTP_USER_AGENT",
                "InfoFin/1.0 (+local financial disclosure monitor)",
            ),
            max_download_bytes=max_download_mb * 1024 * 1024,
            amf_base_url=os.getenv(
                "AMF_ODS_BASE_URL",
                "https://www.info-financiere.gouv.fr",
            ),
            amf_fallback_base_urls=_env_urls(
                "AMF_ODS_FALLBACK_BASE_URLS",
                "https://www.info-financiere.gouv.fr;"
                "https://data.economie.gouv.fr",
            ),
            amf_dataset=os.getenv("AMF_ODS_DATASET", "flux-amf-new-prod"),
            amf_rows=_env_int("AMF_ODS_ROWS", 100),
            euronext_regulated_list_url=os.getenv(
                "EURONEXT_REGULATED_LIST_URL",
                "https://live.euronext.com/en/product_directory/data/"
                "stocks-euronext-regulated/download?mics=MTAA%2CXAMS%2CXBRU%2CXLDN%2C"
                "XLIS%2CXMSM%2CXOSL%2CXPAR",
            ),
            oslo_euronext_news_url=os.getenv(
                "OSLO_EURONEXT_NEWS_URL",
                "https://live.euronext.com/en/markets/oslo/equities/"
                "company-news",
            ),
            oslo_newsweb_base_url=os.getenv(
                "OSLO_NEWSWEB_BASE_URL",
                "https://newsweb.oslobors.no",
            ).rstrip("/"),
            oslo_rate_limit_seconds=max(
                0.0,
                _env_float("OSLO_RATE_LIMIT_SECONDS", 0.5),
            ),
            oslo_lookback_days=_env_int("OSLO_LOOKBACK_DAYS", 400),
            italy_home_url=os.getenv(
                "ITALY_EMARKET_HOME_URL",
                "https://www.emarketstorage.it/",
            ),
            italy_press_releases_url=os.getenv(
                "ITALY_PRESS_RELEASES_URL",
                "https://www.emarketstorage.it/it/comunicati-finanziari",
            ),
            italy_documents_url=os.getenv(
                "ITALY_DOCUMENTS_URL",
                "https://www.emarketstorage.it/it/documenti",
            ),
            italy_1info_url=os.getenv(
                "ITALY_1INFO_URL",
                "https://www.1info.it/PORTALE1INFO",
            ),
            italy_borsa_company_base_url=os.getenv(
                "ITALY_BORSA_COMPANY_BASE_URL",
                "https://www.borsaitaliana.it/borsa/azioni/scheda",
            ),
            italy_rate_limit_seconds=max(
                0.0,
                _env_float("ITALY_RATE_LIMIT_SECONDS", 0.5),
            ),
            italy_lookback_days=_env_int("ITALY_LOOKBACK_DAYS", 400),
            http_verify_ssl=http_verify_ssl,
            italy_verify_ssl=italy_verify_ssl,
            italy_max_pages=max(1, _env_int("ITALY_MAX_PAGES", 2)),
            netherlands_afm_register_url=os.getenv(
                "NETHERLANDS_AFM_REGISTER_URL",
                "https://www.afm.nl/en/sector/registers/"
                "meldingenregisters/financiele-verslaggeving",
            ),
            netherlands_afm_export_type=os.getenv(
                "NETHERLANDS_AFM_EXPORT_TYPE",
                "e8825b05-4004-4301-b736-651e8c61053d",
            ),
            netherlands_home_member_state_url=os.getenv(
                "NETHERLANDS_HOME_MEMBER_STATE_URL",
                "https://www.afm.nl/en/sector/registers/"
                "meldingenregisters/home-member-state",
            ),
            netherlands_home_member_state_export_type=os.getenv(
                "NETHERLANDS_HOME_MEMBER_STATE_EXPORT_TYPE",
                "6b365727-6220-452f-83b1-86a179d70d12",
            ),
            netherlands_rate_limit_seconds=max(
                0.0,
                _env_float("NETHERLANDS_RATE_LIMIT_SECONDS", 0.2),
            ),
            netherlands_lookback_days=max(
                1,
                _env_int("NETHERLANDS_LOOKBACK_DAYS", 900),
            ),
            belgium_fsma_stori_base_url=os.getenv(
                "BELGIUM_FSMA_STORI_BASE_URL",
                "https://www.fsma.be/en/stori",
            ).rstrip("/"),
            belgium_rate_limit_seconds=max(
                0.0,
                _env_float("BELGIUM_RATE_LIMIT_SECONDS", 0.2),
            ),
            belgium_lookback_days=max(
                1,
                _env_int("BELGIUM_LOOKBACK_DAYS", 900),
            ),
            portugal_cmvm_base_url=os.getenv(
                "PORTUGAL_CMVM_BASE_URL",
                "https://www.cmvm.pt/PInstitucional",
            ).rstrip("/"),
            portugal_cmvm_sdi_url=os.getenv(
                "PORTUGAL_CMVM_SDI_URL",
                "https://www.cmvm.pt/PInstitucional/Content?"
                "Input=BD77C8DEEB2702712300D99098915461C2A4F65FE4368A561E6AB83D1E580C4D",
            ),
            portugal_rate_limit_seconds=max(
                0.0,
                _env_float("PORTUGAL_RATE_LIMIT_SECONDS", 0.5),
            ),
            portugal_lookback_days=max(
                1,
                _env_int("PORTUGAL_LOOKBACK_DAYS", 900),
            ),
            ireland_euronext_direct_base_url=os.getenv(
                "IRELAND_EURONEXT_DIRECT_BASE_URL",
                "https://direct.euronext.com",
            ).rstrip("/"),
            ireland_euronext_dublin_url=os.getenv(
                "IRELAND_EURONEXT_DUBLIN_URL",
                "https://www.euronext.com/en/about-euronext/markets/dublin",
            ),
            ireland_rate_limit_seconds=max(
                0.0,
                _env_float("IRELAND_RATE_LIMIT_SECONDS", 0.5),
            ),
            ireland_lookback_days=max(
                1,
                _env_int("IRELAND_LOOKBACK_DAYS", 900),
            ),
            spain_cnmv_base_url=os.getenv(
                "SPAIN_CNMV_BASE_URL",
                "https://www.cnmv.es",
            ).rstrip("/"),
            spain_cnmv_lookback_days=max(
                1,
                _env_int("SPAIN_CNMV_LOOKBACK_DAYS", 30),
            ),
            spain_rate_limit_seconds=max(
                0.0,
                _env_float("SPAIN_RATE_LIMIT_SECONDS", 0.5),
            ),
            spain_verify_ssl=os.getenv("SPAIN_VERIFY_SSL", "true").lower() != "false",
            spain_bme_listed_companies_url=os.getenv(
                "SPAIN_BME_LISTED_COMPANIES_URL",
                "https://www.bolsasymercados.es",
            ).rstrip("/"),
            sweden_fi_base_url=os.getenv(
                "SWEDEN_FI_BASE_URL",
                "https://finanscentralen.fi.se",
            ).rstrip("/"),
            sweden_fi_lookback_days=max(
                1,
                _env_int("SWEDEN_FI_LOOKBACK_DAYS", 30),
            ),
            sweden_rate_limit_seconds=max(
                0.0,
                _env_float("SWEDEN_RATE_LIMIT_SECONDS", 0.5),
            ),
            sweden_verify_ssl=os.getenv("SWEDEN_VERIFY_SSL", "true").lower() != "false",
            sweden_nasdaq_listed_companies_url=os.getenv(
                "SWEDEN_NASDAQ_LISTED_COMPANIES_URL",
                "https://www.nasdaqomxnordic.com",
            ).rstrip("/"),
            denmark_dfsa_base_url=os.getenv(
                "DENMARK_DFSA_BASE_URL",
                "https://www.dfsa.dk/financial-themes/capital-market/"
                "company-announcements",
            ).rstrip("/"),
            denmark_dfsa_lookback_days=max(
                1,
                _env_int("DENMARK_DFSA_LOOKBACK_DAYS", 30),
            ),
            denmark_rate_limit_seconds=max(
                0.0,
                _env_float("DENMARK_RATE_LIMIT_SECONDS", 0.5),
            ),
            denmark_verify_ssl=os.getenv(
                "DENMARK_VERIFY_SSL", "true"
            ).lower() != "false",
            denmark_nasdaq_listed_companies_url=os.getenv(
                "DENMARK_NASDAQ_LISTED_COMPANIES_URL",
                "https://www.nasdaqomxnordic.com",
            ).rstrip("/"),
            finland_oam_base_url=os.getenv(
                "FINLAND_OAM_BASE_URL",
                "https://www.oam.fi",
            ).rstrip("/"),
            finland_oam_lookback_days=max(
                1,
                _env_int("FINLAND_OAM_LOOKBACK_DAYS", 30),
            ),
            finland_rate_limit_seconds=max(
                0.0,
                _env_float("FINLAND_RATE_LIMIT_SECONDS", 0.5),
            ),
            finland_verify_ssl=os.getenv(
                "FINLAND_VERIFY_SSL", "true"
            ).lower() != "false",
            austria_oekb_feed_url=os.getenv(
                "AUSTRIA_OEKB_FEED_URL",
                "https://my.oekb.at/issuer-info/rest/public/meldedaten/iic",
            ),
            austria_oekb_download_base_url=os.getenv(
                "AUSTRIA_OEKB_DOWNLOAD_BASE_URL",
                "https://my.oekb.at/issuer-info/rest/public/meldedaten/download",
            ).rstrip("/"),
            austria_oekb_issuer_list_url=os.getenv(
                "AUSTRIA_OEKB_ISSUER_LIST_URL",
                "https://my.oekb.at/kapitalmarkt-services/"
                "kms-output/oamn/iic/list",
            ),
            austria_oekb_lookback_days=max(
                1,
                _env_int("AUSTRIA_OEKB_LOOKBACK_DAYS", 30),
            ),
            austria_oekb_rate_limit_seconds=max(
                0.0,
                _env_float("AUSTRIA_OEKB_RATE_LIMIT_SECONDS", 0.2),
            ),
            austria_oekb_verify_ssl=os.getenv(
                "AUSTRIA_OEKB_VERIFY_SSL", "true"
            ).lower() != "false",
            poland_knf_oam_base_url=os.getenv(
                "POLAND_KNF_OAM_BASE_URL",
                "https://moam.knf.gov.pl/moam.nsf",
            ).rstrip("/"),
            poland_knf_oam_lookback_days=max(
                1,
                _env_int("POLAND_KNF_OAM_LOOKBACK_DAYS", 30),
            ),
            poland_knf_oam_rate_limit_seconds=max(
                0.0,
                _env_float("POLAND_KNF_OAM_RATE_LIMIT_SECONDS", 0.2),
            ),
            poland_knf_oam_verify_ssl=os.getenv(
                "POLAND_KNF_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            poland_knf_oam_max_pages_per_date=max(
                1,
                _env_int("POLAND_KNF_OAM_MAX_PAGES_PER_DATE", 10),
            ),
            czechia_cnb_oam_start_url=os.getenv(
                "CZECHIA_CNB_OAM_START_URL",
                "https://oam.cnb.cz/sipresextdad/SIPRESWEB.WEB21.START_INPUT_OAM?p_lang=en",
            ),
            czechia_cnb_oam_search_url=os.getenv(
                "CZECHIA_CNB_OAM_SEARCH_URL",
                "https://oam.cnb.cz/xmlpserver/OAM_CNB_CZ/R1_RES.xdo?par_lang=en_US&_xf=xml",
            ),
            czechia_cnb_oam_download_base_url=os.getenv(
                "CZECHIA_CNB_OAM_DOWNLOAD_BASE_URL",
                "https://oam.cnb.cz/sipresextdad/SIPRESWEB.BIP00.DWNL_FILE",
            ).rstrip("/"),
            czechia_cnb_oam_lookback_days=max(
                1,
                _env_int("CZECHIA_CNB_OAM_LOOKBACK_DAYS", 30),
            ),
            czechia_cnb_oam_rate_limit_seconds=max(
                0.0,
                _env_float("CZECHIA_CNB_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            czechia_cnb_oam_verify_ssl=os.getenv(
                "CZECHIA_CNB_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            croatia_hanfa_srpi_base_url=os.getenv(
                "CROATIA_HANFA_SRPI_BASE_URL",
                "https://www.hanfa.hr",
            ).rstrip("/"),
            croatia_hanfa_srpi_lookback_days=max(
                1,
                _env_int("CROATIA_HANFA_SRPI_LOOKBACK_DAYS", 30),
            ),
            croatia_hanfa_srpi_rate_limit_seconds=max(
                0.0,
                _env_float("CROATIA_HANFA_SRPI_RATE_LIMIT_SECONDS", 0.5),
            ),
            croatia_hanfa_srpi_verify_ssl=os.getenv(
                "CROATIA_HANFA_SRPI_VERIFY_SSL", "true"
            ).lower() != "false",
            slovenia_oam_base_url=os.getenv(
                "SLOVENIA_OAM_BASE_URL",
                "https://www.oam.si",
            ).rstrip("/"),
            slovenia_oam_lookback_days=max(
                1,
                _env_int("SLOVENIA_OAM_LOOKBACK_DAYS", 30),
            ),
            slovenia_oam_rate_limit_seconds=max(
                0.0,
                _env_float("SLOVENIA_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            slovenia_oam_verify_ssl=os.getenv(
                "SLOVENIA_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            slovenia_oam_max_pages=max(
                1,
                _env_int("SLOVENIA_OAM_MAX_PAGES", 10),
            ),
            estonia_oam_base_url=os.getenv(
                "ESTONIA_OAM_BASE_URL",
                "https://oam.fi.ee",
            ).rstrip("/"),
            estonia_oam_lookback_days=max(
                1,
                _env_int("ESTONIA_OAM_LOOKBACK_DAYS", 90),
            ),
            estonia_oam_rate_limit_seconds=max(
                0.0,
                _env_float("ESTONIA_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            estonia_oam_verify_ssl=os.getenv(
                "ESTONIA_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            estonia_oam_max_pages=max(
                1,
                _env_int("ESTONIA_OAM_MAX_PAGES", 10),
            ),
            latvia_oam_base_url=os.getenv(
                "LATVIA_OAM_BASE_URL",
                "https://csri.investinfo.lv",
            ).rstrip("/"),
            latvia_oam_lookback_days=max(
                1,
                _env_int("LATVIA_OAM_LOOKBACK_DAYS", 90),
            ),
            latvia_oam_rate_limit_seconds=max(
                0.0,
                _env_float("LATVIA_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            latvia_oam_verify_ssl=os.getenv(
                "LATVIA_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            latvia_oam_max_pages=max(
                1,
                _env_int("LATVIA_OAM_MAX_PAGES", 10),
            ),
            lithuania_oam_base_url=os.getenv(
                "LITHUANIA_OAM_BASE_URL",
                "https://www.oam.lt",
            ).rstrip("/"),
            lithuania_oam_lookback_days=max(
                1,
                _env_int("LITHUANIA_OAM_LOOKBACK_DAYS", 90),
            ),
            lithuania_oam_rate_limit_seconds=max(
                0.0,
                _env_float("LITHUANIA_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            lithuania_oam_verify_ssl=os.getenv(
                "LITHUANIA_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            lithuania_oam_max_pages=max(
                1,
                _env_int("LITHUANIA_OAM_MAX_PAGES", 10),
            ),
            slovakia_nbs_ceri_base_url=os.getenv(
                "SLOVAKIA_NBS_CERI_BASE_URL",
                "https://ceri.nbs.sk",
            ),
            slovakia_nbs_ceri_lookback_days=max(
                1,
                _env_int("SLOVAKIA_NBS_CERI_LOOKBACK_DAYS", 120),
            ),
            slovakia_nbs_ceri_rate_limit_seconds=max(
                0.0,
                _env_float("SLOVAKIA_NBS_CERI_RATE_LIMIT_SECONDS", 0.5),
            ),
            slovakia_nbs_ceri_verify_ssl=os.getenv(
                "SLOVAKIA_NBS_CERI_VERIFY_SSL", "true"
            ).lower() != "false",
            romania_asf_oam_base_url=os.getenv(
                "ROMANIA_ASF_OAM_BASE_URL",
                "https://oam.asfromania.ro",
            ).rstrip("/"),
            romania_asf_oam_lookback_days=max(
                1,
                _env_int("ROMANIA_ASF_OAM_LOOKBACK_DAYS", 365),
            ),
            romania_asf_oam_rate_limit_seconds=max(
                0.0,
                _env_float("ROMANIA_ASF_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            romania_asf_oam_verify_ssl=os.getenv(
                "ROMANIA_ASF_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            romania_asf_oam_max_pages=max(
                1,
                _env_int("ROMANIA_ASF_OAM_MAX_PAGES", 100),
            ),
            bulgaria_bse_x3news_base_url=os.getenv(
                "BULGARIA_BSE_X3NEWS_BASE_URL",
                "https://download.bse-sofia.bg",
            ).rstrip("/"),
            bulgaria_bse_x3news_lookback_days=max(
                1,
                _env_int("BULGARIA_BSE_X3NEWS_LOOKBACK_DAYS", 365),
            ),
            bulgaria_bse_x3news_rate_limit_seconds=max(
                0.0,
                _env_float("BULGARIA_BSE_X3NEWS_RATE_LIMIT_SECONDS", 0.5),
            ),
            bulgaria_bse_x3news_verify_ssl=os.getenv(
                "BULGARIA_BSE_X3NEWS_VERIFY_SSL", "true"
            ).lower() != "false",
            bulgaria_bse_x3news_max_active_buckets=max(
                1,
                _env_int("BULGARIA_BSE_X3NEWS_MAX_ACTIVE_BUCKETS", 3),
            ),
            bulgaria_bse_x3news_max_issuer_scans=max(
                1,
                _env_int("BULGARIA_BSE_X3NEWS_MAX_ISSUER_SCANS", 30),
            ),
            bulgaria_bse_x3news_max_candidates_per_source=max(
                1,
                _env_int("BULGARIA_BSE_X3NEWS_MAX_CANDIDATES_PER_SOURCE", 40),
            ),
            malta_mse_oam_base_url=os.getenv(
                "MALTA_MSE_OAM_BASE_URL",
                "https://www.borzamalta.com.mt",
            ).rstrip("/"),
            malta_mse_oam_lookback_days=max(
                1,
                _env_int("MALTA_MSE_OAM_LOOKBACK_DAYS", 365),
            ),
            malta_mse_oam_rate_limit_seconds=max(
                0.0,
                _env_float("MALTA_MSE_OAM_RATE_LIMIT_SECONDS", 0.5),
            ),
            malta_mse_oam_verify_ssl=os.getenv(
                "MALTA_MSE_OAM_VERIFY_SSL", "true"
            ).lower() != "false",
            web_host=os.getenv("INFOFIN_WEB_HOST", "127.0.0.1"),
            web_port=_env_int("INFOFIN_WEB_PORT", 8765),
            web_workers=_env_int("INFOFIN_WEB_WORKERS", 2),
            web_max_period_days=_env_int("INFOFIN_WEB_MAX_PERIOD_DAYS", 370),
            web_max_candidates=_env_int("INFOFIN_WEB_MAX_CANDIDATES", 100000),
        )
