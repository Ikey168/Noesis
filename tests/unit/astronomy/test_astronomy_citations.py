"""Astronomy records linked to Science papers by bibcode and DOI (#2149, AS09)."""

from __future__ import annotations

import pytest

from src.kb.astronomy_citations import AstronomyCitations, stated_citations
from src.kb.astronomy_queries import AstronomyQueries
from src.kb.astronomy_records import AstronomyError
from tests.unit.astronomy import harness as h


@pytest.fixture()
def world():
    conn = h.connection()
    h.acquire_all(conn)
    papers = h.seed_papers(conn, lambda: h.ms("2099-07-01"))
    return conn, papers


def test_stated_references_normalise_bibcodes_dois_and_circulars():
    record = {
        "kind": "exoplanet",
        "reference": {"text": "x", "bibcode": "2099AJ....999..101F"},
        "discovery": {"reference": {"text": "y", "doi": "10.5555/FICT.2099.101"}},
    }
    assert [(c["kind"], c["value"]) for c in stated_citations(record)] == [
        ("bibcode", "2099AJ....999..101F"),
        ("doi", "10.5555/fict.2099.101"),
    ]
    identification = {"kind": "identification", "announced_in": "MPEC 2099-G42"}
    assert stated_citations(identification)[0] == {
        "kind": "circular",
        "value": "MPEC 2099-G42",
        "stated": "MPEC 2099-G42",
    }


def test_links_resolve_by_exact_identifier_and_unresolved_stays_visible(world):
    conn, papers = world
    citations = AstronomyCitations(conn)
    result = citations.link(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    links = result["links"]
    by_value = {}
    for link in links:
        by_value.setdefault(link["citation_value"], set()).add(
            (link["state"], link.get("document_id"))
        )
    # The bibcode resolves to the paper whose metadata states it (and its DOI); the pairing is the paper's own.
    assert by_value["2099AJ....999..101F"] == {("linked", papers["fict-101-b"])}
    assert by_value["10.5555/fict.2099.101"] == {
        ("linked", papers["fict-101-discovery"])
    }
    # Unresolved references stay visible; circulars are never papers; no title matching links "unrelated".
    assert by_value["10.5555/fict.2099.303"] == {("unresolved", None)}
    assert by_value["MPEC 2099-G42"] == {("unresolved", None)}
    assert papers["unrelated"] not in {link.get("document_id") for link in links}
    assert all(
        link["direction"].startswith("astronomy record -> Science paper")
        for link in links
    )
    again = citations.link(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert again["created"] == []
    # Answers carry the links.
    answer = AstronomyQueries(conn).exoplanet_status_as_of(
        h.NS, "Fict-101 b", "2099-07-01", scopes=h.SCOPES
    )
    ps = next(r for r in answer["dispositions"] if r["source_table"] == "ps")
    assert {(p["citation_kind"], p["state"]) for p in ps["papers"]} == {
        ("bibcode", "linked")
    }


def test_revert_is_final_for_that_paper_revision(world):
    conn, papers = world
    citations = AstronomyCitations(conn)
    links = citations.link(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["links"]
    target = next(
        link for link in links if link.get("document_id") == papers["fict-101-b"]
    )
    reverted = citations.revert(
        h.NS, target["link_id"], "wrong paper", principal_id="rev", scopes=h.SCOPES
    )
    assert reverted["state"] == "reverted"
    citations.link(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert citations._row(h.NS, target["link_id"])["state"] == "reverted"
    with pytest.raises(AstronomyError):
        citations.revert(
            h.NS, target["link_id"], "again", principal_id="rev", scopes=h.SCOPES
        )
    with pytest.raises(AstronomyError):
        citations.link(h.NS, principal_id=h.PRINCIPAL, scopes=h.READ_ONLY)
