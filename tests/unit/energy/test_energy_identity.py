"""EN08 (#2253): zones, balancing areas, countries and plants through reviewable identity."""

from __future__ import annotations

import duckdb
import pytest

from src.kb import energy_records as enr
from src.kb.energy_identity import EnergyIdentity
from src.kb.energy_store import EnergyStore, EnergyStoreError
from tests.unit.energy.harness import NS, SCOPES, acquire_all

REVIEWER = "reviewer"


@pytest.fixture()
def identity():
    conn = duckdb.connect()
    acquire_all(conn, ["energy-entsoe", "energy-eia-capacity", "energy-eia-fuel-type", "energy-ember-generation",
                       "energy-eurostat-balances", "energy-charts-public-power"])
    ident = EnergyIdentity(conn)
    return conn, ident, ident.propose(NS, principal_id="analyst", scopes=SCOPES)


def _match(result, scheme, code, target_prefix=""):
    return next(m for m in result["proposed"] if m["subject"]["scheme"] == scheme and m["subject"]["code"] == code
                and m["target"]["id"].startswith(target_prefix))


def test_proposals_are_candidates_with_method_and_evidence(identity):
    _, ident, result = identity
    assert result["proposed"] and all(m["state"] == "candidate" for m in result["proposed"])
    deu = _match(result, "iso3166-alpha3", "DEU")
    assert deu["method"].startswith("declared code table + geospatial place resolution")
    assert deu["evidence"]["code_table"]["iso2"] == "DE" and deu["evidence"]["resolution_id"].startswith("geocode-resolution:")
    assert ident.connected(NS, "iso3166-alpha3", "DEU", scopes=SCOPES)["same"] == [
        {"subject": {"scheme": "iso3166-alpha3", "code": "DEU"}, "match": None, "basis": "the requested code"}]


def test_eic_zone_to_country_and_balancing_area_matches(identity):
    _, ident, result = identity
    for match in result["proposed"]:
        ident.review(NS, match["match_id"], "accept", "code table checked", principal_id=REVIEWER, scopes=SCOPES)
    zone = _match(result, "eic", "10Y1001A1001A82H")
    assert zone["evidence"]["code_table_entry"]["countries"] == ["DE", "LU"]
    assert zone["evidence"]["unresolved_members"] == ["LU"]
    place = ident.geo.place(NS, zone["target"]["id"], scopes={"knowledge:geospatial:read"})
    assert place["place_type"] == "bidding-zone" and place["source_ids"] == {"eic": "10Y1001A1001A82H"}
    germany = ident.connected(NS, "eurostat-geo", "DE", scopes=SCOPES)
    assert {(s["subject"]["scheme"], s["subject"]["code"]) for s in germany["same"]} == {
        ("eurostat-geo", "DE"), ("iso3166-alpha3", "DEU"), ("energy-charts-country", "de")}
    assert [r["subject"]["code"] for r in germany["related"]] == ["10Y1001A1001A82H"]
    assert "not identical" in germany["related"][0]["relation"]
    ciso = _match(result, "eia-ba", "CISO")
    area = ident.geo.place(NS, ciso["target"]["id"], scopes={"knowledge:geospatial:read"})
    assert area["place_type"] == "balancing-area" and area["source_ids"] == {"eia_ba": "CISO"}
    zone_view = ident.connected(NS, "eic", "10Y1001A1001A82H", scopes=SCOPES)
    assert {r["subject"]["code"] for r in zone_view["related"]} >= {"DEU", "DE", "de"}


def test_plant_matches_are_reviewed_rejected_and_reverted_through_entity_history(identity):
    conn, ident, result = identity
    g1 = _match(result, "eia-generator", "99901:G1")
    g2 = _match(result, "eia-generator", "99901:G2")
    assert g1["target"]["id"] == g2["target"]["id"] == "ent-eia-plant-99901"
    with pytest.raises(EnergyStoreError) as self_review:
        ident.review(NS, g1["match_id"], "accept", "mine", principal_id="analyst", scopes=SCOPES)
    assert self_review.value.code == "self_review"
    accepted = ident.review(NS, g1["match_id"], "accept", "same EIA plant id", principal_id=REVIEWER, scopes=SCOPES)
    assert accepted["state"] == "accepted" and accepted["reviews"][0]["owner_ref"]["owner"] == "entity_history"
    assert conn.execute("SELECT entity_type FROM canonical_entities WHERE canonical_id='ent-eia-plant-99901'").fetchone()
    rejected = ident.review(NS, g2["match_id"], "reject", "battery is a separate facility", principal_id=REVIEWER,
                            scopes=SCOPES)
    assert rejected["state"] == "rejected"
    decisions = conn.execute("SELECT decision_type FROM entity_identity_decisions ORDER BY created_at_ms, decision_id").fetchall()
    assert {d[0] for d in decisions} == {"match", "non-match"}
    plant = ident.connected(NS, "eia-generator", "99901:G1", scopes=SCOPES)
    assert [s["subject"]["code"] for s in plant["same"]] == ["99901:G1"]
    reverted = ident.review(NS, g1["match_id"], "revert", "wrong plant", principal_id=REVIEWER, scopes=SCOPES)
    assert reverted["state"] == "reverted" and reverted["reviews"][-1]["owner_ref"]["undoes"]
    assert [m["state"] for m in ident.matches(NS, scopes=SCOPES, subject_code="99901:G1")] == ["reverted"]
    with pytest.raises(EnergyStoreError):
        ident.review(NS, g2["match_id"], "revert", "not accepted", principal_id=REVIEWER, scopes=SCOPES)


def test_ambiguous_and_unmatched_identifiers_are_reported_not_merged():
    conn = duckdb.connect()
    store = EnergyStore(conn)
    licence = {"id": "ember-cc-by-4.0", "terms_url": "https://ember-energy.org/creative-commons/"}
    for code in ("XKX", "FRA"):
        store.apply(NS, [enr.record(
            "generation", "ember", "ember:fixture", f"fixture:{code}", f"Fixture {code}",
            source_url="https://api.ember-energy.org/", attribution="Ember", licence=licence,
            subject={"kind": "country", "scheme": "iso3166-alpha3", "code": code, "name": None}, unit="TWh",
            resolution="P1M", reference_period={"start": "2026-06"}, values=[],
            release={"key": "k", "basis": "retrieval_time"}, status="unknown", retrieved_at="2026-09-25T06:00:00Z")],
            run_id="r", principal_id="p", scopes=SCOPES)
    ident = EnergyIdentity(conn)
    # A second place named "france" in the namespace makes the country resolution ambiguous.
    ident.geo.register_place(NS, "france", "region", names=[{"value": "france", "language": "und", "kind": "canonical"}],
                             source_ids={"fixture": "other-france"}, parent_ids=[], principal_id="p",
                             scopes={"knowledge:geospatial:write"})
    result = ident.propose(NS, principal_id="analyst", scopes=SCOPES)
    assert [u["code"] for u in result["unmatched"]] == ["XKX"]
    assert [a["code"] for a in result["ambiguous"]] == ["FRA"] and len(result["ambiguous"][0]["candidates"]) == 2
    ambiguous = ident.matches(NS, scopes=SCOPES, subject_code="FRA")
    assert [m["state"] for m in ambiguous] == ["ambiguous"]
    with pytest.raises(EnergyStoreError):
        ident.review(NS, ambiguous[0]["match_id"], "accept", "pick one", principal_id=REVIEWER, scopes=SCOPES)
