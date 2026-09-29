"""Offline acceptance: a legislative dossier to a cited set of declared interests (#2018, T11).

The pinned ``official-political-records`` fixtures for the five register
sources run through the real source-pack runtime; the later EU Transparency
Register export replays through the same adapter and projector. The journey
builds EU and German dossiers, matches one registrant through review, links
declarations by an explicit register field and by a reviewed assertion,
answers ``list_dossier_declared_interests`` with every row citing a register
revision, watches a registrant through a subscription across a restart, and
checks idempotent re-ingestion, a spend-range revision, a deregistration, an
unmatched registrant and a conflicting client list kept side by side. Nothing
touches the network and every party is fictional: offline fixture evidence,
never live coverage.
"""

from __future__ import annotations

import socket

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.kb.lobbying import LobbyingStore, feature_enabled, forbidden_keys
from src.kb.lobbying_identity import LobbyingIdentity
from src.kb.lobbying_links import LobbyingDossierLinks
from src.kb.lobbying_monitoring import LobbyingMonitor
from src.kb.lobbying_queries import LobbyingQueries
from src.kb.subscriptions import SubscriptionStore
from tests.unit import lobbying_harness as h

PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - preflight resolver; no request is ever sent
REGISTER_TABLES = (
    "lobbying_exports",
    "lobbying_entries",
    "lobbying_revisions",
    "lobbying_export_members",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "the acceptance journey must not open a network connection"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


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


def registers(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in REGISTER_TABLES
    }


def run_pack(conn, key):
    value = h.manifest()
    store = SourcePackStore(conn)
    if not conn.execute(
        "SELECT 1 FROM source_pack_versions WHERE pack_id=?", [value["pack_id"]]
    ).fetchone():
        store.install(value, principal_id="operator", enable=True, now_ms=10)
    clock = iter(range(2_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for source_id in h.SOURCES.values():
        runtime.accept_license(value["pack_id"], source_id, principal_id="operator")
    return runtime.run(
        {
            "pack_id": value["pack_id"],
            "run_key": key,
            "operation": "export",
            "source_ids": sorted(h.SOURCES.values()),
            "max_results": 1000,
            "max_bytes": 100_000_000,
            "timeout_ms": 120_000,
        },
        principal_id="operator",
        adapters=runtime.fixture_adapters(value["pack_id"], h.ROOT),
        dns_resolver=PUBLIC_DNS,
    )


def test_dossier_to_cited_declared_interests(tmp_path):
    path = str(tmp_path / "lobbying-acceptance.duckdb")
    conn = duckdb.connect(path)
    receipt = run_pack(conn, "first")
    assert receipt["status"] == "complete", receipt
    store = LobbyingStore(conn)
    assert {e["register"] for e in store.entries("global")} == set(h.SOURCES)
    exports = conn.execute(
        "SELECT DISTINCT evidence_origin FROM lobbying_exports"
    ).fetchall()
    assert exports == [
        ("fixture",)
    ]  # offline fixture evidence, reported apart from any live evidence

    # A registrant monitor takes its baseline before the next EU export.
    monitor = LobbyingMonitor(conn)
    watch = monitor.create(
        "global",
        "assoc",
        watch="registrant",
        key=h.entry_id(conn, h.EU_ASSOC),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    SubscriptionStore(conn).commit_watermark("global", 1)
    assert [
        n["kind"]
        for n in monitor.run(
            watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
        )["notifications"]
    ] == ["registration"]

    # Idempotent re-ingestion: a second runtime run over the same pinned files adds nothing.
    before = registers(conn)
    assert run_pack(conn, "second")["status"] == "complete"
    assert registers(conn)["lobbying_revisions"] == before["lobbying_revisions"]

    later = h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    assert (later["amended"], later["deregistered"]) == (2, 1)

    # Dossiers, explicit-field links and one reviewed assertion.
    dossiers = h.dossiers(conn)
    scopes = (
        h.REVIEW_SCOPES
        | dossiers["scopes"]
        | {"knowledge:reports:write", "knowledge:reports:read"}
    )
    links = LobbyingDossierLinks(conn)
    eu = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    candidate = next(
        link for link in eu["links"] if link["link_kind"] == "unreviewed-candidate"
    )
    links.review(
        "global",
        candidate["link_id"],
        "accept",
        "the MEP's agenda names the file",
        principal_id="bob",
        scopes=scopes,
        evidence={"agenda": "fixture item"},
    )
    de = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["de"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert [link["reference"]["key"] for link in de["links"]] == [
        "de-drucksache:21/9901"
    ]

    # One registrant matched through review; register records untouched.
    identity = LobbyingIdentity(conn)
    proposed = identity.propose("global", principal_id="alice", scopes=scopes)
    registers_before_review = registers(conn)
    for pair in (
        ["lobbying:de-lobbyregister:R009901", "lobbying:eu-tr:000000000101-01"],
        ["lobbying:de-lobbyregister:R009902", "lobbying:eu-tr:000000000202-02"],
    ):
        candidate_id = next(
            c["candidate_id"] for c in proposed["candidates"] if c["records"] == pair
        )
        identity.service.review(
            "global",
            candidate_id,
            "accept",
            "the German entry states the TR number",
            principal_id="reviewer",
            scopes=scopes,
        )
    assert registers(conn) == registers_before_review

    answer = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert forbidden_keys(answer) == []
    rows = answer["declared_interests"] + answer["meetings"]
    assert rows and all(
        (row.get("citation") or {}).get("register_revision_id")
        or (row.get("citation") or {}).get("revision_id")
        for row in rows
    )
    kinds = {link["link_kind"] for row in rows for link in row["links"]}
    assert kinds == {"explicit-field", "reviewed-assertion"}
    (interest,) = answer["declared_interests"]
    assert interest["identity"]["state"] == "matched"
    assert interest["spend_ranges"][0]["as_filed"] == "200 000 - 299 999"
    (group,) = answer["side_by_side"]
    assert group["reconciled"] is False

    # The consultancy's conflicting client lists stay side by side; the UK registrant stays an unmatched string.
    queries = LobbyingQueries(conn)
    consultancy = queries.registrant_declarations(
        "global", scopes=scopes, register="eu-tr", native_id="000000000202-02"
    )
    entity = consultancy["registrants"][0]["identity"]["reviewed_links"][0]["entities"]
    gathered = queries.registrant_declarations(
        "global", scopes=scopes, entity_id=entity[1]
    )
    lists = {
        r["entry"]["register"]: sorted(c["name"] for c in r["in_force"]["clients"])
        for r in gathered["registrants"]
    }
    assert (
        lists["eu-tr"] != lists["de-lobbyregister"] and gathered["reconciled"] is False
    )
    uk = queries.registrant_declarations(
        "global", scopes=scopes, register="uk-orcl", native_id="ORCL0099"
    )
    assert uk["registrants"][0]["identity"]["state"] == "unmatched"
    assert (
        uk["registrants"][0]["in_force"]["name_as_filed"]
        == "Example Public Affairs Ltd"
    )
    forum = queries.registrant_declarations(
        "global", scopes=scopes, register="eu-tr", native_id="000000000303-03"
    )
    assert forum["registrants"][0]["in_force"]["lifecycle"] == "deregistered"

    # The cited evidence bundle.
    report = queries.export_report(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        "acceptance",
        principal_id="alice",
        scopes=scopes | {"namespace:global:read"},
    )["report"]
    assert report["content"]["bibliography"] and forbidden_keys(report) == []

    # Restart: state survives, and the monitor reports the spend revision citing both revisions.
    conn.close()
    conn = duckdb.connect(path)
    SubscriptionStore(conn).commit_watermark("global", 2)
    monitor = LobbyingMonitor(conn)
    (spend,) = monitor.run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )["notifications"]
    assert (
        spend["kind"] == "spend_range_revised" and spend["evidence_origin"] == "fixture"
    )
    assert spend["cites"]["previous_revision_id"] and spend["cites"]["revision_id"]
    assert (spend["old_ranges"][0]["upper"], spend["new_ranges"][0]["upper"]) == (
        "199999",
        "299999",
    )
    again = LobbyingQueries(conn).dossier_declared_interests(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert (
        again["declared_interests"][0]["citation"]
        == answer["declared_interests"][0]["citation"]
    )

    # The feature enabled through the coordinator; the bundle still resolves with it off.
    from tests.unit.composition.test_migration import _migrated

    _, coordinator, bundles, _ = _migrated(conn)
    coordinator.select(
        "political", bundles["political"]["version"], features=["lobbying"]
    )
    assert (
        coordinator.activate("lobbying-on")["status"] == "published"
        and feature_enabled(conn) is True
    )
    off = resolve(
        [{"pack": "political", "version": bundles["political"]["version"]}],
        list(adapt_all().values()),
        provider_descriptors(),
    )
    assert off.ok and off.plan["features"]["political"] == []
    conn.close()
