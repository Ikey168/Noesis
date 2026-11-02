"""Synthetic three-level ownership chain with declared dividends for the dividend-flow estimates (#2891).

Every name, identifier, filing and figure here is fictional and authored for
the tests; nothing is live data. The chain (worked example in
docs/subsystems/dividend-flow-estimates.md)::

    Exampla Dividend Corp (A, pays USD 0.50 per share, record dates 2026-03-11 and 2026-06-10)
      <- Northwind Holdings Ltd (B): Schedule 13D, 12.5 % = 5,000,000 shares, event date 2026-02-20
        <- Fiktiva Family Holdings Ltd (C): PSC band more than 50 % and less than 75 % of shares (open)
          <- Fiktiva Trust Company Ltd (D): PSC band 75 % to 100 % of shares (open)

Beside the chain: a 13F manager (E) with two report periods, a Schedule 13G
holder stating only a percentage (F, needs shares outstanding), a 13G exit
filing below 5 % (H, censored), a natural-person PSC of C (withheld), a GLEIF
consolidation parent of D without a share (an unknown link) and a German
issuer (K, EUR) in which B holds 4.2 % of voting rights under WpHG s. 33.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import duckdb

from src.kb.ownership_records import record, record_v2
from src.kb.ownership_store import OwnershipStore

NS = "ownership-dividends"
MARKET_NS = "market:dividends"
PRINCIPAL = "analyst"
ENTITLEMENT = "entitlement:dividend-fixture"
AS_OF = "2026-06-30"
A = "sec-edgar:cik:0009999301"
B = "companies-house:gb-coh:09990401"
C = "companies-house:gb-coh:09990402"
D = "companies-house:gb-coh:09990403"
E = "sec-edgar:cik:0009999302"
F = "sec-edgar:cik:0009999303"
H = "sec-edgar:cik:0009999304"
K = "bafin-issuer:DE000FIKT0018"
K_LEI = "529900FIKTIVERTRAG18"
PARENT_OF_D = "gleif:lei:529900FIKTIVFOUNDN07"
SCOPES = {
    "knowledge:ownership:read", "knowledge:ownership:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "market:actions:read", "market:financial-facts:read", "market:instruments:read", f"namespace:{MARKET_NS}:read",
    f"market:entitlement:{ENTITLEMENT}:read", f"market:entitlement:{ENTITLEMENT}:derive",
}


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).replace(tzinfo=UTC).timestamp() * 1000)


def _src(provider: str, record_id: str, **extra) -> dict:
    return {"provider": provider, "provider_record_id": record_id, "source_type": "synthetic fixture",
            "note": "authored fixture for #2891; not live data", **extra}


def _filing(accession: str, form: str, filing_date: str, *, event_date=None, period=None, chain: str) -> dict:
    return {"accession_number": accession, "form_type": form, "filing_date": filing_date, "event_date": event_date,
            "period_of_report": period, "amendment": {"is_amendment": False, "chain_key": chain,
                                                      "chain_basis": "fixture"}}


def entities() -> list[dict]:
    def entity(key, provider, name, jurisdiction, identifiers):
        return record("legal_entity", key, _src(provider, key), name=name, jurisdiction=jurisdiction,
                      identifiers=identifiers)

    return [
        entity(A, "sec-edgar", "Exampla Dividend Corp", "US-DE", [{"scheme": "sec-cik", "value": "0009999301"}]),
        entity(B, "companies-house", "Northwind Holdings Ltd", "GB", [{"scheme": "gb-coh", "value": "09990401"}]),
        entity(C, "companies-house", "Fiktiva Family Holdings Ltd", "GB", [{"scheme": "gb-coh", "value": "09990402"}]),
        entity(D, "companies-house", "Fiktiva Trust Company Ltd", "GB", [{"scheme": "gb-coh", "value": "09990403"}]),
        entity(E, "sec-edgar", "Exampla Capital Management LLC", "US-DE", [{"scheme": "sec-cik", "value": "0009999302"}]),
        entity(F, "sec-edgar", "Exampla Employee Benefit Trust", "US-NY",
               [{"scheme": "sec-cik", "value": "0009999303"}]),
        entity(H, "sec-edgar", "Exampla Exit Partners LP", "US-DE", [{"scheme": "sec-cik", "value": "0009999304"}]),
        entity(K, "bafin", "Fiktiv Ertrag AG", "DE", [{"scheme": "isin", "value": "DE000FIKT0018"},
                                                      {"scheme": "lei", "value": K_LEI}]),
        entity(PARENT_OF_D, "gleif", "Fiktiva Foundation", "LI", [{"scheme": "lei", "value": "529900FIKTIVFOUNDN07"}]),
    ]


def schedule_13d(*, shares: str | None = "5000000") -> dict:
    share = {"exact": "12.5", "class": "Common Stock", "native": "percent of class as reported on the cover page"}
    if shares:
        share["shares"] = shares
    return record_v2(
        "ownership_assertion", "sec-edgar:13dg:0009999401-26-000001:reporting-person:0:stake",
        _src("sec-edgar", "0009999401-26-000001", locator={"xpath": "/edgarSubmission/formData/coverPageHeader"}),
        subject_key=A, assertion_kind="shareholding", holder={"key": B, "name": "Northwind Holdings Ltd", "kind": "entity"},
        share=share, validity={"from": "2026-02-20", "from_status": "stated", "to_status": "unknown"},
        statement_date="2026-02-20", basis="SCHEDULE 13D cover page, as reported by the filer",
        filing=_filing("0009999401-26-000001", "SCHEDULE 13D", "2026-02-24", event_date="2026-02-20",
                       chain="13dg:issuer-cik:0009999301:reporting-person-cik:0009999401"))


def psc(key: str, subject: str, holder_key: str | None, name: str, band: dict, *, kind="entity", owner_scoped=None):
    holder = {"key": holder_key, "name": name, "kind": kind} if holder_key else {"name": name, "kind": kind}
    extra = {"owner_scoped": True} if owner_scoped else {}
    return record("ownership_assertion", key, _src("companies-house", key, locator={"field": "natures_of_control"}),
                  subject_key=subject, assertion_kind="shareholding", holder=holder,
                  share={"band": band, "native": "ownership-of-shares band as registered"},
                  validity={"from": "2020-01-06", "from_status": "stated", "to_status": "open"},
                  statement_date="2026-04-30", basis="PSC register snapshot of 2026-04-30", **extra)


BAND_50_75 = {"min": "50", "max": "75", "min_inclusive": False, "max_inclusive": False}
BAND_75_100 = {"min": "75", "max": "100", "min_inclusive": True, "max_inclusive": True}
BAND_25_50 = {"min": "25", "max": "50", "min_inclusive": False, "max_inclusive": True}


def chain_records(*, c_in_b: dict | None = None) -> list[dict]:
    """The three-level chain. ``c_in_b`` replaces C's PSC band in B (e.g. an exact BODS statement)."""
    c_link = c_in_b or psc("companies-house:psc:09990401:FIXC", B, C, "Fiktiva Family Holdings Ltd", BAND_50_75)
    return [
        schedule_13d(),
        c_link,
        psc("companies-house:psc:09990402:FIXD", C, D, "Fiktiva Trust Company Ltd", BAND_75_100),
    ]


