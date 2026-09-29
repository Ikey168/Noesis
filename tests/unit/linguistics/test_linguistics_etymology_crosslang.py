"""Cited etymology chains (LG07, #2185) and cross-language links through the existing records (LG09, #2187)."""

from __future__ import annotations

import json

import pytest

from src.kb.linguistics_crosslang import CrossLanguageLinks
from src.kb.linguistics_etymology import Etymologies
from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import LinguisticsError
from tests.unit import linguistics_harness as h

NS = h.NS
NOUN_WD, NOUN_WT = (
    "lexeme:wikidata-lexemes:L90001",
    "lexeme:kaikki-wiktextract:qnv:tamo:noun:1",
)
TAMU_WD, TAMU_WT = (
    "lexeme:wikidata-lexemes:L90002",
    "lexeme:kaikki-wiktextract:qsv:tamu:noun:0",
)


def accept_lexeme_matches(conn):
    identity = LinguisticsIdentity(conn)
    for candidate in identity.propose_lexeme_matches(
        NS, principal_id="p", scopes=h.SCOPES
    )["candidates"]:
        identity.review(
            NS,
            candidate["candidate_id"],
            "accept",
            "same word",
            principal_id="r",
            scopes=h.SCOPES,
        )


@pytest.fixture()
def conn():
    value = h.connect()
    h.load_all(value)
    return value


def test_borrowed_word_keeps_its_unacquired_source_as_cited_text(conn):
    chain = Etymologies(conn).chain(
        NS, "lexeme:kaikki-wiktextract:qnv:banka:noun:0", scopes=h.SCOPES
    )
    link = chain["links"][0]
    assert link["relation"] == "borrowed" and link["native_relation"] == "bor"
    assert (
        link["target"]["status"] == "unresolved"
        and link["target"]["cited"]["text"] == "banca"
    )
    assert (
        link["citation"]["share_alike"] is True
        and chain["licences"]["share_alike"] is True
    )
    assert chain["notes"][0]["text"] == "Borrowed from Italian banca."


def test_disputed_etymology_is_shown_side_by_side_with_an_unresolved_proto_form(conn):
    accept_lexeme_matches(conn)
    chain = Etymologies(conn).chain(NS, NOUN_WD, scopes=h.SCOPES)
    assert chain["equivalent_lexemes"] == sorted([NOUN_WD, NOUN_WT])
    relations = {
        (link["relation"], link["citation"]["provider"])
        for link in chain["links"]
        if link["depth"] == 0
    }
    assert relations == {
        ("borrowed", "wikidata-lexemes"),
        ("inherited", "kaikki-wiktextract"),
    }
    conflict = chain["conflicts"][0]
    assert {a["relation"] for a in conflict["alternatives"]} == {
        "borrowed",
        "inherited",
    }
    assert "nothing resolved" in conflict["note"]
    proto = next(
        link
        for link in chain["links"]
        if link["relation"] == "inherited" and link["depth"] == 0
    )
    assert (
        proto["target"]["status"] == "unresolved"
        and proto["target"]["cited"]["text"] == "*tamə"
    )
    borrowed = next(link for link in chain["links"] if link["relation"] == "borrowed")
    assert borrowed["target"]["status"] == "resolved" and set(
        borrowed["target"]["group"]
    ) == {TAMU_WD, TAMU_WT}
    assert (
        borrowed["references"][0]["doi"] == "10.5555/velan.1998.12"
        and borrowed["references"][0]["literature"] == []
    )
    # the chain continues through the resolved source: tamu's own (unreferenced) derivation and inheritance
    deeper = [link for link in chain["links"] if link["depth"] == 1]
    assert {link["reference_status"] for link in deeper} >= {"unreferenced"}
    unacquired = next(
        link for link in deeper if link["citation"]["provider"] == "wikidata-lexemes"
    )
    assert (
        unacquired["target"]["status"] == "unresolved"
        and unacquired["target"]["cited"]["source_lexeme_id"] == "L90005"
    )
    assert [c["relation"] for c in chain["cognates"]] == ["cognate"]
    assert chain["cycles"] == [] and chain["licences"]["n"] >= 5


def test_cycles_are_reported_not_followed(conn):
    from src.kb.linguistics_store import LinguisticsStore

    record = {
        "contract": "noesis-linguistic-record-v1",
        "kind": "etymology_assertion",
        "provider": "wikidata-lexemes",
        "source": {
            "source_id": "L90002$cycle",
            "revision": "2100000099",
            "licence": {
                "id": "CC0-1.0",
                "attribution": "Wikidata, CC0",
                "share_alike": False,
            },
        },
        "body": {
            "lexeme": TAMU_WD,
            "relation": "derived",
            "native_relation": "P5191",
            "target": {"source_lexeme_id": "L90001", "provider": "wikidata-lexemes"},
            "references": [],
            "reference_status": "unreferenced",
        },
    }
    LinguisticsStore(conn).apply(
        NS, [record], run_id="cycle", observed_at_ms=h.ms("2026-03-06")
    )
    chain = Etymologies(conn).chain(NS, NOUN_WD, scopes=h.SCOPES)
    assert chain["cycles"] and chain["cycles"][0]["back_to"] == [NOUN_WD]


