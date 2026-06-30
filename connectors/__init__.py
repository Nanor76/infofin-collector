from __future__ import annotations

import logging

import requests

from config import Settings
from connectors.base import Connector
from connectors.france_info_financiere import FranceInfoFinanciereConnector
from connectors.oslo_newsweb import OsloNewsWebConnector

LOGGER = logging.getLogger(__name__)

SUPPORTED_WATCH_MARKETS = (
    "Euronext Paris",
    "Oslo Børs",
    "Euronext Milan",
    "Euronext Star Milan",
    "Euronext Growth Milan",
    "Euronext MIV Milan",
    "Euronext Amsterdam",
    "Euronext Brussels",
    "Euronext Growth Brussels",
    "Euronext Lisbon",
    "Euronext Dublin",
    "Bolsa de Madrid",
    "Bolsa de Barcelona",
    "Bolsa de Bilbao",
    "Bolsa de Valencia",
    "BME Growth",
    "BME Scaleup",
    "Nasdaq Stockholm",
    "Nordic Growth Market",
    "Nasdaq Copenhagen",
    "Nasdaq Helsinki",
    "Vienna Stock Exchange",
    "Warsaw Stock Exchange",
    "Prague Stock Exchange",
    "Zagreb Stock Exchange",
    "Ljubljana Stock Exchange",
    "Tallinn Stock Exchange",
    "Riga Stock Exchange",
    "Vilnius Stock Exchange",
    "Bratislava Stock Exchange",
    "Bucharest Stock Exchange",
    "Bulgarian Stock Exchange",
    "Malta Stock Exchange",
)


def is_supported_market(market: str) -> bool:
    key = market.casefold()
    return any(candidate.casefold() == key for candidate in SUPPORTED_WATCH_MARKETS)


