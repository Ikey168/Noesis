"""Regressions from the federal-statutes code review (#2105): fingerprints, amendment context, undated
corrections and incremental link refreshes."""

from __future__ import annotations

import pytest

from src.ingestion.federal_law_formats import parse_bgbl_act
from src.kb.legal import LegalStore
from src.kb.legal_federal import FederalStatutes, current_stated, rebuild_federal_links
from tests.unit import federal_statutes_harness as h

LINK_TABLES = {
    "legal_provision_citations": "citation_id",
    "legal_implementation_links": "link_id",
    "legal_amendments": "amendment_id",
}


def test_identical_text_from_both_sources_selects_one_and_cites_both():
    """GII carries headings and doc-numbered footnotes RIS lacks; identical provisions are no conflict."""
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    h.apply(conn, "ris", h.ris_pages(("2029-06-01",)), at="2030-03-02")
    federal = FederalStatutes(LegalStore(conn))
    work = federal.statute_work(h.NS, "MPHG")
    versions = federal.versions(h.NS, work["work_id"])
    assert (
        len({v["text_sha256"] for v in versions}) == 2
    )  # the sources' own hashes differ
    result = LegalStore(conn).select_as_of(
        h.NS, work["work_id"], "2030-03-15", scopes=h.READ_ONLY
    )
    assert result["status"] == "source_stated", result["status"]
    assert result["selected"]["validity_basis"] == "source_stated"
    assert [c["validity_basis"] for c in result["corroborated_by"]] == ["observed"]
    assert result["sources"] == ["gii-federal-statutes", "ris-federal-statute-versions"]
    provision = federal.provision_as_of(
        h.NS, "MPHG", "§8", "2030-03-15", scopes=h.READ_ONLY
    )
    assert provision["status"] == "source_stated" and provision["corroborated_by"]


ACT = """<?xml version="1.0" encoding="UTF-8"?>
<akn:akomaNtoso xmlns:akn="http://Inhaltsdaten.LegalDocML.de/1.7/"><akn:act name="regelungstext">
<akn:meta><akn:identification><akn:FRBRWork><akn:FRBRthis value="eli/bund/bgbl-1/2030/47/regelungstext-1"/>
<akn:FRBRdate date="2030-05-02" name="verkuendungsfassung"/></akn:FRBRWork></akn:identification></akn:meta>
<akn:preface><akn:longTitle><akn:p><akn:docTitle>Zweites Gesetz zur Änderung des
Musterpapierhandelsgesetzes (fiktiv)</akn:docTitle></akn:p></akn:longTitle></akn:preface>
<akn:body>
<akn:p>Artikel 1</akn:p><akn:p>Änderung des Musterpapierhandelsgesetzes</akn:p>
<akn:p>Das Musterpapierhandelsgesetz wird wie folgt geändert:</akn:p>
<akn:p>1. § 3 wird wie folgt geändert:</akn:p>
<akn:p>a) Absatz 1 wird aufgehoben.</akn:p>
<akn:p>2. Die Inhaltsübersicht wird wie folgt geändert:</akn:p>
<akn:p>a) Absatz 2 wird aufgehoben.</akn:p>
<akn:p>b) Die Angabe zu § 8 wird gestrichen.</akn:p>
<akn:p>3. § 5 Absatz 2 wird wie folgt gefasst: „(2) Frist.“</akn:p>
</akn:body></akn:act></akn:akomaNtoso>"""


def test_items_without_a_provision_reset_the_context_of_their_sub_items():
    act = parse_bgbl_act(
        ACT.encode(), key="bgbl-1/2030/nr-47", statutes=[h.MPHG], source_url="x"
    )
    rows = [
        (a["item"], a["status"], a["provision"]) for a in act["fields"]["amendments"]
    ]
    assert rows == [
        ("1.", "resolved", "§3"),
        ("a)", "resolved", "§3/abs1"),
        ("2.", "ambiguous", None),
        # Never §3/abs2 or §8: the enclosing item names no single provision.
        ("a)", "unresolved", None),
        ("b)", "unresolved", None),
        ("3.", "resolved", "§5/abs2"),
    ]
    unresolved = [a for a in act["fields"]["amendments"] if a["status"] == "unresolved"]
    assert all(a["instruction"] and a["locator"]["path"] for a in unresolved)


def expression(pages, day="2030-04-01"):
    return next(
        m["item"]["workExample"]
        for m in pages[0]["body"]["member"]
        if day in m["item"]["workExample"]["legislationIdentifier"]
    )


def stated_rows(conn):
    federal = FederalStatutes(LegalStore(conn))
    work = federal.statute_work(h.NS, "MPHG")
    return [
        v
        for v in federal.versions(h.NS, work["work_id"])
        if v["validity_basis"] == "source_stated"
    ]