def exact_bods_c_in_b(percent: str = "60") -> dict:
    return record("ownership_assertion", "open-ownership:statement:fixture-c-in-b", _src(
        "open-ownership", "fixture-c-in-b", statement_id="fixture-c-in-b", locator={"json_pointer": "/3"}),
        subject_key=B, assertion_kind="shareholding", holder={"key": C, "name": "Fiktiva Family Holdings Ltd",
                                                              "kind": "entity"},
        share={"exact": percent, "native": {"exact": percent}},
        validity={"from": "2020-01-06", "from_status": "stated", "to_status": "open"}, statement_date="2026-04-30",
        basis="BODS ownership-or-control statement")


def side_records() -> list[dict]:
    def thirteen_f(accession, period, filing_date, amount):
        return record_v2(
            "ownership_assertion", f"sec-edgar:13f:{accession}:row:1", _src("sec-edgar", accession,
                                                                             locator={"xpath": "/informationTable/infoTable[1]"}),
            subject_key=A, assertion_kind="reported_holding",
            holder={"key": E, "name": "Exampla Capital Management LLC", "kind": "entity"},
            validity={"from": period, "from_status": "stated", "to_status": "unknown"}, statement_date=period,
            basis="Form 13F information table row",
            filing=_filing(accession, "13F-HR", filing_date, period=period,
                           chain=f"13f:manager-cik:0009999302:period:{period}"),
            position={"issuer_name": "EXAMPLA DIVIDEND CORP", "title_of_class": "COM", "cusip": "30099A101",
                      "amount": amount, "amount_type": "SH", "value": "1000", "value_unit": "USD",
                      "investment_discretion": "SOLE", "voting_authority": {"sole": amount, "shared": "0", "none": "0"}})

    def thirteen_g(key, holder, name, accession, event, percent, form="SCHEDULE 13G"):
        return record_v2(
            "ownership_assertion", key, _src("sec-edgar", accession, locator={"xpath": "/edgarSubmission"}),
            subject_key=A, assertion_kind="shareholding", holder={"key": holder, "name": name, "kind": "entity"},
            share={"exact": percent, "class": "Common Stock"},
            validity={"from": event, "from_status": "stated", "to_status": "unknown"}, statement_date=event,
            basis=f"{form} cover page", filing=_filing(accession, form, event, event_date=event,
                                                       chain=f"13dg:issuer-cik:0009999301:{holder}"))

    voting = record("ownership_assertion", "bafin-vr:fixture0001:northwind:s33", _src(
        "bafin", "VR-FIXTURE-0001", statement_id="bafin-notice-rev:fixture-0001", revision="1",
        locator={"field": "percentages.s33"}), subject_key=K,
        holder={"key": B, "name": "Northwind Holdings Ltd", "kind": "entity"}, assertion_kind="voting_rights",
        share={"exact": "4.2"}, validity={"from": "2026-01-15", "from_status": "stated", "to_status": "unknown"},
        statement_date="2026-01-21", basis="WpHG s. 33 (shares) as stated by the notifier")
    voting_instruments = record("ownership_assertion", "bafin-vr:fixture0001:northwind:s38", _src(
        "bafin", "VR-FIXTURE-0001", statement_id="bafin-notice-rev:fixture-0001", revision="1",
        locator={"field": "percentages.s38"}), subject_key=K,
        holder={"key": B, "name": "Northwind Holdings Ltd", "kind": "entity"}, assertion_kind="voting_rights",
        share={"exact": "1.1"}, validity={"from": "2026-01-15", "from_status": "stated", "to_status": "unknown"},
        statement_date="2026-01-21", basis="WpHG s. 38 (instruments) as stated by the notifier")
    return [
        thirteen_f("0009999402-26-000001", "2025-12-31", "2026-02-10", "800000"),
        thirteen_f("0009999402-26-000002", "2026-03-31", "2026-05-12", "900000"),
        thirteen_g("sec-edgar:13dg:0009999403-26-000001:reporting-person:0:stake", F,
                   "Exampla Employee Benefit Trust", "0009999403-26-000001", "2026-02-14", "6.0"),
        thirteen_g("sec-edgar:13dg:0009999404-26-000001:reporting-person:0:stake", H, "Exampla Exit Partners LP",
                   "0009999404-26-000001", "2026-03-01", "4.2", form="SCHEDULE 13G/A"),
        psc("companies-house:psc:09990402:FIXPERSON", C, None, "A. Fixture-Person", BAND_25_50, kind="person",
            owner_scoped=True),
        record("ownership_assertion", f"gleif:rr:{D}:direct", _src("gleif", "rr:fixture-d"), subject_key=D,
               holder={"key": PARENT_OF_D, "name": "Fiktiva Foundation", "kind": "entity"},
               assertion_kind="direct_parent", validity={"from": "2019-05-01", "from_status": "stated",
                                                         "to_status": "open"}),
        voting, voting_instruments,
    ]


