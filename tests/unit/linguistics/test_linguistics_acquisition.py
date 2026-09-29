"""Wikidata lexemes, Wiktextract, Glottolog, WALS, CLDR and ISO 639-3 acquisition (LG03-LG05, #2181-#2183)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.linguistics_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    LinguisticsAdapter,
    fixture_transport,
    linguistics_declaration,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.linguistics_store import LinguisticsStore
from tests.unit import linguistics_harness as h


def records(results, kind=None):
    out = [
        r["linguistic_record"]
        for page in results
        for r in page["records"]
        if r.get("linguistic_record")
    ]
    return [r for r in out if kind is None or r["kind"] == kind]


@pytest.fixture()
def conn():
    return h.connect()


def test_every_implemented_source_is_unverified_live_and_decisions_are_recorded():
    implemented = {
        "wikidata-lexemes",
        "kaikki-wiktextract",
        "glottolog",
        "wals",
        "cldr",
        "sil-iso639-3",
    }
    assert {
        p
        for p, c in PROVIDER_CONTRACTS.items()
        if c["access_decision"] == "unverified-live"
    } == implemented
    assert (
        PROVIDER_CONTRACTS["leipzig-glossing-rules"]["access_decision"] == "link-only"
    )
    assert (
        PROVIDER_CONTRACTS["speaker-recordings"]["access_decision"] == "not-implemented"
    )
    assert all(LIVE_VERIFICATION[p]["status"] == "unverified-live" for p in implemented)


def test_wikidata_lexemes_forms_senses_and_cited_etymologies(conn):
    results = h.run(conn, "wikidata")
    lexemes = {r["source"]["source_id"]: r for r in records(results, "lexeme")}
    noun = lexemes["L90001"]
    assert (
        noun["source"]["revision"] == "2100000001"
        and noun["source"]["revision_date"] == "2026-02-01T10:00:00Z"
    )
    assert (
        noun["body"]["lemma"]["text"] == "tamo"
        and noun["body"]["lexical_category"]["value"] == "Q1084"
    )
    assert (
        noun["source"]["licence"]["id"] == "CC0-1.0"
        and noun["source"]["licence"]["share_alike"] is False
    )
    forms = {r["source"]["source_id"]: r for r in records(results, "form")}
    assert forms["L90001-F2"]["body"]["grammatical_features"] == [
        {"scheme": "wikidata-item", "value": "Q146786"}
    ]
    sense = next(
        r for r in records(results, "sense") if r["source"]["source_id"] == "L90001-S1"
    )
    assert sense["body"]["item_links"] == [
        {"scheme": "wikidata-item", "value": "Q4022", "stated_by": "P5137"}
    ]
    etymologies = {
        r["source"]["source_id"]: r["body"]
        for r in records(results, "etymology_assertion")
    }
    borrowed = etymologies["L90001$etym-1"]
    assert (
        borrowed["relation"] == "borrowed"
        and borrowed["native_relation"] == "P5191/P5886:Q900101"
    )
    assert (
        borrowed["reference_status"] == "referenced"
        and borrowed["references"][0]["doi"] == "10.5555/velan.1998.12"
    )
    assert etymologies["L90002$etym-2"]["reference_status"] == "unreferenced"
    items = {
        r["source"]["source_id"]: r["body"] for r in records(results, "language_item")
    }
    assert (
        items["Q900001"]["glottocodes"][0]["value"] == "nort3456"
        and items["Q900001"]["iso639_3"][0]["value"] == "qnv"
    )
    # canonical entities are never touched
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert not {"entities", "entity_history_identities", "kg_entities"} & tables


def test_wikidata_revision_appends_and_deletion_is_recorded_not_removed(conn):
    h.run(conn, "wikidata", 1)
    h.run(conn, "wikidata", 2)
    store = LinguisticsStore(conn)
    history = store.history(h.NS, "definition_revision:wikidata-lexemes:L90001-S1:en")
    assert [r["body"]["text"] for r in history] == ["river", "river; a large stream"]
    assert [r["source_revision"] for r in history] == ["2100000001", "2100000077"]
    deleted = store.current(h.NS, "lexeme:wikidata-lexemes:L90003")
    assert deleted["body"]["status"] == "deleted"
    assert (
        store.history(h.NS, "lexeme:wikidata-lexemes:L90003")[0]["body"]["lemma"][
            "text"
        ]
        == "tamo"
    )
    assert (
        store.current(h.NS, "form:wikidata-lexemes:L90003-F1") is not None
    )  # earlier records stay


def test_wiktextract_entries_carry_share_alike_and_drop_speaker_data(conn):
    results = h.run(conn, "kaikki")
    everything = records(results)
    assert everything and all(
        r["source"]["licence"]["share_alike"] is True for r in everything
    )
    assert all(
        "Wiktionary contributors" in r["source"]["licence"]["attribution"]
        for r in everything
    )
    assert all(r["source"]["revision"] == "2026-03-01/2026-02-20" for r in everything)
    qnv = results[0]["receipt"]["counts"]
    assert qnv["dropped_speaker_data"] == 2 and qnv["out_of_scope"] == 1
    assert "sounds" not in json.dumps(everything) and "Qnv-tamo.ogg" not in json.dumps(
        everything
    )
    lexemes = {r["source"]["source_id"] for r in records(results, "lexeme")}
    assert {
        "qnv:tamo:noun:1",
        "qnv:tamo:verb:2",
        "qnv:banka:noun:0",
        "qsv:tamu:noun:0",
        "en:river:noun:0",
    } == lexemes
    assert [
        r["body"]["representations"][0]["text"] for r in records(results, "form")
    ] == ["tamoi"]
    note = next(
        r for r in records(results, "etymology_note") if "tamo" in r["body"]["lexeme"]
    )
    assert (
        note["body"]["text"]
        == "Inherited from Proto-Velan *tamə. Cognate with Southern Velan tamu."
    )
    relations = sorted(
        (r["body"]["native_relation"], r["body"]["relation"])
        for r in records(results, "etymology_assertion")
    )
    assert relations == [
        ("bor", "borrowed"),
        ("cog", "cognate"),
        ("inh", "inherited"),
        ("inh", "inherited"),
    ]
    gloss = records(results, "gloss")[0]["body"]
    assert gloss["validation"] == "validated" and gloss["categories"] == ["PL", "PRS"]
    example = records(results, "usage_example")[0]["body"]
    assert example["published_translation"] == {
        "text": "The rivers are wide.",
        "origin": "source-published",
    }
    translations = records(results, "sourced_translation")
    assert {t["body"]["resolved_to"] for t in translations} == {"sense"}
    senses = {r["source"]["source_id"]: r["body"] for r in records(results, "sense")}
    assert (
        senses["qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2"]["identity_basis"]
        == "source-id"
    )


def test_a_later_extract_appends_a_definition_revision(conn):
    h.run(conn, "kaikki", 1)
    second = h.run(conn, "kaikki", 2)
    assert second[0]["stored"]["revised"] == 1
    again = h.run(conn, "kaikki", 2)
    assert (
        again[0]["stored"]["duplicate"] == again[0]["stored"]["records"]
    )  # re-acquisition adds nothing
    key = "definition_revision:kaikki-wiktextract:qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2:en"
    history = LinguisticsStore(conn).history(h.NS, key)
    assert [r["body"]["text"] for r in history] == [
        "a river",
        "a large river; a stream",
    ]
    assert [r["revision_date"] for r in history] == ["2026-03-01", "2026-06-01"]


def test_unparseable_lines_are_rejections_and_entries_never_inherit(conn):
    source, pages = h.staged("kaikki", 1)
    pages[0]["body"] = (
        "not json\n"
        + json.dumps({"word": "tamo", "lang_code": "qnv"})
        + "\n"
        + json.dumps(
            {
                "word": "banka",
                "lang_code": "qnv",
                "pos": "noun",
                "senses": [{"glosses": ["bank"]}],
            }
        )
        + "\n"
    )
    adapter = LinguisticsAdapter(source, transport=fixture_transport(pages))
    page = adapter.fetch_page(
        {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
    )
    rejected = [r for r in page.records if r.get("rejection")]
    assert [r["rejection"]["code"] for r in rejected] == [
        "invalid_line",
        "invalid_entry",
    ]
    banka = [r["linguistic_record"] for r in page.records if r.get("linguistic_record")]
    assert all(
        "etymology" not in r["kind"] for r in banka
    )  # nothing carried over from another entry
    sense = next(r for r in banka if r["kind"] == "sense")
    assert sense["body"]["identity_basis"] == "position"


def test_glottolog_release_change_appends_a_classification_revision(conn):
    first = h.run(conn, "glottolog", 1)
    languoids = {r["body"]["glottocode"]: r["body"] for r in records(first, "languoid")}
    assert set(languoids) == {"velt1234", "nort3456", "sout7890", "coas1122"}
    assert (
        languoids["coas1122"]["level"] == "dialect"
        and languoids["coas1122"]["parent"] == "nort3456"
    )
    assert (
        languoids["nort3456"]["iso639_3"] == "qnv"
        and languoids["nort3456"]["coordinates"]["latitude"] == "61.5"
    )
    assert (
        "coordinates" not in languoids["velt1234"]
        and "iso639_3" not in languoids["velt1234"]
    )
    assert "threatened" not in json.dumps(
        records(first)
    )  # the endangerment parameter is never read
    h.run(conn, "glottolog", 2)
    history = LinguisticsStore(conn).history(h.NS, "languoid:glottolog:coas1122")
    assert [(r["source_revision"], r["body"]["parent"]) for r in history] == [
        ("v5.0", "nort3456"),
        ("v5.1", "sout7890"),
    ]


def test_wals_values_keep_references_and_the_published_glottocode_mapping(conn):
    results = h.run(conn, "wals")
    values = {
        r["source"]["source_id"]: r["body"]
        for r in records(results, "typological_value")
    }
    assert (
        values["nve:81A"]["value"] == "SOV"
        and values["nve:81A"]["glottocode"] == "nort3456"
    )
    assert values["nve:81A"]["references"] == [
        {"key": "Fictiva-2019", "pages": "12-14"}
    ]
    assert values["sve:81A"]["references"] == [
        {"key": "Fictiva-2019", "pages": "15"},
        {"key": "Imaginus-2021"},
    ]
    assert "sve:87A" not in values and "nve:1A" not in values
    parameter = next(
        r["body"]
        for r in records(results, "typological_parameter")
        if r["body"]["parameter"] == "81A"
    )
    assert [c["name"] for c in parameter["codes"]] == [
        "SOV",
        "SVO",
        "No dominant order",
    ]
    h.run(conn, "wals", 2)
    history = LinguisticsStore(conn).history(h.NS, "typological_value:wals:sve:81A")
    assert [r["body"]["value"] for r in history] == ["SVO", "No dominant order"]


def test_cldr_locale_data_and_release_mismatch(conn):
    results = h.run(conn, "cldr")
    data = {
        r["source"]["source_id"]: r["body"]["value"]
        for r in records(results, "locale_data")
    }
    assert (
        data["de:language_display_name:en"] == "Englisch"
        and data["de:script_display_name:Cyrl"] == "Kyrillisch"
    )
    assert data["en:plural_rules:en"]["one"] == "i = 1 and v = 0 @integer 1"
    assert "de:language_display_name:fr" not in data
    source, pages = h.staged("cldr", 1)
    body = copy.deepcopy(pages[0]["body"])
    body["main"]["de"]["identity"]["version"]["_cldrVersion"] = "44"
    pages[0]["body"] = body
    with pytest.raises(SourcePackError) as caught:
        LinguisticsAdapter(source, transport=fixture_transport(pages)).fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 100}, cursor=None
        )
    assert caught.value.code == "schema_drift"


def test_iso_tables_macrolanguages_and_retirements(conn):
    results = h.run(conn, "iso")
    codes = {r["body"]["code"]: r["body"] for r in records(results, "iso_code")}
    assert (
        codes["qmv"]["scope"] == "macrolanguage"
        and codes["qnv"]["scope"] == "individual"
    )
    members = sorted(r["body"]["member"] for r in records(results, "iso_macrolanguage"))
    assert members == ["qnv", "qsv"]
    change = records(results, "iso_code_change")[0]["body"]
    assert (
        change["reason"] == "split"
        and change["split_into"] == ["qnv", "qsv"]
        and change["effective"] == "2024-01-15"
    )


def test_declarations_are_bounded_and_same_host():
    source = h.production("glottolog")
    bad = copy.deepcopy(source)
    bad["linguistics"]["documents"][0]["values_url"] = "https://example.org/values.csv"
    with pytest.raises(SourcePackError):
        linguistics_declaration(bad)
    bad = copy.deepcopy(source)
    bad["linguistics"]["format"] = "wals-cldf"
    with pytest.raises(SourcePackError):
        linguistics_declaration(bad)
    source, pages = h.staged("glottolog", 1)
    adapter = LinguisticsAdapter(source, transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "documents", "parameters": {"q": "x"}}, cursor=None
        )
    assert caught.value.code == "parameter_forbidden"
