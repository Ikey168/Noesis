"""Offline word-to-lexeme-dossier acceptance for the Linguistics pack (LG12, #2190).

The journey runs on authored fixtures only (a fictional language family,
synthetic identifiers; see ``tests/unit/linguistics_harness.py``). Sockets are
blocked, so no network is touched:

ingestion (two Wiktextract extract releases, two Glottolog releases, Wikidata
revisions, WALS, CLDR and ISO 639-3 tables) -> languoid and lexeme identity
review -> etymology chain -> as-of sense and definition queries -> typological
profile -> cross-language links through ``src/kb/cross_language.py`` -> monitor
events.
"""

from __future__ import annotations

import socket

import pytest

from src.domains import registry as domain_registry
from src.kb.linguistics_crosslang import CrossLanguageLinks
from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_monitoring import LinguisticsMonitor
from src.kb.linguistics_queries import LinguisticsQueries, export_lexeme_dossier
from src.kb.linguistics_records import LinguisticsError
from src.kb.linguistics_store import LinguisticsStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit import linguistics_harness as h

NS = h.NS
TAMO_WD, TAMO_WT = (
    "lexeme:wikidata-lexemes:L90001",
    "lexeme:kaikki-wiktextract:qnv:tamo:noun:1",
)
TAMO_VERB_WD, TAMO_VERB_WT = (
    "lexeme:wikidata-lexemes:L90003",
    "lexeme:kaikki-wiktextract:qnv:tamo:verb:2",
)
TAMU_WD, TAMU_WT = (
    "lexeme:wikidata-lexemes:L90002",
    "lexeme:kaikki-wiktextract:qsv:tamu:noun:0",
)
SENSE_WT = "sense:kaikki-wiktextract:qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2"


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the offline journey must not open a socket")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_word_to_cited_lexeme_dossier_offline():
    conn = h.connect()
    subscriptions = SubscriptionStore(conn)
    store = LinguisticsStore(conn)

    # 1. ingestion of the first releases, and monitors set up before the second arrive
    h.load_all(conn, 1)
    subscriptions.commit_watermark(NS, 1, kind="ingestion")
    snapshot_before = store.snapshot(NS)["snapshot_id"]
    again = h.run(conn, "kaikki", 1)
    assert all(
        page["stored"]["duplicate"] == page["stored"]["records"] for page in again
    )  # adds nothing
    assert store.snapshot(NS)["snapshot_id"] == snapshot_before
    monitor = LinguisticsMonitor(conn, now=lambda: 20_000)
    watches = {
        name: monitor.create(
            NS, name, watch=kind, target=target, principal_id="alice", scopes=h.SCOPES
        )["subscription_id"]
        for name, kind, target in (
            ("sense", "sense", SENSE_WT),
            ("dialect", "languoid", "coas1122"),
            ("order", "wals-parameter", "81A"),
            ("verb", "lexeme", TAMO_VERB_WD),
        )
    }
    for subscription_id in watches.values():
        assert (
            monitor.run(subscription_id, principal_id="alice", scopes=h.SCOPES)[
                "baseline"
            ]
            is True
        )

    # 2. languoid identity: stated codes resolve; the macrolanguage and the retired code are reviewed
    identity = LinguisticsIdentity(conn, now=lambda: 30_000)
    assert (
        identity.resolve_languoid(NS, "wikidata-item", "Q900001")["glottocode"]
        == "nort3456"
    )
    assert (
        identity.resolve_languoid(NS, "wiktionary-code", "qsv")["glottocode"]
        == "sout7890"
    )
    languoids = identity.propose_languoid_matches(
        NS, principal_id="alice", scopes=h.SCOPES
    )
    assert {c["state"] for c in languoids["candidates"]} == {"proposed"}
    retired = next(
        c
        for c in languoids["candidates"]
        if c["left_key"] == "language-ref:iso639-3:qrv"
        and c["right_key"] == "languoid:glottolog:nort3456"
    )
    identity.review(
        NS,
        retired["candidate_id"],
        "accept",
        "SIL split remedy names Northern Velan",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    assert (
        identity.record_iso_events(NS, principal_id="alice", scopes=h.SCOPES)[0][
            "decision_type"
        ]
        == "split"
    )

    # 3. lexeme identity: the Wikidata and Wiktionary entries are proposed, the homographs never paired
    lexemes = identity.propose_lexeme_matches(NS, principal_id="alice", scopes=h.SCOPES)
    pairs = {(c["left_key"], c["right_key"]): c for c in lexemes["candidates"]}
    assert set(pairs) == {
        (TAMO_WT, TAMO_WD),
        (TAMO_VERB_WT, TAMO_VERB_WD),
        (TAMU_WT, TAMU_WD),
    }
    for candidate in pairs.values():
        identity.review(
            NS,
            candidate["candidate_id"],
            "accept",
            "same word, same sense item",
            principal_id="reviewer",
            scopes=h.SCOPES,
        )
    assert identity.equivalents(NS, TAMO_WD) == sorted([TAMO_WD, TAMO_WT])
    assert TAMO_VERB_WD not in identity.equivalents(
        NS, TAMO_WD
    )  # the homograph stays separate
    identity.project_locations(
        NS, geo_namespace="global", principal_id="alice", scopes=h.SCOPES
    )

    # 4. the second releases: a definition revision, a reclassified dialect, a changed WALS value, a deletion
    h.load_all(conn, 2, keys=("wikidata", "kaikki", "glottolog", "wals"))
    subscriptions.commit_watermark(NS, 2, kind="ingestion")
    assert store.snapshot(NS)["snapshot_id"] != snapshot_before

    # 5. the etymology chain: borrowed (Wikidata) vs inherited (Wiktionary) side by side, every link cited
    queries = LinguisticsQueries(conn)
    etymology = queries.lexeme_etymology(NS, TAMO_WD, scopes=h.SCOPES)
    assert etymology["conflicts"] and all(
        link["citation"]["attribution"] for link in etymology["links"]
    )
    top = {link["relation"]: link for link in etymology["links"] if link["depth"] == 0}
    assert (
        top["borrowed"]["target"]["status"] == "resolved"
        and top["borrowed"]["reference_status"] == "referenced"
    )
    assert (
        top["inherited"]["target"]["cited"]["text"] == "*tamə"
    )  # the proto-form stays cited text
    borrowed = queries.lexeme_etymology(
        NS, "lexeme:kaikki-wiktextract:qnv:banka:noun:0", scopes=h.SCOPES
    )
    assert (
        borrowed["links"][0]["relation"] == "borrowed"
        and borrowed["links"][0]["target"]["cited"]["text"] == "banca"
    )
    assert etymology["unknowns"]

    # 6. as-of sense and definition queries across the two extract releases
    assert [
        d["text"]
        for d in queries.sense_history(NS, SENSE_WT, "2026-04-01", scopes=h.SCOPES)[
            "definitions"
        ]
    ] == ["a river"]
    assert [
        d["text"]
        for d in queries.sense_history(NS, SENSE_WT, "2026-07-01", scopes=h.SCOPES)[
            "definitions"
        ]
    ] == ["a large river; a stream"]
    changes = queries.definition_changes(NS, SENSE_WT, scopes=h.SCOPES)
    assert [r["source_revision"] for r in changes["changes"][0]["revisions"]] == [
        "2026-03-01/2026-02-20",
        "2026-06-01/2026-05-20",
    ]
    word = queries.lookup_lexeme(NS, "tamo", "nort3456", scopes=h.SCOPES)
    by_key = {lexeme["record_key"]: lexeme for lexeme in word["lexemes"]}
    assert set(by_key) == {TAMO_WD, TAMO_WT, TAMO_VERB_WD, TAMO_VERB_WT}
    assert by_key[TAMO_WD]["equivalent_lexemes"] == sorted([TAMO_WD, TAMO_WT])
    assert by_key[TAMO_VERB_WD]["status"] == "deleted"
    assert set(by_key[TAMO_WD]["paradigm"]) == {"Q110786", "Q146786"}
    in_south = queries.lookup_lexeme(NS, "tamu", "qsv", scopes=h.SCOPES)
    assert {lexeme["record_key"] for lexeme in in_south["lexemes"]} == {
        TAMU_WD,
        TAMU_WT,
    }  # the second language

    # 7. typological profile cited to WALS, the dialect and its reclassification, unknowns visible
    north = queries.languoid_profile(NS, "qnv", scopes=h.SCOPES)
    assert (
        north["identifiers"] == {"glottocode": "nort3456", "iso639_3": "qnv"}
        and north["dialects"] == []
    )
    values = {v["parameter"]: v for v in north["typology"]["values"]}
    assert (
        values["81A"]["value"] == "SOV"
        and values["81A"]["references"]
        and values["81A"]["citation"]["licence"] == "CC-BY-4.0"
    )
    assert north["location"]["places"][0]["state"] == "proposed"
    dialect = queries.languoid_profile(NS, "coas1122", scopes=h.SCOPES)
    assert dialect["level"] == "dialect" and [
        c["parent"] for c in dialect["classification_history"]
    ] == ["nort3456", "sout7890"]
    south = queries.languoid_profile(NS, "sout7890", scopes=h.SCOPES)
    assert (
        "coas1122" in south["dialects"]
        and "WALS 87A: no value on record" in south["unknowns"]
    )
    assert (
        queries.lookup_lexeme(NS, "tamo", "qmv", scopes=h.SCOPES)["status"]
        == "ambiguous"
    )

    # 8. cross-language links through the existing multilingual records
    links = CrossLanguageLinks(conn, initialize=True)
    linked = links.links(NS, TAMO_WD, scopes=h.SCOPES)
    assert {link["kind"] for link in linked["links"]} == {
        "sourced-translation",
        "shared-sense-item",
    }
    assert links.index(NS, principal_id="alice", scopes=h.SCOPES)["written"] > 0
    alias = links.propose_alias(
        NS, TAMO_WD, "entity:river", "Q4022", principal_id="alice", scopes=h.SCOPES
    )
    assert alias["status"] == "candidate"
    translation = links.record_translation(
        NS,
        "definition_revision:wikidata-lexemes:L90001-S1:en",
        "de",
        "Fluss",
        {"name": "fixture-mt", "kind": "machine"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert translation["status"] == "unreviewed"
    word = queries.lookup_lexeme(NS, "tamo", "nort3456", scopes=h.SCOPES)
    definitions = [
        d
        for lexeme in word["lexemes"]
        for sense in lexeme["senses"]
        for d in sense["definitions"]
    ]
    assert "Fluss" not in {
        d["text"] for d in definitions
    }  # no machine translation as a sourced definition
    assert all(d["origin"] == "source-published" for d in definitions)
    assert any(
        t["status"] == "unreviewed"
        for d in definitions
        for t in d["noesis_translations"]
    )
    hits = links.search(NS, "stream", scopes=h.SCOPES)["results"]
    assert any(hit.get("object_type") == "linguistics-definition" for hit in hits)

    # 9. CC BY-SA attribution travels with Wiktionary-derived output and exports respect it
    assert word["licences"]["share_alike"] is True
    wiktionary = by_key[TAMO_WT]
    assert (
        wiktionary["citation"]["share_alike"]
        and "Wiktionary contributors" in wiktionary["citation"]["attribution"]
    )
    with pytest.raises(LinguisticsError):
        export_lexeme_dossier(
            queries, NS, "tamo", "nort3456", target_licence="CC0-1.0", scopes=h.SCOPES
        )
    assert (
        export_lexeme_dossier(
            queries,
            NS,
            "tamo",
            "nort3456",
            target_licence="CC-BY-SA-4.0",
            scopes=h.SCOPES,
        )["licences"]["share_alike"]
        is True
    )

    # 10. monitor events cite the old and new revision or release
    events = {
        name: sorted(
            n["kind"]
            for n in monitor.run(s, principal_id="alice", scopes=h.SCOPES)[
                "notifications"
            ]
        )
        for name, s in watches.items()
    }
    assert events == {
        "sense": ["definition_revised"],
        "dialect": ["classification_changed"],
        "order": ["feature_value_changed"],
        "verb": ["sense_removed"],
    }