def source_ref(name: str, at_ms: int) -> dict:
    return {"source_ref_id": f"src:{name}", "provider": "fixture-only", "provider_object_id": name,
            "source_revision_id": f"fixture:{name}:1", "public_at_ms": at_ms, "source_snapshot_id": f"snapshot:{name}",
            "source_url": None, "retrieved_at_ms": at_ms, "content_hash": hashlib.sha256(name.encode()).hexdigest(),
            "license_id": "fixture-only", "entitlement_id": ENTITLEMENT}


def _action(action_id, issuer_id, security_id, amount, currency, ex_date, record_date, public_iso, **extra):
    public = ms(public_iso)
    return {"contract": "noesis-market-corporate-action-v1", "namespace": MARKET_NS, "owner": None,
            "action_id": action_id, "issuer_id": issuer_id, "security_id": security_id, "listing_id": None,
            "related_security_id": None, "related_listing_id": None, "revision_id": "ignored", "revision": 1,
            "provider": "fixture-provider", "provider_record_id": action_id, "action_type": "cash_dividend",
            "status": "confirmed", "announced_at_ms": public, "public_at_ms": public, "effective_date": ex_date,
            "ex_date": ex_date, "record_date": record_date, "payable_date": None, "ratio_numerator": None,
            "ratio_denominator": None, "cash_amount": amount, "cash_amount_basis": "per_share_before_action",
            "distribution_type": "regular", "currency": currency, "prior_revision_id": None,
            "source_refs": [source_ref(action_id, public)], "recorded_at_ms": public, "record_hash": "ignored",
            **extra}


