"""Labour series linked to definitions, methodology documents and demographic series by citation (#2475)."""

from __future__ import annotations

import copy
import json

from src.ingestion.document_store import _SCHEMA as DOCUMENTS_DDL
from src.kb.labour_identity import LabourIdentity
from src.kb.labour_links import LabourLinks
from src.kb.labour_statistics import LabourComparability
from tests.unit import demographics_harness as dh
from tests.unit import labour_harness as h

ESMS = "https://ec.europa.eu/eurostat/cache/metadata/en/lfsa_esms.htm"


def test_references_resolve_by_exact_identifier_and_the_rest_stay_unresolved_citations():
    conn = h.connection()
    h.load_all(conn)
    conn.execute(DOCUMENTS_DDL)
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:esms", "web", ESMS, "Employment and unemployment (LFS) - ESMS metadata"])
    # A document whose title resembles an ILO resolution is never linked by name.
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:similar", "web", "https://example.org/icls", "19th ICLS Resolution I (2013)"])
    links = LabourLinks(conn)
    result = links.link_references(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert result["linked"] and result["unresolved"]
    linked = links.links(h.NS, scopes=h.READ_ONLY, state="linked")
    assert {link["target"]["id"] for link in linked} == {"doc:esms"}
    assert all(link["reference"]["identifier"] == "lfsa_esms" for link in linked)
    unresolved = links.links(h.NS, scopes=h.READ_ONLY, state="unresolved")
    icls = [u for u in unresolved if u["reference"]["identifier"] == "19th ICLS Resolution I (2013)"]
    assert icls and all(u["target"] is None for u in icls)
    celex = [u for u in unresolved if u["reference"].get("scheme") == "celex"]
    assert celex and celex[0]["evidence"]["lookup"] == "provider_absent"
    # Idempotent.
    assert links.link_references(h.NS, principal_id="svc", scopes=h.SCOPES) == {"linked": [], "unresolved": []}


def test_denominators_link_only_when_the_source_names_a_held_demographic_series():
    conn = h.connection()
    dh.apply(conn, "eurostat", 0)  # demo_pjan, Germany
    item = copy.deepcopy(h.source("eurostat"))
    declared = item["labour_statistics"]["documents"]
    declared[2]["denominator"] = {"provider": "eurostat", "series_code": "demo_pjan", "geography_code": "DE",
                                  "label": "Population on 1 January", "stated_in": "fixture declaration"}
    h.apply(conn, "eurostat", item=item, retrieved_at_ms=h.FIRST_RETRIEVAL)
    result = LabourLinks(conn).link_denominators(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert len(result["linked"]) == 1 and len(result["unresolved"]) == 1
    links = {link["reference"]["series_code"]: link for link in LabourLinks(conn).links(h.NS, scopes=h.READ_ONLY,
                                                                                         kind="denominator")}
    assert links["demo_pjan"]["target"]["kind"] == "demographic-series"
    assert links["lfsa_agan"]["state"] == "unresolved" and "not held" in links["lfsa_agan"]["evidence"]["reason"]
    assert "value" not in json.dumps(links["demo_pjan"]["target"])


def test_definition_differences_become_proposed_notes_for_the_same_place():
    conn = h.connection()
    h.load_all(conn)
    places = h.register_places(conn)
    identity = LabourIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="a", scopes=h.SCOPES,
                                             geo_namespace="geo")["assertions"]:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code", principal_id="r",
                            scopes=h.SCOPES)
    proposed = LabourLinks(conn).propose_definition_notes(h.NS, principal_id="analyst", scopes=h.SCOPES)
    notes = [LabourComparability(conn).note(h.NS, n, scopes=h.READ_ONLY) for n in proposed["proposed"]]
    relations = {n["relation"] for n in notes}
    assert {"different_definition_basis", "different_age_bounds"} <= relations
    # ILO (DEU) and Eurostat (DE) series meet through the accepted place mapping of both codes to Germany.
    cross = [n for n in notes if {c["provider"] for c in n["cited"]} == {"ilostat", "eurostat-lfs"}]
    assert cross and all(n["state"] == "proposed" for n in cross)
    assert places["de"]
