"""Definition, etymology, classification and feature-value monitors through subscriptions (LG10, #2188)."""

from __future__ import annotations

import pytest

from src.kb.linguistics_monitoring import EVENTS, LinguisticsMonitor
from src.kb.linguistics_records import LinguisticsError
from src.kb.linguistics_store import LinguisticsStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit import linguistics_harness as h

NS = h.NS


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


@pytest.fixture()
def setup():
    conn = h.connect()
    h.load_all(conn, 1)
    subscriptions = SubscriptionStore(conn)
    subscriptions.commit_watermark(NS, 1, kind="ingestion")
    monitor = LinguisticsMonitor(conn, now=lambda: 10_000)
    return conn, monitor, subscriptions


def watch(monitor, watch_kind, target):
    return monitor.create(
        NS,
        f"{watch_kind}:{target}",
        watch=watch_kind,
        target=target,
        principal_id="alice",
        scopes=h.SCOPES,
    )["subscription_id"]


def test_two_releases_produce_cited_events(setup):
    conn, monitor, subscriptions = setup
    lexeme = watch(monitor, "lexeme", "lexeme:wikidata-lexemes:L90001")
    verb = watch(monitor, "lexeme", "lexeme:wikidata-lexemes:L90003")
    sense = watch(
        monitor,
        "sense",
        "sense:kaikki-wiktextract:qnv:tamo:noun:1#sense:en-tamo-qnv-noun-Ab1Cd2",
    )
    languoid = watch(monitor, "languoid", "coas1122")
    parameter = watch(monitor, "wals-parameter", "81A")
    for subscription_id in (lexeme, verb, sense, languoid, parameter):
        baseline = monitor.run(subscription_id, principal_id="alice", scopes=h.SCOPES)
        assert baseline["baseline"] is True and {
            n["kind"] for n in baseline["notifications"]
        } <= {"in_view"}
    h.load_all(conn, 2, keys=("wikidata", "kaikki", "glottolog", "wals"))
    subscriptions.commit_watermark(NS, 2, kind="ingestion")
    revised = monitor.run(lexeme, principal_id="alice", scopes=h.SCOPES)
    assert kinds(revised) == ["definition_revised"]
    cites = revised["notifications"][0]["cites"]
    assert (
        cites["previous"]["source_revision"] == "2100000001"
        and cites["current"]["source_revision"] == "2100000077"
    )
    assert kinds(monitor.run(verb, principal_id="alice", scopes=h.SCOPES)) == [
        "sense_removed"
    ]
    sense_events = monitor.run(sense, principal_id="alice", scopes=h.SCOPES)
    assert kinds(sense_events) == ["definition_revised"]
    assert (
        sense_events["notifications"][0]["cites"]["current"]["revision_date"]
        == "2026-06-01"
    )
    moved = monitor.run(languoid, principal_id="alice", scopes=h.SCOPES)
    assert kinds(moved) == ["classification_changed"]
    assert moved["notifications"][0]["cites"] == {
        "previous": {
            "revision_id": moved["notifications"][0]["cites"]["previous"][
                "revision_id"
            ],
            "release": "v5.0",
        },
        "current": {
            "revision_id": moved["notifications"][0]["cites"]["current"]["revision_id"],
            "release": "v5.1",
        },
    }
    assert kinds(monitor.run(parameter, principal_id="alice", scopes=h.SCOPES)) == [
        "feature_value_changed"
    ]
    # replaying the watermark, or a refresh that changed nothing, raises nothing new
    assert (
        monitor.run(lexeme, 2, principal_id="alice", scopes=h.SCOPES)["status"]
        == "replayed"
    )
    h.load_all(conn, 2, keys=("wikidata",))
    subscriptions.commit_watermark(NS, 3, kind="ingestion")
    assert (
        monitor.run(lexeme, principal_id="alice", scopes=h.SCOPES)["notifications"]
        == []
    )
    polled = monitor.poll(lexeme, principal_id="alice", scopes=h.SCOPES)
    assert polled["events"]


