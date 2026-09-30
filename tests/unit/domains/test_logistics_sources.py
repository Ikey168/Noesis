"""Logistics source audit, records and acquisition (#2229: SL01 #2527, SL02 #2532, SL03-SL06 #2534-#2540)."""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from jsonschema import Draft7Validator

from src.ingestion.connectors.dataset.eurostat import EurostatConnector
from src.ingestion.logistics_sources import (
    FREIGHT_INDEX_DECISIONS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    LogisticsAdapter,
    LogisticsFormatError,
    check_document,
    coverage_report,
    fixture_transport,
    parse_unctad,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.logistics_ports import LogisticsPorts
from src.kb.logistics_records import LogisticsError
from src.kb.logistics_series import LogisticsStore, operator_import
from tests.unit import logistics_harness as h

SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-logistics-record-v1.json").read_text())


def test_every_provider_and_freight_index_has_a_recorded_contract_and_licence_decision():
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("access", "format", "identifiers", "terms", "licence", "rate_limits", "revision_model",
                    "temporal_semantics"):
            assert contract.get(key), (provider, key)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    in_scope = {k for k, v in FREIGHT_INDEX_DECISIONS.items() if v["decision"] == "in-scope"}
    assert in_scope == {"bls-ppi-deep-sea-freight"}
    for key, decision in FREIGHT_INDEX_DECISIONS.items():
        assert decision["decision"] in {"in-scope", "excluded"} and decision["reason"], key
    report = coverage_report()["freight_indices"]
    excluded = [i for i in report if i["decision"] == "excluded"]
    assert {i["index_id"] for i in excluded} >= {"baltic-dry-index", "drewry-wci", "freightos-fbx", "scfi"}
    assert all(i["status"] == "excluded by licence decision" and i["reason"] for i in excluded)
    doc = (h.ROOT / "docs/roadmaps/economics-logistics-source-audit.md").read_text()
    for needle in ("Access decisions", "Freight-index licence decisions", "Bounded coverage", "_verify_",
                   "Baltic Dry Index", "UN/LOCODE", "UNCTADstat", "mar_mg_aa_pwhd"):
        assert needle in doc


def test_the_separate_source_pack_validates_and_its_pinned_fixtures_replay_offline():
    manifest = h.manifest()
    assert manifest["pack_id"] == "economic-shipping-and-logistics" and manifest["domains"] == ["economic"]
    assert {s["source_id"] for s in manifest["sources"]} == set(h.SOURCES.values())
    raw = json.loads(h.PACK_PATH.read_text())
    report = SourcePackConformance(h.ROOT).offline(raw)
    assert report["valid"] and report["coverage"]["verified"] == 4
    # economic.json is not touched by this feature: the Economics bundle's pin stays where it was.
    economic = json.loads((h.ROOT / "config/source_packs/economic.json").read_text())
    assert not [s for s in economic["sources"] if s.get("connector") == "logistics"]


def test_an_excluded_index_cannot_be_declared_and_undeclared_controls_are_refused():
    source = h.source("bls")
    document = json.loads(json.dumps(source["logistics"]["documents"][0]))
    document["index_id"] = "baltic-dry-index"
    with pytest.raises(LogisticsFormatError) as caught:
        check_document("bls-timeseries-json", document)
    assert caught.value.code == "excluded_by_licence"
    adapter = LogisticsAdapter(source, transport=fixture_transport(h.pages("bls")))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "release", "parameters": {"series": "X"}}, cursor=None)
    assert refused.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as budget:
        LogisticsAdapter(h.source("eurostat"), transport=fixture_transport(h.pages("eurostat"))).fetch_page(
            {"operation": "release", "parameters": {}, "limit": 1}, cursor=None)
    assert budget.value.code == "budget_exhausted"


