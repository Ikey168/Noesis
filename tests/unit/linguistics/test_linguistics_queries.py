"""As-of lexeme, sense and definition answers and cited typological profiles (LG08, #2186)."""

from __future__ import annotations

import pytest

from src.kb.linguistics_crosslang import CrossLanguageLinks
from src.kb.linguistics_queries import LinguisticsQueries, export_lexeme_dossier
from src.kb.linguistics_records import LinguisticsError
from tests.unit import linguistics_harness as h

NS = h.NS
NOUN_SENSE_WT = "sense:kaikki-wiktextract:qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2"


@pytest.fixture(scope="module")
def conn():
    value = h.connect()
    h.load_all(value, 1)
    h.load_all(value, 2, keys=("wikidata", "kaikki", "glottolog", "wals"))
    return value


def test_lookup_lexeme_returns_every_source_with_paradigms_senses_and_licences(conn):
    answer = LinguisticsQueries(conn).lookup_lexeme(
        NS, "TAMO", "nort3456", scopes=h.SCOPES
    )
    keys = {lexeme["record_key"]: lexeme for lexeme in answer["lexemes"]}
    assert set(keys) == {
        "lexeme:wikidata-lexemes:L90001",
        "lexeme:wikidata-lexemes:L90003",
        "lexeme:kaikki-wiktextract:qnv:tamo:noun:1",
        "lexeme:kaikki-wiktextract:qnv:tamo:verb:2",
    }
    noun = keys["lexeme:wikidata-lexemes:L90001"]
    assert (
        set(noun["paradigm"]) == {"Q110786", "Q146786"}
        and noun["paradigm"]["Q146786"][0]["text"] == "tamoi"
    )
    assert noun["senses"][0]["definitions"][0]["text"] == "river; a large stream"
    assert noun["senses"][0]["definitions"][0]["origin"] == "source-published"
    assert (
        keys["lexeme:wikidata-lexemes:L90003"]["status"] == "deleted"
    )  # recorded, not removed
    wiktionary = keys["lexeme:kaikki-wiktextract:qnv:tamo:noun:1"]
    example = wiktionary["senses"][0]["usage_examples"][0]
    assert example["gloss"]["validation"] == "validated" and example[
        "source_citation"
    ].startswith("Fictional")
    assert answer["licences"]["share_alike"] is True and isinstance(
        answer["licences"]["n"], int
    )
    assert any(
        "Wiktionary contributors" in a for a in answer["licences"]["attributions"]
    )


def test_lookup_with_a_macrolanguage_is_reported_not_guessed(conn):
    answer = LinguisticsQueries(conn).lookup_lexeme(NS, "tamo", "qmv", scopes=h.SCOPES)
    assert (
        answer["status"] == "ambiguous"
        and answer["lexemes"] == []
        and answer["unknowns"]
    )


def test_sense_history_as_of_and_definition_changes(conn):
    queries = LinguisticsQueries(conn)
    before = queries.sense_history(NS, NOUN_SENSE_WT, "2026-04-01", scopes=h.SCOPES)
    after = queries.sense_history(NS, NOUN_SENSE_WT, "2026-06-01", scopes=h.SCOPES)
    assert [d["text"] for d in before["definitions"]] == ["a river"]
    assert [d["text"] for d in after["definitions"]] == ["a large river; a stream"]
    assert after["definitions"][0]["in_force_since"] == "2026-06-01"
    too_early = queries.sense_history(NS, NOUN_SENSE_WT, "2026-01-01", scopes=h.SCOPES)
    assert too_early["definitions"] == [] and too_early["unknowns"]
    # acquisition cutoff: before the second extract was acquired, the old definition is in force
    cutoff = queries.sense_history(
        NS,
        NOUN_SENSE_WT,
        "2026-07-01",
        scopes=h.SCOPES,
        acquired_by_ms=h.ms("2026-03-10"),
    )
    assert [d["text"] for d in cutoff["definitions"]] == ["a river"]
    changes = queries.definition_changes(NS, NOUN_SENSE_WT, scopes=h.SCOPES)
    revisions = changes["changes"][0]["revisions"]
    assert [r["text"] for r in revisions] == ["a river", "a large river; a stream"]
    assert (
        revisions[1]["previous_revision_id"] == revisions[0]["revision_id"]
        and changes["n"] == 2
    )
    assert changes["licences"]["share_alike"] is True


