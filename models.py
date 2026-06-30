from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Issuer:
    name: str
    isin: str
    symbol: str
    market: str
    id: int | None = None
    oslo_issuer_id: str | None = None
    newsweb_url: str | None = None
    euronext_company_url: str | None = None
    italy_storage_provider: str | None = None
    italy_emarket_url: str | None = None
    italy_1info_url: str | None = None
    borsa_italiana_company_url: str | None = None
    netherlands_afm_issuer_url: str | None = None
    netherlands_afm_detail_url: str | None = None
    netherlands_home_member_state: str | None = None
    netherlands_afm_record_id: str | None = None
    belgium_fsma_stori_url: str | None = None
    belgium_fsma_detail_url: str | None = None
    belgium_home_member_state: str | None = None
    belgium_fsma_record_id: str | None = None
    portugal_cmvm_sdi_url: str | None = None
    portugal_cmvm_detail_url: str | None = None
    portugal_cmvm_record_id: str | None = None
    portugal_home_member_state: str | None = None
    ireland_euronext_oam_url: str | None = None
    ireland_euronext_direct_url: str | None = None
    ireland_detail_url: str | None = None
    ireland_record_id: str | None = None
    ireland_home_member_state: str | None = None
    spain_cnmv_entity_url: str | None = None
    spain_cnmv_nif: str | None = None
    spain_cnmv_record_id: str | None = None
    spain_bme_company_url: str | None = None
    spain_home_member_state: str | None = None
    spain_pea_country_check: str | None = None
    sweden_fi_issuer_url: str | None = None
    sweden_fi_record_id: str | None = None
    sweden_fi_detail_url: str | None = None
    sweden_home_member_state: str | None = None
    sweden_nasdaq_company_url: str | None = None
    sweden_pea_country_check: str | None = None
    denmark_dfsa_issuer_url: str | None = None
    denmark_dfsa_record_id: str | None = None
    denmark_dfsa_detail_url: str | None = None
    denmark_home_member_state: str | None = None
    denmark_nasdaq_company_url: str | None = None
    denmark_pea_country_check: str | None = None
    finland_oam_company_id: str | None = None
    finland_oam_issuer_url: str | None = None
    finland_oam_detail_url: str | None = None
    finland_home_member_state: str | None = None
    finland_nasdaq_company_url: str | None = None
    finland_pea_country_check: str | None = None
    austria_oekb_oam_id: str | None = None
    austria_oekb_oam_issuer_url: str | None = None
    austria_oekb_oam_detail_url: str | None = None
    austria_home_member_state: str | None = None
    austria_pea_country_check: str | None = None
    investor_relations_url: str | None = None
    reports_url: str | None = None
    pea_geography_status: str | None = None