def test_etymology_sense_added_and_iso_events(setup):
    conn, monitor, subscriptions = setup
    lexeme = watch(monitor, "lexeme", "lexeme:wikidata-lexemes:L90002")
    languoid = watch(monitor, "languoid", "nort3456")
    monitor.run(lexeme, principal_id="alice", scopes=h.SCOPES)
    monitor.run(languoid, principal_id="alice", scopes=h.SCOPES)
    store = LinguisticsStore(conn)
    licence = {"id": "CC0-1.0", "attribution": "Wikidata, CC0", "share_alike": False}
    base = {"contract": "noesis-linguistic-record-v1", "provider": "wikidata-lexemes"}
    source = {
        "revision": "2100000100",
        "revision_date": "2026-08-01T00:00:00Z",
        "licence": licence,
    }
    store.apply(
        NS,
        [
            {
                **base,
                "kind": "lexeme",
                "source": {**source, "source_id": "L90002"},
                "body": store.current(NS, "lexeme:wikidata-lexemes:L90002")["body"],
            },
            {
                **base,
                "kind": "sense",
                "source": {**source, "source_id": "L90002-S1"},
                "body": store.current(NS, "sense:wikidata-lexemes:L90002-S1")["body"],
            },
            {
                **base,
                "kind": "definition_revision",
                "source": {**source, "source_id": "L90002-S1:en"},
                "body": store.current(
                    NS, "definition_revision:wikidata-lexemes:L90002-S1:en"
                )["body"],
            },
            {
                **base,
                "kind": "sense",
                "source": {**source, "source_id": "L90002-S2"},
                "body": {
                    "lexeme": "lexeme:wikidata-lexemes:L90002",
                    "identity_basis": "source-id",
                },
            },
            {
                **base,
                "kind": "etymology_assertion",
                "source": {**source, "source_id": "L90002$etym-3"},
                "body": {
                    "lexeme": "lexeme:wikidata-lexemes:L90002",
                    "relation": "derived",
                    "native_relation": "P5191",
                    "target": {
                        "source_lexeme_id": "L90006",
                        "provider": "wikidata-lexemes",
                    },
                    "references": [],
                    "reference_status": "unreferenced",
                },
            },
        ],
        run_id="manual",
        observed_at_ms=h.ms("2026-08-02"),
    )
    iso = {
        "contract": "noesis-linguistic-record-v1",
        "provider": "sil-iso639-3",
        "kind": "iso_code_change",
        "source": {
            "source_id": "qnv:2026-08-01",
            "revision": "2026-08-01",
            "revision_date": "2026-08-01",
            "licence": {"id": "SIL", "attribution": "SIL", "share_alike": False},
        },
        "body": {
            "code": "qnv",
            "name": "Northern Velan",
            "reason": "change",
            "reason_code": "C",
            "change_to": "qnw",
            "effective": "2026-08-01",
        },
    }
    store.apply(NS, [iso], run_id="manual-iso", observed_at_ms=h.ms("2026-08-02"))
    subscriptions.commit_watermark(NS, 2, kind="ingestion")
    assert kinds(monitor.run(lexeme, principal_id="alice", scopes=h.SCOPES)) == [
        "etymology_changed",
        "sense_added",
    ]
    assert kinds(monitor.run(languoid, principal_id="alice", scopes=h.SCOPES)) == [
        "iso_code_changed"
    ]


def test_late_older_release_raises_no_false_event(setup):
    conn, monitor, subscriptions = setup
    languoid = watch(monitor, "languoid", "coas1122")
    h.load_all(conn, 2, keys=("glottolog",))
    subscriptions.commit_watermark(NS, 2, kind="ingestion")
    monitor.run(languoid, principal_id="alice", scopes=h.SCOPES)
    h.run(
        conn, "glottolog", 1, observed_at_ms=h.ms("2026-08-01")
    )  # the older release arrives late
    subscriptions.commit_watermark(NS, 3, kind="ingestion")
    assert (
        monitor.run(languoid, principal_id="alice", scopes=h.SCOPES)["notifications"]
        == []
    )


def test_watch_validation_and_not_ready():
    monitor = LinguisticsMonitor(h.connect())
    with pytest.raises(LinguisticsError) as caught:
        monitor.create(
            NS,
            "x",
            watch="lexeme",
            target="lexeme:wikidata-lexemes:L1",
            principal_id="a",
            scopes=h.SCOPES,
        )
    assert caught.value.code == "not_ready"
    with pytest.raises(LinguisticsError) as caught:
        monitor.poll("knowledge-subscription:none", principal_id="a", scopes=h.SCOPES)
    assert caught.value.code == "not_ready"
    assert set(EVENTS) == {
        "definition_revised",
        "sense_added",
        "sense_removed",
        "etymology_changed",
        "classification_changed",
        "iso_code_changed",
        "feature_value_changed",
    }


def test_invalid_watch_targets(setup):
    _, monitor, _ = setup
    for kind_, target in (
        ("languoid", "BAD"),
        ("wals-parameter", "x"),
        ("lexeme", "lexeme:x:y"),
        ("word", "x"),
    ):
        with pytest.raises(LinguisticsError):
            monitor.create(
                NS,
                f"bad-{kind_}",
                watch=kind_,
                target=target,
                principal_id="a",
                scopes=h.SCOPES,
            )