def test_languoid_profile_across_a_glottolog_release_change(conn):
    queries = LinguisticsQueries(conn)
    before = queries.languoid_profile(
        NS, "coas1122", scopes=h.SCOPES, as_of="2026-03-01"
    )
    after = queries.languoid_profile(NS, "coas1122", scopes=h.SCOPES)
    assert before["parent"] == "nort3456" and after["parent"] == "sout7890"
    assert [c["release"] for c in after["classification_history"]] == ["v5.0", "v5.1"]
    assert (
        after["level"] == "dialect"
        and after["typology"]["values"][0]["value"] == "no value on record"
    )
    north = queries.languoid_profile(NS, "qnv", scopes=h.SCOPES)
    assert north["languoid"] == "nort3456" and north["identifiers"] == {
        "glottocode": "nort3456",
        "iso639_3": "qnv",
    }
    assert [p["name"] for p in north["classification_path"]] == ["Veltic"]
    assert north["iso639_3"]["macrolanguages"] == ["qmv"]
    assert north["iso639_3"]["retired_predecessors"][0]["code"] == "qrv"
    values = {v["parameter"]: v for v in north["typology"]["values"]}
    assert (
        values["81A"]["value"] == "SOV"
        and values["81A"]["references"][0]["pages"] == "12-14"
    )
    assert (
        values["87A"]["value"] == "Noun-Adjective"
        and north["wikidata_items"][0]["item"] == "Q900001"
    )
    south = queries.languoid_profile(NS, "sout7890", scopes=h.SCOPES)
    south_values = {v["parameter"]: v["value"] for v in south["typology"]["values"]}
    assert south_values == {"81A": "No dominant order", "87A": "no value on record"}
    assert "WALS 87A: no value on record" in south["unknowns"]
    assert (
        queries.languoid_profile(NS, "sout7890", scopes=h.SCOPES, as_of="2025-01-01")[
            "status"
        ]
        == "unresolved"
    )
    older = queries.languoid_profile(
        NS, "sout7890", scopes=h.SCOPES, as_of="2026-02-01"
    )
    assert {v["parameter"]: v["value"] for v in older["typology"]["values"]}[
        "81A"
    ] == "SVO"
    assert queries.languoid_profile(NS, "qmv", scopes=h.SCOPES)["status"] == "ambiguous"


def test_lexeme_etymology_reports_unknowns_and_licences(conn):
    answer = LinguisticsQueries(conn).lexeme_etymology(
        NS, "lexeme:kaikki-wiktextract:qnv:banka:noun:0", scopes=h.SCOPES
    )
    assert (
        answer["n"] == 1
        and answer["unknowns"]
        and answer["licences"]["share_alike"] is True
    )


def test_machine_translations_are_labelled_and_never_definitions(conn):
    CrossLanguageLinks(conn).record_translation(
        NS,
        "definition_revision:wikidata-lexemes:L90001-S1:en",
        "de",
        "Fluss",
        {"name": "fixture-mt", "kind": "machine"},
        principal_id="p",
        scopes=h.SCOPES,
    )
    answer = LinguisticsQueries(conn).lookup_lexeme(
        NS, "tamo", "nort3456", scopes=h.SCOPES
    )
    noun = next(
        lx
        for lx in answer["lexemes"]
        if lx["record_key"] == "lexeme:wikidata-lexemes:L90001"
    )
    definition = noun["senses"][0]["definitions"][0]
    assert (
        definition["text"] != "Fluss"
        and definition["noesis_translations"][0]["status"] == "unreviewed"
    )
    assert "not a sourced definition" in definition["noesis_translations"][0]["label"]


def test_exports_that_would_break_share_alike_are_refused(conn):
    queries = LinguisticsQueries(conn)
    with pytest.raises(LinguisticsError) as caught:
        export_lexeme_dossier(
            queries, NS, "tamo", "nort3456", target_licence="CC-BY-4.0", scopes=h.SCOPES
        )
    assert caught.value.code == "export_refused"
    with pytest.raises(LinguisticsError):
        export_lexeme_dossier(
            queries,
            NS,
            "tamo",
            "nort3456",
            target_licence="CC-BY-SA-4.0",
            scopes=h.SCOPES,
            keep_attribution=False,
        )
    shared = export_lexeme_dossier(
        queries, NS, "tamo", "nort3456", target_licence="CC-BY-SA-4.0", scopes=h.SCOPES
    )
    assert (
        shared["licences"]["share_alike"] is True and shared["attribution_kept"] is True
    )
    stripped = export_lexeme_dossier(
        queries,
        NS,
        "tamo",
        "nort3456",
        target_licence="CC-BY-4.0",
        scopes=h.SCOPES,
        exclude_share_alike=True,
    )
    assert (
        stripped["excluded_share_alike"]
        and stripped["licences"]["share_alike"] is False
    )
    assert all(lx["provider"] == "wikidata-lexemes" for lx in stripped["lexemes"])


def test_reads_are_not_ready_before_any_source_ran_and_scoped():
    queries = LinguisticsQueries(h.connect())
    with pytest.raises(LinguisticsError) as caught:
        queries.lookup_lexeme(NS, "tamo", scopes=h.SCOPES)
    assert caught.value.code == "not_ready"
    with pytest.raises(LinguisticsError) as caught:
        queries.lookup_lexeme(NS, "tamo", scopes={"knowledge:linguistics:read"})
    assert caught.value.code == "unauthorized"