def test_doi_references_resolve_to_science_literature_records(conn):
    conn.execute(
        "CREATE TABLE documents (document_id TEXT, source_type TEXT, title TEXT, metadata TEXT)"
    )
    conn.execute(
        "INSERT INTO documents VALUES ('paper:velan', 'paper', 'Velan etymologies', ?)",
        [json.dumps({"doi": "https://doi.org/10.5555/VELAN.1998.12"})],
    )
    chain = Etymologies(conn).chain(NS, NOUN_WD, scopes=h.SCOPES)
    borrowed = next(link for link in chain["links"] if link["relation"] == "borrowed")
    assert borrowed["references"][0]["literature"] == [
        {
            "document_id": "paper:velan",
            "title": "Velan etymologies",
            "doi": "10.5555/velan.1998.12",
        }
    ]


def test_unknown_lexeme_has_no_etymology_on_record(conn):
    chain = Etymologies(conn).chain(NS, "lexeme:wikidata-lexemes:L1", scopes=h.SCOPES)
    assert chain["status"] == "not_on_record" and chain["licences"]["n"] == 0


def test_sourced_translation_and_shared_sense_item_links(conn):
    links = CrossLanguageLinks(conn).links(NS, NOUN_WT, scopes=h.SCOPES)
    kinds = {link["kind"] for link in links["links"]}
    assert kinds == {"sourced-translation", "shared-sense-item"}
    incoming = next(
        link for link in links["links"] if link["kind"] == "sourced-translation"
    )
    assert incoming["direction"] == "incoming" and incoming["from"].startswith(
        "sense:kaikki-wiktextract:en:river"
    )
    shared = next(
        link for link in links["links"] if link["kind"] == "shared-sense-item"
    )
    assert (
        shared["item"] == "Q4022"
        and TAMU_WT in shared["linked_lexemes"]
        and TAMU_WD in shared["linked_lexemes"]
    )
    assert links["licences"]["share_alike"] is True


def test_search_alias_and_machine_translation_go_through_cross_language(conn):
    links = CrossLanguageLinks(conn)
    indexed = links.index(NS, principal_id="p", scopes=h.SCOPES)
    assert (
        indexed["written"] > 0
        and links.index(NS, principal_id="p", scopes=h.SCOPES)["written"] == 0
    )
    found = links.search(NS, "river", scopes=h.SCOPES)
    definitions = [
        hit
        for hit in found["results"]
        if hit.get("object_type") == "linguistics-definition"
    ]
    assert definitions and all(
        hit["linguistics"]["origin"] == "source-published" for hit in definitions
    )
    alias = links.propose_alias(
        NS, NOUN_WD, "entity:river", "Q4022", principal_id="p", scopes=h.SCOPES
    )
    assert (
        alias["contract"] == "noesis-multilingual-alias-v1"
        and alias["status"] == "candidate"
    )
    with pytest.raises(LinguisticsError):
        links.propose_alias(
            NS, NOUN_WD, "entity:x", "Q1", principal_id="p", scopes=h.SCOPES
        )
    definition = "definition_revision:wikidata-lexemes:L90001-S1:en"
    translated = links.record_translation(
        NS,
        definition,
        "de",
        "Fluss",
        {"name": "fixture-mt", "kind": "machine"},
        principal_id="p",
        scopes=h.SCOPES,
    )
    assert (
        translated["contract"] == "noesis-translation-record-v1"
        and translated["status"] == "unreviewed"
    )
    assert "not a sourced definition" in translated["label"]
    # the machine translation is never a sourced definition
    from src.kb.linguistics_store import LinguisticsStore

    texts = [
        r["body"]["text"]
        for r in LinguisticsStore(conn).currents(NS, kind="definition_revision")
    ]
    assert "Fluss" not in texts
    hit = next(
        h_
        for h_ in links.search(NS, "Fluss", scopes=h.SCOPES)["results"]
        if h_["kind"] == "translation"
    )
    assert (
        hit["label"] == "Noesis translation (not a sourced definition)"
        and hit["status"] == "unreviewed"
    )
    assert (
        links.translations(NS, LinguisticsStore(conn).current(NS, definition))[0][
            "status"
        ]
        == "unreviewed"
    )


def test_no_new_alias_translation_or_search_tables(conn):
    links = CrossLanguageLinks(conn)
    links.index(NS, principal_id="p", scopes=h.SCOPES)
    links.propose_alias(
        NS, NOUN_WD, "entity:river", "Q4022", principal_id="p", scopes=h.SCOPES
    )
    LinguisticsIdentity(conn).propose_lexeme_matches(
        NS, principal_id="p", scopes=h.SCOPES
    )
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    linguistics = {t for t in tables if t.startswith("ling_")}
    assert not {
        t
        for t in linguistics
        if any(w in t for w in ("alias", "translation", "search", "index", "text"))
    }
    assert {"multilingual_aliases", "translation_records", "language_texts"} <= tables
