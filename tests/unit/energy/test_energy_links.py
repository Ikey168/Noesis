"""EN09 (#2255): links to climate-environment, market and facility records by shared identifier or citation only."""

from __future__ import annotations

import duckdb
import pytest

from src.ingestion import environment_providers as envp
from src.kb.energy_identity import EnergyIdentity
from src.kb.energy_links import EnergyLinks
from src.kb.energy_store import EnergyStore, EnergyStoreError
from src.kb.environment_store import EnvironmentStore
from tests.unit.energy import fixture_builder as fb
from tests.unit.energy.harness import NS, SCOPES, acquire, acquire_all, register_market_listing

ENV_NS = "environment"
ENV_SCOPES = {"knowledge:environment:read", "knowledge:environment:write", f"namespace:{ENV_NS}:read",
              f"namespace:{ENV_NS}:write"}


def _environment_generation(conn):
    """Climate and Environment's own record of the same ENTSO-E A75 document (its own acquisition, not ours)."""

    selection = {**fb.ENTSOE_SELECTION, "documents": ["generation"]}
    step = envp.plan("entsoe", selection)[0]
    page = next(p for p in fb.load("energy-entsoe")["native_pages"] if p["params"]["documentType"] == "A75")
    records, _ = envp.parse_step(step, page["body"].encode(), {})
    EnvironmentStore(conn).apply(ENV_NS, records, run_id="env-run", principal_id="env", scopes=ENV_SCOPES)
    return records


def test_entsoe_series_link_to_climate_environment_by_shared_document_identifier():
    conn = duckdb.connect()
    links = EnergyLinks(conn)
    assert links.links(NS, scopes=SCOPES)["status"] == "no links on record"
    acquire(conn, "energy-entsoe")
    before = links.link_environment(NS, environment_namespace=ENV_NS, principal_id="analyst", scopes=SCOPES,
                                    environment_scopes=ENV_SCOPES)
    assert before == {"linked": [], "absent": "no climate-environment records on record"}
    _environment_generation(conn)
    result = links.link_environment(NS, environment_namespace=ENV_NS, principal_id="analyst", scopes=SCOPES,
                                    environment_scopes=ENV_SCOPES)
    assert len(result["linked"]) == 2  # one per fuel series of document fixture-a75-7f3c1d revision 1
    listed = links.links(NS, scopes=SCOPES)["links"]
    assert {item["target"]["owner"] for item in listed} == {"climate-environment"}
    assert all(item["citation"]["locator"] == "mRID fixture-a75-7f3c1d revision 1" for item in listed)
    price = next(s for s in EnergyStore(conn).series(NS, scopes=SCOPES, record_type="price"))
    assert links.links(NS, price["series_id"], scopes=SCOPES) == {"links": [], "status": "no links on record"}
    # A later revision is a different document revision: no link until Climate and Environment has it too.
    acquire(conn, "entsoe_revision_2")
    again = links.link_environment(NS, environment_namespace=ENV_NS, principal_id="analyst", scopes=SCOPES,
                                   environment_scopes=ENV_SCOPES)
    assert sorted(again["linked"]) == sorted(result["linked"])


def test_links_need_a_citation_and_are_never_inferred():
    conn = duckdb.connect()
    acquire(conn, "energy-eia-capacity")
    links = EnergyLinks(conn)
    series = EnergyStore(conn).series(NS, scopes=SCOPES)[0]
    with pytest.raises(EnergyStoreError) as missing:
        links.link_cited(NS, series["series_id"], {"owner": "infrastructure", "kind": "facility", "id": "f1"},
                         citation={"source": "", "locator": ""}, principal_id="analyst", scopes=SCOPES)
    assert missing.value.code == "citation_required"
    with pytest.raises(EnergyStoreError) as inferred:
        links.link_cited(NS, series["series_id"], {"owner": "infrastructure", "kind": "facility", "id": "f1"},
                         citation={"source": "gazetteer", "locator": "row 3", "basis": "name similarity"},
                         principal_id="analyst", scopes=SCOPES)
    assert inferred.value.code == "inferred_link_refused"
    cited = links.link_cited(NS, series["series_id"], {"owner": "infrastructure", "kind": "facility", "id": "f1"},
                             citation={"source": "EIA-860 plant file", "locator": "plant 99901, sheet Plant, row 12",
                                       "basis": "shared identifier: EIA plant id"},
                             principal_id="analyst", scopes=SCOPES)
    assert cited["linked"].startswith("energy-link:")


def test_facility_links_require_an_accepted_identity_and_a_rejected_one_blocks_them():
    conn = duckdb.connect()
    acquire_all(conn, ["energy-eia-capacity"])
    identity = EnergyIdentity(conn)
    proposals = identity.propose(NS, principal_id="analyst", scopes=SCOPES)["proposed"]
    g1 = next(m for m in proposals if m["subject"]["code"] == "99901:G1")
    g2 = next(m for m in proposals if m["subject"]["code"] == "99901:G2")
    store = EnergyStore(conn)
    series = {s["subject"]["code"]: s for s in store.series(NS, scopes=SCOPES)}
    facility = {"owner": "critical-infrastructure", "record_id": "facility:fixture-solar-park",
                "entity_id": "ent-eia-plant-99901"}
    citation = {"source": "facility register", "locator": "facility:fixture-solar-park operator entity"}
    links = EnergyLinks(conn)
    unreviewed = links.link_facility(NS, series["99901:G1"]["series_id"], facility, citation=citation,
                                     principal_id="analyst", scopes=SCOPES)
    assert unreviewed["blocked"] and "candidate" in unreviewed["reason"]
    identity.review(NS, g1["match_id"], "accept", "same plant id", principal_id="reviewer", scopes=SCOPES)
    identity.review(NS, g2["match_id"], "reject", "separate battery facility", principal_id="reviewer", scopes=SCOPES)
    linked = links.link_facility(NS, series["99901:G1"]["series_id"], facility, citation=citation,
                                 principal_id="analyst", scopes=SCOPES)
    assert not linked["blocked"] and linked["linked"]
    blocked = links.link_facility(NS, series["99901:G2"]["series_id"], facility, citation=citation,
                                  principal_id="analyst", scopes=SCOPES)
    assert blocked["blocked"] and "rejected" in blocked["reason"]
    identity.review(NS, g1["match_id"], "revert", "wrong plant", principal_id="reviewer", scopes=SCOPES)
    item = links.links(NS, series["99901:G1"]["series_id"], scopes=SCOPES)["links"][0]
    assert item["identity_state"] == "reverted" and item["active"] is False


def test_price_vintages_link_to_the_market_bars_that_cite_them():
    from src.kb.energy_market import publish_prices

    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    listing = register_market_listing(conn)
    price = next(s for s in EnergyStore(conn).series(NS, scopes=SCOPES, record_type="price"))
    vintage = EnergyStore(conn).vintages(NS, price["series_id"], scopes=SCOPES)[-1]
    publish_prices(conn, NS, vintage["vintage_id"], listing_id=listing, entitlement_id="entitlement:entsoe",
                   principal_id="analyst", scopes={"operator"})
    links = EnergyLinks(conn)
    assert len(links.link_market(NS, principal_id="analyst", scopes=SCOPES)["linked"]) == 3
    listed = links.links(NS, price["series_id"], scopes=SCOPES)["links"]
    assert {item["target"]["owner"] for item in listed} == {"market"}
    assert all(vintage["vintage_id"] in item["citation"]["locator"] for item in listed)
