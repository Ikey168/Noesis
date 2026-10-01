"""IP07 (#2619): income series linked to Demographics, Labour and methodology documents by basis, pinned revisions."""

from __future__ import annotations

import json

from src.ingestion.document_store import _SCHEMA as DOCUMENTS_DDL
from src.kb.income_distribution_identity import IncomeIdentity
from src.kb.income_distribution_links import IncomeLinks
from tests.unit import demographics_harness as dh
from tests.unit import income_distribution_harness as h
from tests.unit import labour_harness as lh

SCOPES = h.SCOPES | {"knowledge:demographics:read", "knowledge:labour:read"}
ESMS = "https://ec.europa.eu/eurostat/cache/metadata/en/ilc_esms.htm"


def test_missing_providers_and_targets_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    links = IncomeLinks(conn)
    demographics = links.link_demographics(h.NS, principal_id="svc", scopes=SCOPES)
    labour = links.link_labour(h.NS, principal_id="svc", scopes=SCOPES)
    assert demographics["missing"] and labour["missing"] and not demographics["linked"]
    states = {(link["kind"], link["state"]) for link in links.links(h.NS, scopes=h.READ_ONLY)}
    assert states == {("denominator", "provider_absent"), ("labour", "provider_absent")}
    absent = links.links(h.NS, scopes=h.READ_ONLY, kind="labour")[0]
    assert "not installed" in absent["evidence"]["reason"] and absent["vintage_id"]


def test_shared_identifiers_and_accepted_matches_link_pinned_revisions_without_derived_values():
    conn = h.connection()
    h.load_all(conn)
    dh.apply(conn, "eurostat", 0)  # Eurostat demo_pjan for DE
    lh.load_all(conn)  # Labour series for DE (Eurostat LFS) and DEU (ILOSTAT, OECD)
    links = IncomeLinks(conn)
    result = links.link_demographics(h.NS, principal_id="svc", scopes=SCOPES)
    silc = h.series(conn, "eurostat-silc", "LI_R_MD60")
    denominators = links.links(h.NS, scopes=h.READ_ONLY, series_id=silc["series_id"], kind="denominator")
    (linked,) = [link for link in denominators if link["state"] == "linked"]
    assert linked["basis"] == "shared-identifier" and linked["target"]["series_code"] == "demo_pjan"
    assert linked["target"]["vintage_id"] and linked["vintage_id"]  # both sides pinned to a revision
    assert "shared_reference_years" in linked["evidence"] and "value" not in json.dumps(linked["target"])
    # PIP (DEU) has no DEU population series held: reported until an accepted match supplies the DE code.
    pip = h.series(conn, "pip", "pip:DEU:national:income:headcount", ppp=2017)
    pip_links = links.links(h.NS, scopes=h.READ_ONLY, series_id=pip["series_id"], kind="denominator")
    assert [link["state"] for link in pip_links] == ["target_not_held"]
    assert result["missing"]

    h.register_places(conn, keys=("de",))
    identity = IncomeIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="proposer", scopes=SCOPES)["proposed"]:
        identity.review(h.NS, assertion["assertion_id"], "accept", "identifier", principal_id="reviewer",
                        scopes=SCOPES)
    links.link_demographics(h.NS, principal_id="svc", scopes=SCOPES)
    via_place = [link for link in links.links(h.NS, scopes=h.READ_ONLY, series_id=pip["series_id"],
                                              kind="denominator") if link["state"] == "linked"]
    assert via_place and via_place[0]["basis"] == "accepted-match"
    assert via_place[0]["evidence"]["assertion_id"] and via_place[0]["reference"] == {"scheme": "eurostat-geo",
                                                                                    "code": "DE"}

    labour = links.link_labour(h.NS, principal_id="svc", scopes=SCOPES)
    assert labour["linked"]
    oecd = h.series(conn, "oecd-idd", "INC_DISP_GINI")
    targets = {link["target"]["provider"] for link in links.links(h.NS, scopes=h.READ_ONLY,
                                                                 series_id=oecd["series_id"], kind="labour")
               if link["state"] == "linked"}
    assert {"ilostat", "oecd"} <= targets
    # Idempotent.
    assert links.link_labour(h.NS, principal_id="svc", scopes=SCOPES) == {"linked": [], "missing": []}


def test_methodology_references_resolve_by_exact_url_only():
    conn = h.connection()
    h.load_all(conn)
    conn.execute(DOCUMENTS_DDL)
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:ilc-esms", "web", ESMS, "Income and living conditions - ESMS metadata"])
    conn.execute("INSERT INTO documents (document_id, source_type, url, title) VALUES (?,?,?,?)",
                 ["doc:similar", "web", "https://example.org/pip", "PIP methodology handbook"])
    links = IncomeLinks(conn)
    result = links.link_methodology(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert result["linked"] and result["unresolved"]
    linked = [link for link in links.links(h.NS, scopes=h.READ_ONLY, kind="methodology") if link["state"] == "linked"]
    assert {link["target"]["id"] for link in linked} == {"doc:ilc-esms"}
    assert all(link["basis"] == "citation" for link in linked)
    celex = [link for link in links.links(h.NS, scopes=h.READ_ONLY, kind="methodology")
             if link["reference"].get("scheme") == "celex"]
    assert celex and {link["state"] for link in celex} == {"unresolved"}
