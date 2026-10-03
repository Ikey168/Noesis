"""IB07 (#2738): business series linked to Labour, Trade and methodology documents by citation or accepted match."""

from __future__ import annotations

import json

import pytest

from src.kb.business_statistics_identity import BusinessIdentity
from src.kb.business_statistics_links import BusinessLinks
from src.kb.business_statistics_records import BusinessError
from src.kb.labour_identity import LabourIdentity
from tests.unit import business_statistics_harness as h
from tests.unit import labour_harness as lh
from tests.unit import trade_harness as th


def _accept_places(conn, identity, proposals):
    for assertion in proposals["assertions"]:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "published code", principal_id="reviewer",
                            scopes=h.SCOPES | lh.SCOPES)


def test_absent_providers_and_targets_are_reported_never_dropped():
    conn = h.connection()
    h.load_all(conn)
    links = BusinessLinks(conn)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    trade = links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert labour["missing"] and trade["missing"] and not labour["linked"] and not trade["linked"]
    states = {(link["kind"], link["state"]) for link in links.links(h.NS, scopes=h.READ_ONLY)}
    assert states == {("labour", "provider_absent"), ("trade", "provider_absent")}
    absent = links.links(h.NS, scopes=h.READ_ONLY, kind="trade")[0]
    assert "not installed" in absent["evidence"]["reason"] and absent["vintage_id"]
    # Idempotent: linking again adds nothing.
    assert links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES) == {"linked": [], "missing": []}


def test_labour_links_by_shared_code_or_accepted_place_matches_pin_revisions_and_never_merge():
    conn = h.connection()
    h.load_all(conn)
    lh.load_all(conn)
    labour_vintages = conn.execute("SELECT count(*) FROM labour_vintages").fetchone()[0]
    links = BusinessLinks(conn)
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    sts = h.series_by(conn, "eurostat-sts", classification="C", adjustment="SCA", unit="I21")
    linked = [link for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=sts["series_id"], kind="labour")
              if link["state"] == "linked"]
    assert linked and {link["basis"] for link in linked} == {"shared-identifier"}
    assert {link["target"]["provider"] for link in linked} == {"eurostat-lfs"}
    sector_link = next(link for link in linked if link["target"]["sector"])
    assert sector_link["evidence"]["classification"]["relation"] == "same-code"  # NACE Rev.2 C on both sides
    assert sector_link["target"]["vintage_id"] and sector_link["vintage_id"]  # both sides pinned
    assert "value" not in json.dumps(sector_link["target"]) and "no value is combined" in sector_link["evidence"][
        "note"]
    cbp = h.series_by(conn, "us-census-cbp", indicator="EMP", classification="31-33", version="2022")
    before = links.links(h.NS, scopes=h.READ_ONLY, series_id=cbp["series_id"], kind="labour")
    assert [link["state"] for link in before] == ["target_not_held"]  # LAUS codes differ until places are reviewed

    places = h.register_places(conn)
    business = BusinessIdentity(conn)
    _accept_places(conn, business, business.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES))
    labour = LabourIdentity(conn)
    _accept_places(conn, labour, labour.propose_places(lh.NS, principal_id="proposer", scopes=lh.SCOPES,
                                                       geo_namespace="global"))
    links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    after = [link for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=cbp["series_id"], kind="labour")
             if link["state"] == "linked"]
    assert after and {link["basis"] for link in after} == {"accepted-match"}
    laus = next(link for link in after if link["target"]["native_key"].startswith("LASST06"))
    assert laus["evidence"]["place_id"] == places["ca"]
    assert laus["evidence"]["business_assertion_id"] and laus["evidence"]["labour_assertion_id"]
    assert laus["evidence"]["classification"]["relation"] == "none"
    # Linking never rewrites the Labour store: its vintages are unchanged.
    assert conn.execute("SELECT count(*) FROM labour_vintages").fetchone()[0] == labour_vintages


def test_trade_links_by_reporter_place_only():
    conn = h.connection()
    h.load_all(conn)
    th.load_all(conn)
    links = BusinessLinks(conn)
    with pytest.raises(BusinessError):
        links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES - {"knowledge:trade:read"})
    links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    births = h.series_by(conn, "eurostat-business-demography", indicator="V11920")
    linked = [link for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=births["series_id"], kind="trade")
              if link["state"] == "linked"]
    assert linked and {link["target"]["provider"] for link in linked} == {"eurostat-comext"}
    assert {link["basis"] for link in linked} == {"shared-identifier"}
    assert "not mapped" in linked[0]["evidence"]["classification"]

    h.register_places(conn)
    identity = BusinessIdentity(conn)
    _accept_places(conn, identity, identity.propose_places(h.NS, principal_id="proposer", scopes=h.SCOPES))
    links.link_trade(h.NS, principal_id="svc", scopes=h.SCOPES)
    via_place = [link for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=births["series_id"], kind="trade")
                 if link["basis"] == "accepted-match"]
    assert via_place and {link["target"]["reporter"]["scheme"] for link in via_place} == {"m49"}
    cbp = h.series_by(conn, "us-census-cbp", indicator="ESTAB", classification="00", version="2017")
    assert {link["state"] for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=cbp["series_id"],
                                                  kind="trade")} == {"target_not_held"}


def test_methodology_references_resolve_only_by_exact_url():
    from src.ingestion.document_store import _SCHEMA as DOCUMENTS_DDL

    conn = h.connection()
    h.load_all(conn)
    links = BusinessLinks(conn)
    first = links.link_methodology(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert first["unresolved"] and not first["linked"]
    conn.execute(DOCUMENTS_DDL)
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:sts-esms", "web", "https://ec.europa.eu/eurostat/cache/metadata/en/sts_esms.htm", "STS ESMS"])
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:similar", "web", "https://example.org/cbp", "County Business Patterns methodology"])
    second = links.link_methodology(h.NS, principal_id="svc", scopes=h.SCOPES)
    resolved = [links.link(h.NS, link_id) for link_id in second["linked"]]
    assert resolved and {link["target"]["id"] for link in resolved} == {"doc:sts-esms"}
    assert {link["basis"] for link in resolved} == {"citation"}
