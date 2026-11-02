"""Derived dividend-flow estimates along ownership chains (#2891), over the synthetic fixture chain only."""

from __future__ import annotations

import copy
import json
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import pytest

from src.domains.market.dividend_flows import (
    ASSUMPTIONS,
    DIVIDEND_SOURCES,
    METHOD,
    METHOD_ID,
    DividendFlowError,
    _statement_value,
    _validate,
    export_bundle,
    guard,
    reproduce,
)
from src.evidence_bundle import verify_bundle
from src.kb.ownership_records import record, record_v2
from tests.unit import dividend_flow_harness as h

ROOT = Path(__file__).resolve().parents[3]
CENT = Decimal("0.01")


def _one(result, holding_date, recipient):
    found = [e for e in result["estimates"]
             if e["dividend"]["holding_date"] == holding_date and e["recipient"] == recipient]
    assert len(found) == 1, [(e["dividend"]["holding_date"], e["path"]) for e in result["estimates"]]
    return found[0]


def _width(item):
    return Decimal(item["estimate"]["interval"]["high"]) - Decimal(item["estimate"]["interval"]["low"])


@pytest.fixture(scope="module")
def chain():
    conn, acquired = h.build()
    return conn, acquired, h.query(conn, acquired, issuer=h.A, max_depth=3)


