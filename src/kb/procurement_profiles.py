"""Private supplier and buyer profiles for Public Procurement.

This reuses the Funding & Grants applicant-profile machinery
(:class:`src.kb.funding_profiles.FundingProfileStore`) unchanged: one owner
per profile inside one namespace, append-only revisions, idempotent commands,
owner-reviewed versus proposed facts, per-fact effective dates and evidence,
and no operator bypass. Only the fact registry, the tables and the scopes
differ.

A *supplier* profile records capabilities (CPV interests), jurisdictions,
size, turnover, certifications, past contracts and exclusion-ground
self-declarations. A *buyer* profile records organisation, sector and
thresholds. A fact that has not been stated is *unknown* and eligibility
names it; nothing is defaulted to ``False``. Profiles never enter public
source records (:data:`src.kb.procurement_records.PRIVATE_MARKERS`).
"""

from __future__ import annotations

import re

from src.kb.funding_profiles import (
    FundingProfileError,
    FundingProfileStore,
    _choice,
    _countries,
    _country,
    _money,
    _nonnegative_int,
    _strings,
)
from src.kb.procurement_records import READ_SCOPE, WRITE_SCOPE, cpv_code

CONTRACT = "noesis-procurement-profile-v1"
KINDS = ("supplier", "buyer")
SECTIONS = ("supplier", "exclusion", "buyer", "preferences")
SET_ASIDE_STATUSES = ("small-business", "8a", "hubzone", "sdvosb", "wosb", "edwosb", "vosb", "sme")
# Exclusion-ground self-declarations: ``True`` means the ground APPLIES to the
# supplier (e.g. a conviction exists). Unstated grounds stay unknown.
EXCLUSION_GROUNDS = {
    "exclusion.criminal_conviction": "participation in a criminal organisation, corruption, fraud, terrorist offences, money laundering or human trafficking (final conviction)",
    "exclusion.tax_arrears": "breach of obligations to pay taxes",
    "exclusion.social_security_arrears": "breach of obligations to pay social security contributions",
    "exclusion.insolvency": "bankruptcy, insolvency, winding-up or arrangement with creditors",
    "exclusion.professional_misconduct": "grave professional misconduct",
    "exclusion.conflict_of_interest": "conflict of interest",
    "exclusion.distortion_of_competition": "agreements aimed at distorting competition",
    "exclusion.prior_termination": "significant deficiencies leading to early termination of a prior contract",
    "exclusion.misrepresentation": "misrepresentation or withheld information",
    "exclusion.environmental_social_labour": "breach of environmental, social or labour law obligations",
}


def _cpv_list(value):
    return isinstance(value, list) and bool(value) and all(cpv_code(v) for v in value)


def _past_contracts(value):
    return isinstance(value, list) and all(
        isinstance(item, dict) and set(item) <= {"title", "buyer", "year", "value", "cpv", "reference_available"}
        and item.get("title") and (item.get("value") is None or _money(item["value"]))
        and (item.get("cpv") is None or cpv_code(item["cpv"])) for item in value)


def _thresholds(value):
    return isinstance(value, list) and all(
        isinstance(item, dict) and set(item) <= {"label", "amount", "currency", "basis"} and item.get("label")
        and _money({"amount": item.get("amount"), "currency": item.get("currency")}) for item in value)


def _certifications(value):
    return _strings(value) and all(len(v) <= 200 for v in value)