def _fact(issuer_id, taxonomy, concept, value, instant, filed_iso, accession):
    public = ms(filed_iso)
    logical = f"fact:{accession}:{concept}:{instant}"
    return {"contract": "noesis-market-financial-fact-v1", "namespace": MARKET_NS, "owner": None,
            "fact_observation_id": logical, "issuer_id": issuer_id, "revision_id": "ignored", "revision": 1,
            "filing_accession": accession, "filing_form": "10-K", "taxonomy": taxonomy, "concept": concept,
            "canonical_concept": None, "statement": "balance_sheet", "mapping_status": "unmapped",
            "context_id": f"context:{instant}", "context_id_kind": "companyfacts_composite_key", "unit": "shares",
            "period": {"kind": "instant", "instant_date": instant}, "fiscal_year": 2025, "fiscal_period": "FY",
            "period_class": "instant", "value_lexical": value, "scale": 0, "decimals": "INF", "filed_at_ms": public,
            "accepted_at_ms": public, "public_at_ms": public, "retrieved_at_ms": public,
            "source_document_revision_id": f"filing:{accession}", "source_locator": f"companyfacts/{concept}/{instant}",
            "provider": "fixture-provider", "prior_revision_id": None, "source_refs": [source_ref(logical, public)],
            "recorded_at_ms": public, "record_hash": "0" * 64}