def test_three_level_chain_reproduces_from_its_cited_inputs(chain):
    conn, _, result = chain
    top = _one(result, "2026-03-11", h.D)
    assert top["path"] == [h.A, h.B, h.C, h.D] and top["depth"] == 3 and top["kind"] == "look-through"
    assert top["label"] == "derived" and top["reported"] is False and top["method"] == METHOD
    assert top["estimate"]["currency"] == "USD" and top["estimate"]["point"] is None  # a band is never a point
    assert top["estimate"]["interval"] == {"low": "862500.00", "high": "2025000.00"}
    assert top["estimate"]["interval_covers"]["holding_bands"] is True
    assert top["estimate"]["interval_covers"]["timing"] == "bounded"
    assert sorted(c["role"] for c in top["inputs"]) == ["dividend", "holding", "holding", "holding"]
    assert top["n"] == 4 and "look-through" in top["assumptions"]

    # By hand from the cited inputs only: dividend x 13D shares (+/- 1 pp) x PSC band x PSC band.
    cited = {c["role"] + str(i): c for i, c in enumerate(top["inputs"])}
    dividend = next(c for c in cited.values() if c["role"] == "dividend")
    row = conn.execute("SELECT payload_json FROM market_corporate_action_revisions WHERE namespace=? AND "
                       "revision_id=?", [h.MARKET_NS, dividend["revision_id"]]).fetchone()
    per_share = Decimal(json.loads(row[0])["cash_amount"])
    from src.kb.ownership_store import OwnershipStore

    store = OwnershipStore(conn, initialize=False)
    bodies = [store.get(h.NS, c["record_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES, revision=c["revision"])
              ["record"] for c in top["inputs"] if c["role"] == "holding"]
    thirteen_d = next(b for b in bodies if b["source"]["provider"] == "sec-edgar")
    shares, pct = Decimal(thirteen_d["share"]["shares"]), Decimal(thirteen_d["share"]["exact"])
    bands = {b["subject_key"]: b["share"]["band"] for b in bodies if "band" in (b.get("share") or {})}
    low = per_share * shares * (pct - 1) / pct * Decimal(bands[h.B]["min"]) / 100 * Decimal(bands[h.C]["min"]) / 100
    high = per_share * shares * (pct + 1) / pct * Decimal(bands[h.B]["max"]) / 100 * Decimal(bands[h.C]["max"]) / 100
    assert top["estimate"]["interval"] == {"low": str(low.quantize(CENT, ROUND_FLOOR)),
                                           "high": str(high.quantize(CENT, ROUND_CEILING))}

    checked = reproduce(conn, result, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert checked["ok"] is True and len(checked["estimates"]) == len(result["estimates"])
    assert {c["estimate_id"] for c in checked["estimates"]} == {e["estimate_id"] for e in result["estimates"]}


def test_interval_widens_when_an_input_is_a_band():
    conn, acquired = h.build(c_in_b=h.exact_bods_c_in_b("60"))
    exact = h.query(conn, acquired, issuer=h.A, max_depth=3)
    conn_band, acquired_band = h.build()
    banded = h.query(conn_band, acquired_band, issuer=h.A, max_depth=3)
    exact_c, band_c = _one(exact, "2026-03-11", h.C), _one(banded, "2026-03-11", h.C)
    # Exact 60 % of B: 0.50 x [4.6M, 5.4M] x 0.6, with the stated point 0.50 x 5M x 0.6.
    assert exact_c["estimate"]["interval"] == {"low": "1380000.00", "high": "1620000.00"}
    assert exact_c["estimate"]["point"] == "1500000.00"
    assert exact_c["estimate"]["interval_covers"]["holding_bands"] is False
    assert band_c["estimate"]["interval"] == {"low": "1150000.00", "high": "2025000.00"}
    assert band_c["estimate"]["point"] is None and band_c["estimate"]["interval_covers"]["holding_bands"] is True
    assert _width(band_c) > _width(exact_c)
    assert Decimal(band_c["estimate"]["interval"]["low"]) <= Decimal(exact_c["estimate"]["interval"]["low"])
    assert Decimal(band_c["estimate"]["interval"]["high"]) >= Decimal(exact_c["estimate"]["interval"]["high"])
    assert reproduce(conn, exact, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["ok"] is True


def test_timing_rules_by_disclosure_regime(chain):
    _, _, result = chain
    direct_b = _one(result, "2026-03-11", h.B)
    statement = direct_b["links"][0]["statements"][0]
    assert statement["regime"] == "sec-13d" and statement["timing"]["treatment"] == "material-change-1pp"
    assert statement["timing"]["mismatch_days"] == 19
    assert statement["pct"] == {"low": "11.5", "high": "13.5", "point": "12.5"}
    assert direct_b["estimate"]["interval"] == {"low": "2300000.00", "high": "2700000.00"}
    assert direct_b["estimate"]["point"] == "2500000.00"
    # 13F: the 2025-12-31 position bracketed by the 2026-03-31 report, both cited.
    manager = _one(result, "2026-03-11", h.E)
    assert manager["estimate"]["interval"] == {"low": "400000.00", "high": "450000.00"}
    assert [c["role"] for c in manager["inputs"]].count("next-report") == 1
    assert manager["links"][0]["statements"][0]["timing"]["treatment"] == "bracketed-by-next-report"
    # No later 13F on record for the June dividend: the interval says it does not bound timing.
    june = _one(result, "2026-06-10", h.E)
    assert june["estimate"]["interval_covers"]["timing"] == "not-bounded" and "timing-not-bounded" in june["flags"]
    # A 13G percentage with no share count needs the cited shares-outstanding fact; stale after 92 days.
    trust = _one(result, "2026-03-11", h.F)
    assert "shares-outstanding" in [c["role"] for c in trust["inputs"]]
    assert trust["estimate"]["point"] == "1200000.00" and "shares-outstanding" in trust["assumptions"]
    stale = [n for n in result["not_estimated"] if n["recipient"] == h.F]
    assert [(n["dividend"]["holding_date"], n["reason"]) for n in stale] == [("2026-06-10", "stale-holding")]
    # The June PSC bands: the register's notification window is still open at the as-of date.
    assert "notification-window-open" in _one(result, "2026-06-10", h.D)["flags"]


def test_regime_rules_are_pure_functions():
    dividend = {"holding_date": "2026-05-20", "period_start": None}
    bafin = record("ownership_assertion", "bafin-vr:x:holder:s33", {"provider": "bafin", "provider_record_id": "x"},
                   subject_key="bafin-issuer:X", holder={"name": "H", "kind": "entity"}, assertion_kind="voting_rights",
                   share={"exact": "10"}, validity={"from": "2026-01-15", "from_status": "stated",
                                                    "to_status": "unknown"})
    params = {"max_unbounded_gap_days": 92}
    from datetime import date

    value = _statement_value([bafin], [], dividend, date(2026, 6, 30), params)
    # Exactly at a threshold: the bracket is [10, 15) and its lower bound is not the publication threshold.
    assert (value["pct"]["low"], value["pct"]["high"]) == (Decimal(10), Decimal(15))
    assert value["timing"]["treatment"] == "threshold-bracket"
    early = _statement_value([bafin], [], dividend, date(2026, 5, 25), params)
    assert "notification-window-open" in early["flags"]
    low = {**bafin, "share": {"exact": "3.4"}}
    assert _statement_value([low], [], dividend, date(2026, 6, 30), params)["censoring"]["lower_bound_at_threshold"]
    below = {**bafin, "share": {"exact": "2.9"}}
    assert _statement_value([below], [], dividend, date(2026, 6, 30), params)["reason"] == "below-publication-threshold"
    instruments = {**bafin, "record_key": "bafin-vr:x:holder:s38"}
    assert _statement_value([instruments], [], dividend, date(2026, 6, 30), params)["reason"] == \
        "instrument-basis-not-dividend-bearing"
    option = record_v2("ownership_assertion", "sec-edgar:13f:x", {"provider": "sec-edgar", "provider_record_id": "x"},
                       subject_key="s", holder={"key": "k", "name": "M", "kind": "entity"},
                       assertion_kind="reported_holding", validity={"from": "2026-03-31", "from_status": "stated"},
                       filing={"accession_number": "0000000001-26-000001", "form_type": "13F-HR",
                               "period_of_report": "2026-03-31",
                               "amendment": {"is_amendment": False, "chain_key": "13f:manager-cik:1:period:2026-03-31"}},
                       position={"cusip": "30099A101", "amount": "100", "amount_type": "SH", "put_call": "Call"})
    assert _statement_value([option], [], dividend, date(2026, 6, 30), params)["reason"] == "option-position-not-shares"


def test_holder_mode_keeps_currencies_apart_and_lists_what_is_not_estimated():
    conn, acquired = h.build()
    result = h.query(conn, acquired, holder=h.D, max_depth=3)
    assert result["mode"] == "holder" and result["entity"]["entity"] == h.D
    assert set(result["by_currency"]) == {"USD", "EUR"}
    assert not {"total", "totals", "sum"} & set(result)
    eur = [e for e in result["estimates"] if e["estimate"]["currency"] == "EUR"]
    assert len(eur) == 1 and eur[0]["path"] == [h.K, h.B, h.C, h.D]
    # WpHG s. 33 4.2 % of 10,000,000 shares: the [3, 5) bracket, EUR 1.20, then both PSC bands.
    assert eur[0]["estimate"]["interval"] == {"low": "135000.00", "high": "450000.00"}
    direct = eur[0]["links"][0]
    assert direct["statements"][0]["timing"]["treatment"] == "threshold-bracket"
    assert [s["reason"] for s in direct["statements_not_used"]] == ["instrument-basis-not-dividend-bearing"]
    assert "shares-outstanding" in [c["role"] for c in eur[0]["inputs"]]
    assert {u["holder"] for u in result["unknown_links"]} == {h.PARENT_OF_D}
    assert all(u["reason"] == "no-share-stated" for u in result["unknown_links"])
    assert result["withheld"] and all("A. Fixture-Person" not in json.dumps(w) for w in result["withheld"])
    assert "A. Fixture-Person" not in json.dumps(result["estimates"])
    assert {u["entity"] for u in result["unlinked_entities"]} == {h.B, h.C}
    assert [x["reason"] for x in result["excluded_dividends"]] == ["status-announced"]
    assert DIVIDEND_SOURCES["bundesanzeiger"]["status"] == "not-implemented"
    assert result["sources"]["bundesanzeiger"]["reason"]


def test_below_threshold_holdings_are_never_estimated(chain):
    _, _, result = chain
    assert not [e for e in result["estimates"] if e["recipient"] == h.H]
    exits = [n for n in result["not_estimated"] if n["recipient"] == h.H]
    assert exits and {n["reason"] for n in exits} == {"below-publication-threshold"}
    assert all("estimate" not in n for n in exits)


def test_max_depth_truncates_and_cycles_are_reported():
    conn, acquired = h.build()
    shallow = h.query(conn, acquired, issuer=h.A, max_depth=2)
    assert shallow["truncated"] is True and max(e["depth"] for e in shallow["estimates"]) == 2
    assert not [e for e in shallow["estimates"] if e["recipient"] == h.D]
    with pytest.raises(DividendFlowError) as refused:
        h.query(conn, acquired, issuer=h.A, max_depth=11)
    assert refused.value.code == "invalid_request"
    cross = h.psc("companies-house:psc:09990402:FIXB", h.C, h.B, "Northwind Holdings Ltd", h.BAND_25_50)
    conn, acquired = h.build(extra=[cross])
    looped = h.query(conn, acquired, issuer=h.A, max_depth=5)
    assert [h.A, h.B, h.C, h.B] in [c["path"] for c in looped["cycles"]]
    assert all(len(set(e["path"])) == len(e["path"]) for e in looped["estimates"])


def test_estimates_are_never_presented_as_reported_figures(chain):
    conn, _, result = chain
    assert result["label"] == "derived" and result["reported"] is False and "not a reported figure" in result["notice"]
    from src.analytics.honesty import validate_analytic_output

    assert validate_analytic_output(result) == []  # the repo's honesty envelope: n, method, assumptions
    for item in result["estimates"]:
        assert item["label"] == "derived" and item["reported"] is False and item["method"] == METHOD
        assert item["method_id"] == METHOD_ID and item["method_version"] == "1.0.0"
        assert validate_analytic_output(item) == []
        assert set(item["estimate"]) == {"currency", "point", "interval", "interval_covers"}
        assert item["estimate"]["interval_covers"]["interval_kind"] == "bounds"
        assert item["n"] == len(item["inputs"]) >= 2 and set(item["assumptions"]) <= set(ASSUMPTIONS)
        assert "not a reported figure" in item["statement"]
    for row in result["dividends"] + result["reported_context"]:
        assert row["label"] == "reported"  # the declared inputs, kept apart from the derived estimates
    for tamper in ({"label": "reported"}, {"reported": True}, {"method": "other@1"}, {"method_id": "other"},
                   {"estimate": {**result["estimates"][0]["estimate"], "amount": "1"}}):
        forged = copy.deepcopy(result)
        forged["estimates"][0].update(tamper)
        with pytest.raises(DividendFlowError) as refused:
            guard(forged)
        assert refused.value.code == "not_derived"
        with pytest.raises(DividendFlowError):
            export_bundle(conn, forged)
    forged = copy.deepcopy(result)
    forged["estimates"][0]["reported"] = True
    with pytest.raises(DividendFlowError) as invalid:
        _validate(forged)
    assert invalid.value.code == "contract_invalid"


def test_evidence_bundle_and_tamper_detection(chain):
    conn, _, result = chain
    bundle = export_bundle(conn, result)
    assert bundle["contract"] == "noesis-evidence-bundle-v1" and verify_bundle(bundle).valid
    cited = {c["revision_id"] for e in result["estimates"] for c in e["inputs"]}
    cited |= {c["revision_id"] for d in result["dividends"] for c in d["inputs"]}
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    assert {o["payload"]["revision_id"] for o in evidence} == cited
    root = next(o for o in bundle["objects"] if o["id"] in bundle["roots"])
    assert root["payload"]["result_hash"] == result["result_hash"] and root["payload"]["label"] == "derived"
    edited = copy.deepcopy(result)
    edited["estimates"][0]["estimate"]["interval"]["high"] = "1.00"
    assert reproduce(conn, edited, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["ok"] is False
    moved = copy.deepcopy(result)
    moved["estimates"][0]["inputs"][0]["record_hash"] = "0" * 64
    with pytest.raises(DividendFlowError) as changed:
        reproduce(conn, moved, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert changed.value.code == "input_changed"


def test_filed_per_share_facts_are_used_without_double_counting_periods():
    from src.domains.market.financial_facts import MarketFinancialFactStore
    from src.domains.market.instruments import MarketInstrumentStore

    issuer_key = "sec-edgar:cik:0009999311"
    holder = record_v2(
        "ownership_assertion", "sec-edgar:13dg:0009999411-25-000001:reporting-person:0:stake",
        {"provider": "sec-edgar", "provider_record_id": "0009999411-25-000001"}, subject_key=issuer_key,
        assertion_kind="shareholding", holder={"key": h.B, "name": "Northwind Holdings Ltd", "kind": "entity"},
        share={"exact": "8.0", "shares": "800000"},
        validity={"from": "2025-12-15", "from_status": "stated", "to_status": "unknown"}, statement_date="2025-12-15",
        filing={"accession_number": "0009999411-25-000001", "form_type": "SCHEDULE 13D", "filing_date": "2025-12-17",
                "event_date": "2025-12-15", "amendment": {"is_amendment": False, "chain_key": "13dg:facts-only"}})
    issuer = record("legal_entity", issuer_key, {"provider": "sec-edgar", "provider_record_id": issuer_key},
                    name="Exampla Facts-Only Corp", identifiers=[{"scheme": "sec-cik", "value": "0009999311"}])
    conn, acquired = h.build(extra=[issuer, holder], side=False)
    clock = {"now": h.ms("2026-03-01T00:00:00")}
    ref = h.source_ref("facts-only", clock["now"])
    MarketInstrumentStore(conn, now=lambda: clock["now"]).put_issuer(
        h.MARKET_NS, issuer_id="issuer:facts-only", kg_entity_id="kg:issuer:facts-only",
        display_name="Exampla Facts-Only Corp", source_refs=[ref], principal_id="operator", scopes={"operator"},
        identifiers=[{"scheme": "cik", "value": "0009999311", "valid_from_ms": 0, "valid_to_ms": None,
                      "source_ref_id": ref["source_ref_id"]}])
    facts = MarketFinancialFactStore(conn, now=lambda: clock["now"])
    for concept_id, start, end, value, period_class in (("fy", "2025-01-01", "2025-12-31", "2.00", "annual"),
                                                        ("q4", "2025-10-01", "2025-12-31", "0.50", "quarter")):
        payload = h._fact("issuer:facts-only", "us-gaap", "CommonStockDividendsPerShareDeclared", value, end,
                          "2026-02-27T21:00:00", "0009999311-26-000001")
        payload.update({"fact_observation_id": f"fact:facts-only:dps:{concept_id}", "unit": "USD/shares",
                        "context_id": f"context:{concept_id}", "period_class": period_class,
                        "period": {"kind": "duration", "start_date": start, "end_date": end}})
        facts.put_fact(h.MARKET_NS, payload, principal_id="operator", scopes={"operator"})
    result = h.query(conn, acquired, issuer=issuer_key, max_depth=1)
    assert [d["per_share"] for d in result["dividends"]] == ["2"]
    assert [x["reason"] for x in result["excluded_dividends"]] == ["overlapping-period-not-used"]
    item = _one(result, "2025-12-31", h.B)
    assert item["dividend"]["source"] == "market-financial-fact" and item["dividend"]["period_start"] == "2025-01-01"
    timing = item["links"][0]["statements"][0]["timing"]
    assert timing["mismatch_days"] == 348 and timing["treatment"] == "material-change-1pp"
    assert item["estimate"]["interval"] == {"low": "1400000.00", "high": "1800000.00"}
    assert reproduce(conn, result, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["ok"] is True


def test_market_inputs_missing_are_partial_not_zero():
    import duckdb

    from src.domains.market.dividend_flows import estimate_dividend_flows
    from src.kb.ownership_store import OwnershipStore

    conn = duckdb.connect()
    OwnershipStore(conn, now=lambda: 1).apply(h.NS, h.entities() + h.chain_records(), run_id="r",
                                             observed_at_ms=1, principal_id=h.PRINCIPAL)
    result = estimate_dividend_flows(conn, h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, issuer=h.A,
                                     principal_id=h.PRINCIPAL, scopes=h.SCOPES, acquired_by_ms=h.ms("2026-07-01T00:00:00"))
    assert result["status"] == "partial" and result["estimates"] == []
    assert {d["code"] for d in result["degraded"]} == {"store_unavailable"}
    assert result["unlinked_entities"] == [{"entity": h.A, "reason": "no-market-issuer-link"}]


def test_invalid_requests_are_refused(chain):
    conn, acquired, _ = chain
    for kwargs in ({"issuer": h.A, "holder": h.D}, {}, {"issuer": h.A, "as_of": "2026-06"},
                   {"issuer": h.A, "window_days": 0}):
        with pytest.raises(DividendFlowError) as refused:
            h.query(conn, acquired, **kwargs)
        assert refused.value.code == "invalid_request"