def test_an_undated_later_expression_becomes_current_and_replays_are_idempotent():
    conn = h.connection()
    h.apply(
        conn, "ris", h.ris_pages(("2030-04-01",)), at="2030-04-05"
    )  # dated 2030-04-02
    undated = h.ris_pages(("2030-04-01",))
    example = expression(undated)
    example.pop("dateModified")
    example["temporalCoverage"] = "2030-04-01/2030-11-30"
    first = h.apply(conn, "ris", undated, at="2030-05-01")
    assert first["corrections"] == 1
    current = [v for v in stated_rows(conn) if v["current"]]
    assert [v["validity_to"] for v in current] == ["2030-11-30"]
    for at in ("2030-05-02", "2030-05-03"):
        replay = h.apply(conn, "ris", undated, at=at)
        assert (
            replay["statute_versions"],
            replay["corrections"],
            replay["unchanged"],
        ) == (0, 0, 1)
    assert len(stated_rows(conn)) == 2
    # A dated replay of the first statement after the undated one is a new correction, recorded once.
    again = h.apply(conn, "ris", h.ris_pages(("2030-04-01",)), at="2030-05-04")
    assert again["corrections"] == 1
    assert (
        h.apply(conn, "ris", h.ris_pages(("2030-04-01",)), at="2030-05-05")["unchanged"]
        == 1
    )
    assert [v["validity_to"] for v in stated_rows(conn) if v["current"]] == [
        "2030-12-31"
    ]


def test_current_stated_ranks_by_source_date_then_observation_order():
    assert current_stated([("a", "2030-04-02", 1), ("b", None, 2)]) == "b"
    assert current_stated([("a", None, 1), ("b", "2030-04-02", 2)]) == "b"
    assert current_stated([("a", "2030-05-01", 1), ("b", "2030-03-01", 2)]) == "a"
    assert current_stated([("a", None, 1), ("b", None, 2)]) == "b"
    assert current_stated([]) is None


def snapshot(conn):
    return {
        table: conn.execute(f"SELECT * FROM {table} ORDER BY {key}").fetchall()
        for table, key in LINK_TABLES.items()
    }


ORDERS = {
    "statutes_first": ("cellar", "gii1", "ris", "bgbl", "gii2", "rii"),
    "decisions_first": ("rii", "cellar", "bgbl", "gii1", "ris", "gii2"),
    "mixed": ("gii1", "rii", "bgbl", "cellar", "gii2", "ris"),
}


def ingest(conn, step):
    if step == "cellar":
        h.cellar_directive(conn)
    elif step == "gii1":
        h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    elif step == "gii2":
        h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    elif step == "ris":
        h.apply(conn, "ris", h.ris_pages(), at="2030-04-05")
    elif step == "bgbl":
        h.apply(conn, "bgbl", h.bgbl_pages(), at="2030-03-15")
    else:
        h.apply(conn, "rii", h.rii_pages(), at="2031-06-01")


@pytest.mark.parametrize("order", sorted(ORDERS))
def test_incremental_link_refresh_equals_a_full_rebuild(order):
    conn = h.connection()
    for step in ORDERS[order]:
        ingest(conn, step)
    incremental = snapshot(conn)
    assert (
        incremental["legal_provision_citations"]
        and incremental["legal_implementation_links"]
    )
    conn.execute("BEGIN")
    rebuild_federal_links(LegalStore(conn), h.NS)
    conn.execute("COMMIT")
    assert snapshot(conn) == incremental
    # Every ordering converges on the same links.
    reference = h.connection()
    for step in ORDERS["statutes_first"]:
        ingest(reference, step)
    assert snapshot(reference) == incremental


def test_eu_court_and_berlin_ingests_do_not_touch_federal_links_without_federal_records(
    monkeypatch,
):
    import src.kb.legal_federal as federal_module

    calls = []
    monkeypatch.setattr(
        federal_module.FederalStatutes,
        "rebuild_decision_citations",
        lambda self, namespace, **kw: calls.append(kw) or 0,
    )
    conn = h.connection()
    h.apply(conn, "rii", h.rii_pages(), at="2031-06-01")
    h.cellar_directive(conn)
    assert calls == []
    assert (
        conn.execute("SELECT count(*) FROM legal_provision_citations").fetchone()[0]
        == 0
    )
    # With federal records present, an unrelated EU ingest re-links nothing.
    monkeypatch.undo()
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    before = snapshot(conn)
    monkeypatch.setattr(
        federal_module.FederalStatutes,
        "rebuild_decision_citations",
        lambda self, namespace, **kw: calls.append(kw) or 0,
    )
    record = {
        "contract": "noesis-native-regional-v1",
        "provider": "cellar",
        "provider_id": "cellar:fiktiv-2",
        "kind": "legislation",
        "language": "de",
        "title": "Fiktiver Rechtsakt",
        "fields": {
            "work": "http://publications.europa.eu/resource/cellar/fiktiv-2",
            "celex": "32031R0001",
        },
        "native": {"xml_sha256": "1" * 64},
        "sections": [],
    }
    LegalStore(conn).project(h.NS, [record], run_id="eu", source_id="cellar-fixture")
    assert calls == [] and snapshot(conn) == before