def connector_for_market(
    market: str,
    *,
    settings: Settings,
    session: requests.Session,
) -> Connector | None:
    key = market.casefold()
    if key == "euronext paris":
        return FranceInfoFinanciereConnector(
            session=session,
            base_url=settings.amf_base_url,
            fallback_base_urls=settings.amf_fallback_base_urls,
            dataset=settings.amf_dataset,
            rows=settings.amf_rows,
            timeout=settings.http_timeout_seconds,
        )
    if key == "oslo børs":
        return OsloNewsWebConnector(
            session=session,
            euronext_news_url=settings.oslo_euronext_news_url,
            newsweb_base_url=settings.oslo_newsweb_base_url,
            rate_limit_seconds=settings.oslo_rate_limit_seconds,
            lookback_days=settings.oslo_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.http_verify_ssl,
        )
    if key in (
        "euronext milan",
        "euronext star milan",
        "euronext growth milan",
        "euronext miv milan",
    ):
        from connectors.italy_emarketstorage import ItalyEmarketStorageConnector
        return ItalyEmarketStorageConnector(
            session=session,
            home_url=settings.italy_home_url,
            press_releases_url=settings.italy_press_releases_url,
            documents_url=settings.italy_documents_url,
            oneinfo_url=settings.italy_1info_url,
            borsa_company_base_url=settings.italy_borsa_company_base_url,
            market=market,
            rate_limit_seconds=settings.italy_rate_limit_seconds,
            lookback_days=settings.italy_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.italy_verify_ssl,
            max_pages=settings.italy_max_pages,
        )
    if key == "euronext amsterdam":
        from connectors.netherlands_afm import NetherlandsAfmConnector
        return NetherlandsAfmConnector(
            session=session,
            register_url=settings.netherlands_afm_register_url,
            export_type=settings.netherlands_afm_export_type,
            home_member_state_url=(
                settings.netherlands_home_member_state_url
            ),
            home_member_state_export_type=(
                settings.netherlands_home_member_state_export_type
            ),
            rate_limit_seconds=settings.netherlands_rate_limit_seconds,
            lookback_days=settings.netherlands_lookback_days,
            timeout=settings.http_timeout_seconds,
        )
    if key in {"euronext brussels", "euronext growth brussels"}:
        from connectors.belgium_fsma_stori import BelgiumFsmaStoriConnector
        return BelgiumFsmaStoriConnector(
            session=session,
            base_url=settings.belgium_fsma_stori_base_url,
            market=market,
            rate_limit_seconds=settings.belgium_rate_limit_seconds,
            lookback_days=settings.belgium_lookback_days,
            timeout=settings.http_timeout_seconds,
        )
    if key == "euronext lisbon":
        from connectors.portugal_cmvm_sdi import PortugalCmvmSdiConnector
        return PortugalCmvmSdiConnector(
            session=session,
            base_url=settings.portugal_cmvm_base_url,
            sdi_url=settings.portugal_cmvm_sdi_url,
            market=market,
            rate_limit_seconds=settings.portugal_rate_limit_seconds,
            lookback_days=settings.portugal_lookback_days,
            timeout=settings.http_timeout_seconds,
        )
    if key == "euronext dublin":
        from connectors.ireland_euronext_direct import (
            IrelandEuronextDirectConnector,
        )
        return IrelandEuronextDirectConnector(
            session=session,
            base_url=settings.ireland_euronext_direct_base_url,
            dublin_url=settings.ireland_euronext_dublin_url,
            market=market,
            rate_limit_seconds=settings.ireland_rate_limit_seconds,
            lookback_days=settings.ireland_lookback_days,
            timeout=settings.http_timeout_seconds,
        )
    if key in (
        "bolsa de madrid",
        "bolsa de barcelona",
        "bolsa de bilbao",
        "bolsa de valencia",
        "bme growth",
        "bme scaleup",
    ):
        from connectors.spain_cnmv import SpainCnmvConnector
        return SpainCnmvConnector(
            session=session,
            base_url=settings.spain_cnmv_base_url,
            bme_listed_companies_url=settings.spain_bme_listed_companies_url,
            market=market,
            rate_limit_seconds=settings.spain_rate_limit_seconds,
            lookback_days=settings.spain_cnmv_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.spain_verify_ssl,
        )
    if key in (
        "nasdaq stockholm",
        "nordic growth market",
    ):
        from connectors.sweden_fi import SwedenFiConnector
        return SwedenFiConnector(
            session=session,
            base_url=settings.sweden_fi_base_url,
            nasdaq_listed_companies_url=settings.sweden_nasdaq_listed_companies_url,
            market=market,
            rate_limit_seconds=settings.sweden_rate_limit_seconds,
            lookback_days=settings.sweden_fi_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.sweden_verify_ssl,
        )
    if key == "nasdaq copenhagen":
        from connectors.denmark_dfsa_oam import DenmarkDfsaOamConnector
        return DenmarkDfsaOamConnector(
            session=session,
            base_url=settings.denmark_dfsa_base_url,
            nasdaq_listed_companies_url=(
                settings.denmark_nasdaq_listed_companies_url
            ),
            market=market,
            rate_limit_seconds=settings.denmark_rate_limit_seconds,
            lookback_days=settings.denmark_dfsa_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.denmark_verify_ssl,
        )
    if key == "nasdaq helsinki":
        from connectors.finland_oam import FinlandOamConnector
        return FinlandOamConnector(
            session=session,
            base_url=settings.finland_oam_base_url,
            rate_limit_seconds=settings.finland_rate_limit_seconds,
            lookback_days=settings.finland_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.finland_verify_ssl,
        )
    if key == "vienna stock exchange":
        from connectors.austria_oekb_oam import AustriaOekbOamConnector
        return AustriaOekbOamConnector(
            session=session,
            feed_url=settings.austria_oekb_feed_url,
            download_base_url=settings.austria_oekb_download_base_url,
            issuer_list_url=settings.austria_oekb_issuer_list_url,
            rate_limit_seconds=settings.austria_oekb_rate_limit_seconds,
            lookback_days=settings.austria_oekb_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.austria_oekb_verify_ssl,
        )
    if key == "warsaw stock exchange":
        from connectors.poland_knf_oam import PolandKnfOamConnector
        return PolandKnfOamConnector(
            session=session,
            base_url=settings.poland_knf_oam_base_url,
            rate_limit_seconds=settings.poland_knf_oam_rate_limit_seconds,
            lookback_days=settings.poland_knf_oam_lookback_days,
            timeout=max(settings.http_timeout_seconds, 45),
            verify_ssl=settings.poland_knf_oam_verify_ssl,
            max_pages_per_date=(
                settings.poland_knf_oam_max_pages_per_date
            ),
            cache_path=(
                settings.data_dir.parent
                / "cache"
                / "poland_knf_oam.json"
            ),
        )
    if key == "prague stock exchange":
        from connectors.czechia_cnb_curi import CzechiaCnbCuriConnector
        return CzechiaCnbCuriConnector(
            session=session,
            start_url=settings.czechia_cnb_oam_start_url,
            search_url=settings.czechia_cnb_oam_search_url,
            download_base_url=settings.czechia_cnb_oam_download_base_url,
            rate_limit_seconds=settings.czechia_cnb_oam_rate_limit_seconds,
            lookback_days=settings.czechia_cnb_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.czechia_cnb_oam_verify_ssl,
        )
    if key == "zagreb stock exchange":
        from connectors.croatia_hanfa_srpi import CroatiaHanfaSrpiConnector
        return CroatiaHanfaSrpiConnector(
            session=session,
            base_url=settings.croatia_hanfa_srpi_base_url,
            rate_limit_seconds=settings.croatia_hanfa_srpi_rate_limit_seconds,
            lookback_days=settings.croatia_hanfa_srpi_lookback_days,
            timeout=max(settings.http_timeout_seconds, 45),
            verify_ssl=settings.croatia_hanfa_srpi_verify_ssl,
        )
    if key == "ljubljana stock exchange":
        from connectors.slovenia_oam import SloveniaOamConnector
        return SloveniaOamConnector(
            session=session,
            base_url=settings.slovenia_oam_base_url,
            rate_limit_seconds=settings.slovenia_oam_rate_limit_seconds,
            lookback_days=settings.slovenia_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.slovenia_oam_verify_ssl,
            max_pages=settings.slovenia_oam_max_pages,
        )
    if key == "tallinn stock exchange":
        from connectors.estonia_oam import EstoniaOamConnector
        return EstoniaOamConnector(
            session=session,
            base_url=settings.estonia_oam_base_url,
            rate_limit_seconds=settings.estonia_oam_rate_limit_seconds,
            lookback_days=settings.estonia_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.estonia_oam_verify_ssl,
            max_pages=settings.estonia_oam_max_pages,
        )
    if key == "riga stock exchange":
        from connectors.latvia_oam import LatviaOamConnector
        return LatviaOamConnector(
            session=session,
            base_url=settings.latvia_oam_base_url,
            rate_limit_seconds=settings.latvia_oam_rate_limit_seconds,
            lookback_days=settings.latvia_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.latvia_oam_verify_ssl,
            max_pages=settings.latvia_oam_max_pages,
        )
    if key == "vilnius stock exchange":
        from connectors.lithuania_oam import LithuaniaOamConnector
        return LithuaniaOamConnector(
            session=session,
            base_url=settings.lithuania_oam_base_url,
            rate_limit_seconds=settings.lithuania_oam_rate_limit_seconds,
            lookback_days=settings.lithuania_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.lithuania_oam_verify_ssl,
            max_pages=settings.lithuania_oam_max_pages,
        )
    if key == "bratislava stock exchange":
        from connectors.slovakia_nbs_ceri import SlovakiaNbsCeriConnector
        return SlovakiaNbsCeriConnector(
            session=session,
            base_url=settings.slovakia_nbs_ceri_base_url,
            rate_limit_seconds=settings.slovakia_nbs_ceri_rate_limit_seconds,
            lookback_days=settings.slovakia_nbs_ceri_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.slovakia_nbs_ceri_verify_ssl,
        )
    if key == "bucharest stock exchange":
        from connectors.romania_asf_oam import RomaniaAsfOamConnector
        return RomaniaAsfOamConnector(
            session=session,
            base_url=settings.romania_asf_oam_base_url,
            rate_limit_seconds=settings.romania_asf_oam_rate_limit_seconds,
            lookback_days=settings.romania_asf_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.romania_asf_oam_verify_ssl,
            max_pages=settings.romania_asf_oam_max_pages,
        )
    if key == "bulgarian stock exchange":
        from connectors.bulgaria_bse_x3news import BulgariaBseX3NewsConnector
        return BulgariaBseX3NewsConnector(
            session=session,
            base_url=settings.bulgaria_bse_x3news_base_url,
            rate_limit_seconds=settings.bulgaria_bse_x3news_rate_limit_seconds,
            lookback_days=settings.bulgaria_bse_x3news_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.bulgaria_bse_x3news_verify_ssl,
            max_active_buckets=settings.bulgaria_bse_x3news_max_active_buckets,
            max_issuer_scans=settings.bulgaria_bse_x3news_max_issuer_scans,
            max_candidates_per_source=(
                settings.bulgaria_bse_x3news_max_candidates_per_source
            ),
        )
    if key == "malta stock exchange":
        from connectors.malta_mse_oam import MaltaMseOamConnector
        return MaltaMseOamConnector(
            session=session,
            base_url=settings.malta_mse_oam_base_url,
            rate_limit_seconds=settings.malta_mse_oam_rate_limit_seconds,
            lookback_days=settings.malta_mse_oam_lookback_days,
            timeout=settings.http_timeout_seconds,
            verify_ssl=settings.malta_mse_oam_verify_ssl,
        )
    LOGGER.debug("Aucun connecteur pour le marché %s", market)
    return None