SUPPLIER_FACTS = {
    "supplier.legal_name": ("supplier", lambda v: isinstance(v, str) and bool(v.strip()), "legal name as registered"),
    "supplier.establishment_country": ("supplier", _country, "ISO country of establishment"),
    "supplier.jurisdictions": ("supplier", _countries, "ISO countries where the supplier can perform contracts"),
    "supplier.cpv_interests": ("supplier", _cpv_list, "CPV codes (any hierarchy level) the supplier can deliver"),
    "supplier.naics_codes": ("supplier", lambda v: _strings(v) and all(re.fullmatch(r"\d{2,6}", x) for x in v), "NAICS codes (US)"),
    "supplier.size": ("supplier", _choice("micro", "small", "medium", "large"), "enterprise size class"),
    "supplier.sme": ("supplier", lambda v: isinstance(v, bool), "EU SME status"),
    "supplier.employees": ("supplier", _nonnegative_int, "headcount"),
    "supplier.annual_turnover": ("supplier", _money, "most recent annual turnover (amount + currency)"),
    "supplier.turnover_band": ("supplier", _choice("<1m", "1m-2m", "2m-10m", "10m-50m", ">50m"), "turnover band (EUR equivalent as stated by the owner)"),
    "supplier.insurance_cover": ("supplier", _money, "professional indemnity insurance cover"),
    "supplier.years_trading": ("supplier", _nonnegative_int, "years since the business started trading"),
    "supplier.certifications": ("supplier", _certifications, "held certifications (e.g. 'ISO 9001', 'ISO/IEC 27001')"),
    "supplier.references_count": ("supplier", _nonnegative_int, "comparable references the supplier can name"),
    "supplier.past_contracts": ("supplier", _past_contracts, "owner-stated past contracts (never generated)"),
    "supplier.set_aside_statuses": ("supplier", lambda v: isinstance(v, list) and all(x in SET_ASIDE_STATUSES for x in v), "US set-aside / SME statuses held"),
    "supplier.sam_registered": ("supplier", lambda v: isinstance(v, bool), "active SAM.gov entity registration (UEI)"),
    "supplier.languages": ("supplier", _strings, "languages the supplier can bid in (ISO 639-1)"),
    **{key: ("exclusion", lambda v: isinstance(v, bool), description) for key, description in EXCLUSION_GROUNDS.items()},
}
BUYER_FACTS = {
    "buyer.organisation_name": ("buyer", lambda v: isinstance(v, str) and bool(v.strip()), "contracting authority name"),
    "buyer.country": ("buyer", _country, "ISO country"),
    "buyer.sector": ("buyer", _choice("central-government", "regional-government", "local-government", "health",
                                      "education", "utilities", "defence", "public-undertaking", "other"), "sector"),
    "buyer.authority_type": ("buyer", _choice("central", "sub-central", "utility", "body-governed-by-public-law", "other"), "EU authority type"),
    "buyer.cpv_interests": ("buyer", _cpv_list, "CPV areas the buyer procures"),
    "buyer.thresholds": ("buyer", _thresholds, "owner-stated procurement thresholds"),
    "buyer.identifiers": ("buyer", lambda v: isinstance(v, list) and all(isinstance(i, dict) and i.get("scheme") and i.get("id") for i in v), "identifiers (LEI, VAT, national)"),
}
PREFERENCE_FACTS = {
    "preferences.max_effort": ("preferences", _choice("low", "medium", "high"), "acceptable bid effort"),
    "preferences.timezone": ("preferences", lambda v: isinstance(v, str) and ("/" in v or v == "UTC"), "IANA timezone for notifications"),
    "preferences.min_days_to_deadline": ("preferences", _nonnegative_int, "minimum preparation days"),
    "preferences.min_contract_value": ("preferences", _money, "smallest worthwhile contract value"),
    "preferences.max_contract_value": ("preferences", _money, "largest contract value the supplier can take on"),
    "preferences.watch_buyers": ("preferences", _strings, "buyer names or identifiers to watch"),
}
FACTS = {**SUPPLIER_FACTS, **BUYER_FACTS, **PREFERENCE_FACTS}
_KIND_FACTS = {"supplier": {**SUPPLIER_FACTS, **PREFERENCE_FACTS}, "buyer": {**BUYER_FACTS, **PREFERENCE_FACTS}}


class ProcurementProfileError(FundingProfileError):
    pass


class ProcurementProfileStore(FundingProfileStore):
    CONTRACT = CONTRACT
    FACTS = FACTS
    SECTIONS = SECTIONS
    TABLE = "procurement_profile"
    ID_PREFIX = "procurement-profile:"
    READ_SCOPE = READ_SCOPE
    WRITE_SCOPE = WRITE_SCOPE
    SUBJECT = "procurement"

    def _declared_facts(self, state):
        return _KIND_FACTS[state.get("profile_kind", "supplier")]

    def _check_fact(self, state, key):
        if key.startswith("custom."):
            return
        if key not in _KIND_FACTS[state.get("profile_kind", "supplier")]:
            raise ProcurementProfileError("unknown_fact", f"{key} is not a {state.get('profile_kind')} profile fact")

    def create(self, namespace, request_key, *, label, principal_id, scopes, kind="supplier", extra=None):
        if kind not in KINDS:
            raise ProcurementProfileError("invalid_profile", "profile kind is supplier or buyer")
        return super().create(namespace, request_key, label=label, principal_id=principal_id, scopes=scopes,
                              extra={"profile_kind": kind, **(extra or {})})


def money_facts(facts):
    """Expose money facts per currency (``supplier.annual_turnover.EUR``) for rule comparison.

    A rule stated in EUR therefore sees a GBP turnover as *unknown*, never as a
    comparable number: no currency conversion is ever applied.
    """
    result = dict(facts)
    for key, value in facts.items():
        if isinstance(value, dict) and set(value) == {"amount", "currency"}:
            result[f"{key}.{value['currency']}"] = value["amount"]
    return result