def seed_market(conn, *, clock: dict) -> None:
    from src.domains.market.actions import MarketCorporateActionStore
    from src.domains.market.financial_facts import MarketFinancialFactStore
    from src.domains.market.instruments import MarketInstrumentStore
    from tests.unit.domains.market_entitlement_fixtures import (
        register_market_entitlement,
    )

    op = {"principal_id": "operator", "scopes": {"operator"}}
    register_market_entitlement(conn, MARKET_NS, ENTITLEMENT, "fixture-only", "fixture-only", now_ms=0)
    clock["now"] = ms("2025-01-02T00:00:00")
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    ref = source_ref("issuer-master", clock["now"])
    instruments.put_issuer(MARKET_NS, issuer_id="issuer:exampla-dividend", kg_entity_id="kg:issuer:exampla-dividend",
                           display_name="Exampla Dividend Corp", source_refs=[ref], identifiers=[
                               {"scheme": "cik", "value": "0009999301", "valid_from_ms": 0, "valid_to_ms": None,
                                "source_ref_id": ref["source_ref_id"]}], **op)
    instruments.put_issuer(MARKET_NS, issuer_id="issuer:fiktiv-ertrag", kg_entity_id="kg:issuer:fiktiv-ertrag",
                           display_name="Fiktiv Ertrag AG", source_refs=[ref], identifiers=[
                               {"scheme": "lei", "value": K_LEI, "valid_from_ms": 0, "valid_to_ms": None,
                                "source_ref_id": ref["source_ref_id"]}], **op)
    for issuer_id, security_id, currency in (("issuer:exampla-dividend", "security:exampla-dividend-common", "USD"),
                                             ("issuer:fiktiv-ertrag", "security:fiktiv-ertrag-ord", "EUR")):
        instruments.put_security(MARKET_NS, issuer_id=issuer_id, security_id=security_id,
                                 security_type="common_equity", denomination_currency=currency,
                                 source_refs=[source_ref(security_id, clock["now"])], **op)
    actions = MarketCorporateActionStore(conn, now=lambda: clock["now"])
    for payload in (
        _action("action:exampla-div-2026q1", "issuer:exampla-dividend", "security:exampla-dividend-common", "0.50",
                "USD", "2026-03-10", "2026-03-11", "2026-02-25T21:00:00"),
        _action("action:exampla-div-2026q2", "issuer:exampla-dividend", "security:exampla-dividend-common", "0.50",
                "USD", "2026-06-09", "2026-06-10", "2026-05-26T21:00:00"),
        _action("action:exampla-div-2026q3-announced", "issuer:exampla-dividend", "security:exampla-dividend-common",
                "0.55", "USD", "2026-06-29", "2026-06-29", "2026-06-15T21:00:00", status="announced"),
        _action("action:fiktiv-div-2026", "issuer:fiktiv-ertrag", "security:fiktiv-ertrag-ord", "1.20", "EUR",
                "2026-05-19", "2026-05-20", "2026-05-15T18:00:00"),
    ):
        clock["now"] = payload["public_at_ms"]
        actions.put_action(MARKET_NS, payload, **op)
    facts = MarketFinancialFactStore(conn, now=lambda: clock["now"])
    for payload in (
        _fact("issuer:exampla-dividend", "us-gaap", "CommonStockSharesOutstanding", "40000000", "2025-12-31",
              "2026-02-15T21:00:00", "0009999301-26-000002"),
        _fact("issuer:fiktiv-ertrag", "ifrs-full", "NumberOfSharesOutstanding", "10000000", "2025-12-31",
              "2026-03-20T18:00:00", "FIKTIV-ESEF-2025"),
    ):
        clock["now"] = payload["public_at_ms"]
        facts.put_fact(MARKET_NS, payload, **op)
    clock["now"] = ms("2026-07-01T00:00:00")


def build(*, c_in_b: dict | None = None, extra: list[dict] | None = None, side: bool = True,
          path: str | None = None):
    """A seeded connection (ownership + market) and the fixed acquisition cutoff to query with."""
    conn = duckdb.connect(path) if path else duckdb.connect()
    clock = {"now": ms("2026-06-01T00:00:00")}
    store = OwnershipStore(conn, now=lambda: clock["now"])
    from src.kb.ownership_identity import OwnershipIdentityService

    OwnershipIdentityService(conn, now=lambda: clock["now"])
    records = entities() + chain_records(c_in_b=c_in_b) + (side_records() if side else []) + list(extra or [])
    store.apply(NS, records, run_id="fixture:dividend-flows", observed_at_ms=clock["now"], principal_id=PRINCIPAL)
    seed_market(conn, clock=clock)
    return conn, ms("2026-07-01T00:00:00")


def query(conn, acquired_by_ms: int, **kwargs):
    from src.domains.market.dividend_flows import estimate_dividend_flows

    kwargs.setdefault("as_of", AS_OF)
    return estimate_dividend_flows(conn, NS, market_namespace=MARKET_NS, principal_id=PRINCIPAL, scopes=SCOPES,
                                   acquired_by_ms=acquired_by_ms, **kwargs)