def test_unlocode_releases_are_versions_with_revisions_and_removed_codes_kept():
    conn = h.connection()
    h.apply(conn, "unlocode", retrieved_at_ms=h.FIRST_RETRIEVAL)
    ports = LogisticsPorts(conn)
    current = {p["unlocode"]: p for p in ports.ports(h.NS)}
    # Port-function entries of the declared countries only: Berlin (no port function) and Le Havre (FR) are skipped.
    assert set(current) == {"DEBRV", "DEEME", "DEHAM", "DEWVN", "NLRTM"}
    hamburg = current["DEHAM"]["record"]
    assert hamburg["function"] == "12345---" and hamburg["status"] == "AI" and hamburg["subdivision"] == "HH"
    assert hamburg["coordinates"] == {"published": "5333N 00958E", "parsed": True, "lat": 53.55, "lon": 9.966667}
    assert current["DEEME"]["record"]["coordinates"] is None and current["DEHAM"]["release_version"] == "2099-1"
    for port in current.values():
        assert not list(Draft7Validator(SCHEMA).iter_errors({"record_type": "port", **port}))
    result = h.apply(conn, "unlocode", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert result[0]["ports"] == {"created": 1, "revised": 2, "unchanged": 2, "removed": 1}
    wilhelmshaven = ports.port_history(h.NS, "DEWVN")
    assert [(r["revision"], r["state"]) for r in wilhelmshaven] == [(1, "active"), (2, "removed")]
    assert wilhelmshaven[-1]["release_version"] == "2099-2" and wilhelmshaven[0]["record"]["name"] == "Wilhelmshaven"
    assert ports.port(h.NS, "DEEME")["state"] == "marked-for-removal"
    assert ports.port(h.NS, "DEHAM")["change_indicator"] == "|" and ports.port(h.NS, "DECUX")["revisions"] == 1
    assert ports.port(h.NS, "DEWVN", as_of_day="2099-07-01")["state"] == "active"
    assert "DEWVN" not in {p["unlocode"] for p in ports.ports(h.NS)}
    assert "DEWVN" in {p["unlocode"] for p in ports.ports(h.NS, include_removed=True)}
    # Re-acquiring the same release adds nothing.
    assert h.apply(conn, "unlocode", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)[0]["status"] == "unchanged"


def test_unctad_series_keep_port_identifiers_beside_published_unlocodes_breaks_and_gaps():
    conn = h.connection()
    h.apply(conn, "unctad", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = LogisticsStore(conn)
    calls = {s["geography"]["code"]: s for s in store.find_series(h.NS, concept="port_calls")}
    assert set(calls) == {"1101", "1102", "1103", "2201"}
    assert calls["1101"]["geography"] == {"kind": "port", "scheme": "unctad-port", "code": "1101", "label": "Hamburg",
                                          "unlocode": "DEHAM"}
    assert "unlocode" not in calls["1102"]["geography"]
    wilhelmshaven = store.values(h.NS, calls["1103"]["current_vintage_id"])
    assert wilhelmshaven[1] == {"period": "2098", "period_published": "2098", "value_text": None, "value": None,
                                "status": "not_published", "flags": {}, "footnotes": ["Not available"]}
    hamburg = store.values(h.NS, calls["1101"]["current_vintage_id"])
    assert [v["value"] for v in hamburg] == ["7800", "7900"] and hamburg[1]["footnotes"] == ["Provisional"]
    fleet = store.find_series(h.NS, concept="merchant_fleet_by_flag", codes=[("m49", "276")])
    assert len(fleet) == 1 and fleet[0]["geography"]["scheme"] == "m49"
    assert [b["period"] for b in store.breaks(h.NS, fleet[0]["series_id"])] == ["2098"]
    lsci = store.find_series(h.NS, concept="port_liner_shipping_connectivity", codes=[("unlocode", "NLRTM")])
    assert [v["period"] for v in store.values(h.NS, lsci[0]["current_vintage_id"])] == ["2098-Q3", "2098-Q4"]
    for series in store.find_series(h.NS):
        assert not list(Draft7Validator(SCHEMA).iter_errors(series)), series["series_id"]


def test_a_7z_bulk_file_is_refused_and_the_extracted_csv_is_an_operator_import():
    source = h.source("unctad")
    document = source["logistics"]["documents"][2]
    with pytest.raises(LogisticsFormatError) as caught:
        parse_unctad(b"7z\xbc\xaf'\x1c" + b"\x00" * 20, document=document)
    assert caught.value.code == "unsupported_archive"
    conn = h.connection()
    csv_bytes = b"Year,Economy,Economy Label,TEU\n2097,276,Germany,15100000\n"
    result = operator_import(conn, h.NS, source, 2, csv_bytes, principal_id="operator-1", scopes=h.SCOPES,
                             now=lambda: h.FIRST_RETRIEVAL)
    assert result["status"] == "applied" and result["evidence_origin"] == "operator"
    store = LogisticsStore(conn)
    (series,) = store.find_series(h.NS)
    release = store.release(h.NS, store.vintage_rows(h.NS, series["series_id"])[0]["release_id"])
    assert release["evidence_origin"] == "operator"
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w") as archive:
        archive.writestr("US_ContPortThroughput.csv", csv_bytes)
    assert parse_unctad(zipped.getvalue(), document=document)["item_count"] == 1


def test_eurostat_maritime_through_the_connector_keeps_reporting_ports_routes_flags_and_confidential_cells():
    url = EurostatConnector().dataset_url("mar_mg_aa_pwhd", {"rep_mar": ["DE001", "DE003"], "unit": "THS_T"})
    assert "geo=" not in url and url.count("rep_mar=") == 2
    conn = h.connection()
    h.apply(conn, "eurostat", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = LogisticsStore(conn)
    ports = {s["geography"]["code"]: s for s in store.find_series(h.NS, geo_kind="port")}
    assert set(ports) == {"DE001", "DE003", "DE999"}
    assert ports["DE001"]["geography"]["scheme"] == "eurostat-port" and "unlocode" not in ports["DE001"]["geography"]
    hamburg = store.values(h.NS, ports["DE001"]["current_vintage_id"])
    assert hamburg[1]["flags"] == {"status": "p", "status_label": "provisional"} and hamburg[1]["value"] == "112000"
    other = store.values(h.NS, ports["DE999"]["current_vintage_id"])
    assert other[1]["status"] == "confidential" and other[1]["value"] is None
    (route,) = store.find_series(h.NS, geo_kind="route")
    assert route["geography"]["code"] == "DE001" and route["partner"] == {"scheme": "eurostat-port", "code": "NL002",
                                                                           "label": "Rotterdam"}
    vintage = store.vintage_rows(h.NS, route["series_id"])[0]
    assert vintage["release_basis"] == "eurostat_dataset_updated" and vintage["release_at"].startswith("2099-05-02")


def test_observations_live_in_the_economics_series_storage_and_revisions_add_vintages():
    conn = h.connection()
    h.apply(conn, "unctad", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = LogisticsStore(conn)
    (hamburg,) = store.find_series(h.NS, concept="port_calls", codes=[("unlocode", "DEHAM")])
    sid = hamburg["series_id"]
    header = conn.execute("SELECT provider, geography, unit FROM dataset_series WHERE series_id=?", [sid]).fetchone()
    assert header == ("logistics:unctadstat", "unctad-port:1101", "number")
    mapped = conn.execute("SELECT provider_code FROM economic_series_map WHERE domain='economics' AND series_id=?",
                          [sid]).fetchone()
    assert mapped == ("unctadstat:US.PortCalls:1101",)
    # An unchanged re-acquisition adds nothing; a later release with a revised value adds a vintage.
    assert {r["status"] for r in h.apply(conn, "unctad", retrieved_at_ms=h.FIRST_RETRIEVAL + 1)} == {"unchanged"}
    h.apply(conn, "unctad", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    vintages = store.vintage_rows(h.NS, sid)
    assert len(vintages) == 2 and vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    rows = conn.execute("SELECT as_of, period, value FROM dataset_observations WHERE series_id=? AND period='2098' "
                        "ORDER BY as_of", [sid]).fetchall()
    assert [r[2] for r in rows] == [7900.0, 7950.0]
    economic = conn.execute("SELECT count(*), max(revision_of) IS NOT NULL FROM economic_vintages WHERE "
                            "domain='economics' AND series_id=?", [sid]).fetchone()
    assert economic == (2, True)
    # Series that did not change in the later release keep one vintage.
    (rotterdam,) = store.find_series(h.NS, concept="port_calls", codes=[("unlocode", "NLRTM")])
    assert rotterdam["vintage_count"] == 1


def test_freight_index_observations_carry_the_licence_decision_and_a_conflicting_republication_is_refused():
    conn = h.connection()
    h.apply(conn, "bls", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = LogisticsStore(conn)
    (index,) = store.find_series(h.NS, provider="bls-ppi")
    assert index["freight_index"]["decision"] == "in-scope" and index["freight_index"]["base"].startswith("December")
    values = store.values(h.NS, index["current_vintage_id"])
    assert all(v["licence"]["id"] == "us-public-domain" and v["licence"]["attribution"] for v in values)
    assert [v["flags"]["preliminary"] for v in values] == [False, True, True]
    records = h.fetch("bls")[0]
    tampered = json.loads(json.dumps(records))
    tampered[0]["logistics_release"]["content_sha256"] = "0" * 64
    with pytest.raises(LogisticsError) as caught:
        store.apply_release(h.NS, tampered[0]["logistics_release"], [tampered[0]["logistics_item"]])
    assert caught.value.code == "vintage_conflict"
