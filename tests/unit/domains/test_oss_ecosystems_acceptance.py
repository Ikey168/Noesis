"""Offline package-to-history-and-graph acceptance for the OSS Ecosystems pack (OS12, #2204).

One journey with sockets blocked: poll 1 through the source-pack runtime, poll 2
through the same adapters and projector, SPDX normalisation, repository and
organisation identity review, release and licence history, dependency graphs
as of two dates, advisories and an inventory by citation to the Technology
records, and monitor events. Every fixture is an authored response for
fictional packages; live coverage is reported separately.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.domains.technical.inventory import InventoryStore
from src.kb.oss_ecosystem_graph import dependency_graph_as_of
from src.kb.oss_ecosystem_identity import OssIdentity, repository_key_of
from src.kb.oss_ecosystem_links import OssLinks
from src.kb.oss_ecosystem_monitoring import OssPackageMonitor
from src.kb.oss_ecosystem_queries import (
    licence_history,
    package_release_history,
    packages_by_organisation,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import vulnerability_harness as vh
from tests.unit.oss_ecosystems import fixture_builder as fb
from tests.unit.oss_ecosystems import harness as h

TECH = {"knowledge:technical:read"}
MONITOR = h.READ | {"knowledge:subscriptions:read", "knowledge:subscriptions:write"}
D1, D2 = fb.DATES["d1"], fb.DATES["d2"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _protected(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name LIKE 'vuln_%' "
            "OR table_name LIKE 'technical_%' ORDER BY table_name"
        ).fetchall()
    ]
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables
    }


def test_package_to_cited_history_graphs_licences_identity_advisories_and_monitoring():
    conn = duckdb.connect()
    receipt = h.run_fixture_pack(
        conn, "poll-1"
    )  # poll 1 through the runtime (every source, fixtures)
    assert receipt["status"] == "complete"
    vh.osv(conn, "osv_PYSEC-2099-1.json", "PYSEC-2099-1", at=vh.DAY["01-11"])
    inventory = InventoryStore(conn).import_inventory(
        "fixture-parser==1.1.0\nfixture-tokens==1.2.0\n",
        "requirements.txt",
        owner_id="alice",
    )["inventory_id"]
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:fixture-labs', 'Fixture Labs', 'ORG'), "
        "('ent:ada', 'Ada Example', 'PERSON')"
    )
    monitor = OssPackageMonitor(conn, now=lambda: fb.POLL_MS[2] + 86_400_000)
    watch = monitor.create(
        h.NS,
        "parser",
        watch="package",
        key="pkg:pypi:fixture-parser",
        principal_id="alice",
        scopes=MONITOR,
    )["subscription_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    monitor.run(watch, principal_id="alice", scopes=MONITOR)
    protected = _protected(conn)

    h.ingest(conn, 2)  # the second poll
    revisions = conn.execute("SELECT count(*) FROM oss_revisions").fetchone()[0]
    h.ingest(conn, 2)
    h.ingest(conn, 1)  # re-acquisition and a late older poll add nothing
    assert conn.execute("SELECT count(*) FROM oss_revisions").fetchone()[0] == revisions

    # Release history with a yank, a deprecation and an unpublish, reasons verbatim, cited.
    parser = package_release_history(
        conn, h.NS, "pkg:pypi:fixture-parser", scopes=h.READ, as_of=D2
    )
    yanked = next(r for r in parser["releases"] if r["version"] == "1.1.0")
    assert (yanked["state"], yanked["reason"]) == (
        "yanked",
        "Broken wheel metadata; use 1.0.0",
    )
    assert (
        yanked["revision_id"].startswith("oss-rev:")
        and parser["knowledge_cutoff"]
        and parser["gaps"]
    )
    tokenizer = package_release_history(
        conn, h.NS, "pkg:npm:@fixture-labs/tokenizer", scopes=h.READ, as_of=D2
    )
    states = {r["version"]: r["state"] for r in tokenizer["releases"]}
    assert states["1.1.0"] == "deprecated" and states["0.9.0"] == "unpublished"

    # Licence change as SPDX expressions with the list version; deps.dev disagreement kept side by side.
    licences = licence_history(conn, h.NS, "pkg:pypi:fixture-parser", scopes=h.READ)
    assert [
        (c["from"], c["to"], c["spdx_list_version"]) for c in licences["changes"]
    ] == [("MIT", "BUSL-1.1", "3.25")]
    npm_licences = licence_history(
        conn, h.NS, "pkg:npm:@fixture-labs/tokenizer", scopes=h.READ
    )
    assert npm_licences["source_disagreements"][0]["deps-dev"] == "Apache-2.0"

    # Declared dependencies of two releases and graphs as of two dates, unresolved edges shown.
    first = dependency_graph_as_of(
        conn, h.NS, "pkg:cargo:fixture-codec", "0.1.0", D1, scopes=h.READ
    )
    second = dependency_graph_as_of(
        conn, h.NS, "pkg:cargo:fixture-codec", "0.1.0", D2, scopes=h.READ
    )
    assert [e["to"] for e in first["edges"]] == ["pkg:cargo:fixture-bytes@0.4.1"]
    assert [e["to"] for e in second["edges"]] == [
        "pkg:cargo:fixture-bytes@0.4.0"
    ]  # 0.4.1 was yanked
    pypi_graph = dependency_graph_as_of(
        conn, h.NS, "pkg:pypi:fixture-parser", "1.1.0", D2, scopes=h.READ
    )
    assert {u["name"] for u in pypi_graph["unresolved"]} == {
        "fixture-missing",
        "colorama",
    }
    assert "not an observed lockfile" in pypi_graph["semantics"]
    newer = dependency_graph_as_of(
        conn, h.NS, "pkg:pypi:fixture-parser", "2.0.0", D2, scopes=h.READ
    )
    assert newer["unresolved"][0]["reason"].startswith("unsatisfiable")

    # Declaring organisation and the reviewed repository with an archive snapshot.
    identity = OssIdentity(conn)
    identity.propose_repositories(h.NS, principal_id="alice", scopes=h.WRITE)
    candidate = next(
        c
        for c in identity.candidates(
            h.NS, scopes=h.READ, key=repository_key_of("github.com/fixture-labs/parser")
        )
        if c["left_key"].endswith("fixture-parser")
    )
    assert (
        candidate["basis"] == "archive-tag-corroborated" and candidate["shared_claim"]
    )
    identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "project urls and archive tags",
        principal_id="rev",
        scopes=h.REVIEW,
    )
    organisations = identity.propose_organisations(
        h.NS, principal_id="alice", scopes=h.WRITE
    )["candidates"]
    assert organisations and all(
        c["right_key"] == "entity:ent:fixture-labs" for c in organisations
    )
    reviewed = package_release_history(
        conn, h.NS, "pkg:pypi:fixture-parser", scopes=h.READ
    )
    assert (
        reviewed["repository"]["reviewed"][0]["repository"]
        == "github.com/fixture-labs/parser"
    )
    snapshots = [
        a for a in reviewed["repository"]["archive"] if a["detail"] == "snapshot"
    ]
    assert snapshots and snapshots[-1]["snapshot_swhid"].startswith("swh:1:snp:")
    assert {p["kind"] for p in reviewed["publishers"]} == {"pypi-organisation"}
    assert {
        p["package"]
        for p in packages_by_organisation(conn, h.NS, "fixture-labs", scopes=h.READ)[
            "packages"
        ]
    } >= {"pkg:pypi:fixture-parser"}

    # Advisories and the inventory by citation to technology records.
    links = OssLinks(conn)
    advisories = links.package_advisories(
        h.NS, "pkg:pypi:fixture-parser", version="1.1.0", scopes=h.READ | TECH
    )
    assert advisories["citations"][0]["advisories"][0]["native_id"] == "PYSEC-2099-1"
    beside = links.compare_inventory(
        h.NS,
        inventory,
        "pkg:pypi:fixture-parser",
        "1.1.0",
        owner_id="alice",
        scopes=h.READ | TECH,
        inventory_date=D2,
    )
    assert [e["coordinate"] for e in beside["pinned_but_yanked_or_deprecated"]] == [
        "pkg:pypi:fixture-parser"
    ]

    # Monitor events cite old and new revisions.
    SubscriptionStore(conn).commit_watermark(h.NS, 2, kind="ingestion")
    events = monitor.run(watch, principal_id="alice", scopes=MONITOR)["notifications"]
    kinds = {n["kind"] for n in events}
    assert {
        "release_yanked",
        "release_published",
        "licence_changed",
        "repository_link_changed",
    } <= kinds
    assert all(
        n["cites"].get("new_revision_id") or n["cites"].get("decision_id")
        for n in events
    )

    # No person in any record or answer, and no Technology table written by the OSS pack.
    answers = json.dumps(
        [
            parser,
            tokenizer,
            licences,
            first,
            second,
            pypi_graph,
            reviewed,
            advisories,
            beside,
            events,
            organisations,
        ]
    )
    stored = h.all_statements(conn)
    for text in (answers, stored):
        assert not [p for p in h.PERSONAL_STRINGS if p in text]
    assert "ent:ada" not in answers
    after = _protected(conn)
    assert {t: rows for t, rows in after.items() if t in protected} == protected
