"""Offline provision-to-dockets and place-to-statistics acceptance for the Legal courts features (CJ13, #2430).

The pinned ``legal-research`` 1.4.0 fixtures for the five ``courts-justice``
sources replay through the real source-pack runtime (fixture adapters compiled
from the installed pack) with sockets blocked and the ``courts`` and
``justice-statistics`` features selected. A statute provision and an
organisational party reach cited dockets and decisions with quoted
dispositions; a place reaches cited statistics with definitions, vintages and
comparability notes; an unqualified cross-jurisdiction comparison is refused.
Every docket, party, place and figure is fictional; nothing here is live
evidence.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.domains import registry as domain_registry
from src.ingestion.courts_justice_sources import FIXTURE_SECRET, CourtsJusticeAdapter, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.courts_justice import feature_enabled, forbidden_keys, readiness
from src.kb.courts_justice_identity import CourtsIdentity, JusticePlaces
from src.kb.courts_justice_monitoring import CourtsJusticeMonitor
from src.kb.justice_statistics import JusticeStatisticsStore
from src.kb.legal_court_citations import CourtCitations
from src.kb.legal_dockets import LegalDocketStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit import courts_justice_harness as h
from tests.unit.composition.test_migration import _migrated
from src.mcp_host.introspection import tool_map

PACK_ID = "legal-research"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
EXCLUDED_WORDS = ("recidivism", "risk score", "safety rating", "ranking", "won the case", "lost the case")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


class Env:
    def __init__(self, conn=None) -> None:
        self.conn = conn or h.connection()
        self.clock = 4_102_444_800_000
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("legal", bundles["legal"]["version"], features=["courts", "justice-statistics"])
        coordinator.activate("courts-justice-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source_id in h.SOURCES:
            runtime.accept_license(PACK_ID, source_id, principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "records", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def without_notices(value):
    """The answer without its boundary notices, which name the exclusions they refuse."""
    if isinstance(value, dict):
        return {k: without_notices(v) for k, v in value.items() if k not in {"notice", "note", "reason"}}
    if isinstance(value, list):
        return [without_notices(v) for v in value]
    return value


def v2_adapter(source_id: str) -> CourtsJusticeAdapter:
    return CourtsJusticeAdapter(h.source(source_id), transport=fixture_transport(
        h.native_pages(source_id, h.V2[source_id])), secret=FIXTURE_SECRET)


def test_provision_and_party_to_cited_dockets_and_place_to_cited_statistics():
    env = Env()
    conn = env.conn
    assert feature_enabled(conn, "courts") and feature_enabled(conn, "justice-statistics")
    first = env.run(h.SOURCES, "courts-1")
    assert first["status"] == "complete"
    before = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("legal_docket_revisions", "justice_vintages", "legal_works")}
    replay = env.run(h.SOURCES, "courts-replay", adapters={})
    assert replay["status"] == "complete"
    assert {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in before} == before  # nothing added

    # Minimisation: the natural-person defendant is never stored by name.
    stored = json.dumps(conn.execute("SELECT * FROM legal_docket_parties").fetchall())
    assert "Jane Roe" not in stored and "natural person 1 (Defendant)" in stored

    # Monitors start before the later provider revisions arrive.
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    monitor = CourtsJusticeMonitor(conn, now=env.now)
    docket_sub = monitor.create(h.NS, "docket", watch="docket", key=h.DOCKET, principal_id="alice",
                                scopes=h.SCOPES)["subscription_id"]
    series_sub = monitor.create(h.NS, "series", watch="series", key="us-state:EX", principal_id="alice",
                                scopes=h.SCOPES)["subscription_id"]
    monitor.run(docket_sub, 1, principal_id="alice", scopes=h.SCOPES)
    monitor.run(series_sub, 1, principal_id="alice", scopes=h.SCOPES)
    later = ("courtlistener-dockets", "fbi-cde-summarized", "police-uk-street-crime", "eurostat-crime-iccs")
    assert env.run(later, "courts-2", adapters={s: v2_adapter(s) for s in later})["status"] == "complete"
    subs.commit_watermark(h.NS, 2)
    docket_events = monitor.run(docket_sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert sorted(n["summary"]["entry_number"] for n in docket_events if n["kind"] == "new_docket_entry") == [3, 4]
    series_events = monitor.run(series_sub, 2, principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {"new_vintage", "revised_observation"} <= {n["kind"] for n in series_events}

    # Citation links: exact parsing only, unresolved citations kept.
    h.seed_us_code(conn)
    links = CourtCitations(conn).link(h.NS, scopes=h.SCOPES)["links"]
    assert {"123 F.3d 456", "5 C.F.R. § 2635.101"} <= {x["normalized"] for x in links if x["status"] == "unresolved"}

    # Provision to dockets and decisions as of a date, with quoted dispositions.
    dockets = LegalDocketStore(conn)
    answer = dockets.dockets_for_provision(h.NS, "42 U.S.C. § 1983", scopes=h.READ_ONLY, as_of="2099-08-01")
    (docket,) = answer["dockets"]
    assert docket["revision"]["revision_no"] == 2 and docket["date_terminated"] == "2099-06-20"
    assert [e["entry_number"] for e in docket["citing_entries"]] == [1, 3]
    decision = next(d for d in answer["decisions"] if d["record_key"] == h.CLUSTER)
    assert decision["disposition"]["quoted"] == "GRANTED in part and DENIED in part"
    assert decision["disposition"]["locator"]["field"] == "disposition"
    assert all(d["citation"]["retrieved_at_ms"] for d in answer["decisions"])
    earlier = dockets.dockets_for_provision(h.NS, "42 U.S.C. § 1983", scopes=h.READ_ONLY, as_of="2099-04-01")
    assert earlier["dockets"][0]["revision"]["revision_no"] == 1
    unknown = dockets.dockets_for_provision(h.NS, "18 U.S.C. § 1001", scopes=h.READ_ONLY)
    assert unknown["status"] == "no_docket_on_record"

    # Party to dockets through a reviewed identity match only.
    owner = h.seed_ownership(conn)
    identity = CourtsIdentity(conn, now=env.now)
    (candidate,) = identity.propose(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES,
                                    ownership_namespace=h.NS)["candidates"]
    assert dockets.dockets_for_party(h.NS, owner, scopes=h.SCOPES)["status"] == "no_docket_on_record"
    identity.review(h.NS, candidate["candidate_id"], "accept", "filing names the same company",
                    principal_id="bob", scopes=h.REVIEW_SCOPES)
    party = dockets.dockets_for_party(h.NS, owner, scopes=h.SCOPES, as_of="2099-08-01")
    assert party["dockets"][0]["record_key"] == h.DOCKET

    # Place to statistics with definitions, vintages and comparability notes.
    places = h.seed_places(conn)
    JusticePlaces(conn, now=env.now).resolve(h.NS, geo_namespace=h.NS, principal_id="alice", scopes=h.SCOPES)
    stats = JusticeStatisticsStore(conn, now=env.now)
    de = stats.statistics_for_place(h.NS, places["DE"], scopes=h.SCOPES, as_of="2099-05-01")
    (column,) = de["sources"]
    assert column["vintage"]["vintage_no"] == 1 and column["vintage"]["later_vintages"] == [2]
    series = next(s for s in column["series"] if s["series_key"].endswith(":NR"))
    assert series["definition"]["code"] == "ICCS0401" and series["definition"]["label"] == "Robbery"
    assert series["observations"][-1]["flags"][0]["label"] == "provisional"
    assert {n["kind"] for n in column["coverage_notes"]} == {"national_definition_footnote", "esms_comparability"}
    uk = stats.statistics_for_place(h.NS, "police-uk-neighbourhood:example-force/EX01", scopes=h.SCOPES)
    assert uk["sources"][0]["coverage_notes"][0]["kind"] == "anonymisation"
    refused = stats.compare_places(h.NS, ["us-state:EX", "eurostat-geo:DE"], scopes=h.SCOPES)
    assert refused["comparison"]["status"] == "refused_no_comparability_note" and refused["series"]
    qualified = stats.compare_places(h.NS, ["eurostat-geo:DE", "eurostat-geo:FR"], scopes=h.SCOPES)
    assert qualified["comparison"]["status"] == "qualified_by_notes"

    for result in (answer, party, de, uk, refused, qualified):
        assert forbidden_keys(result) == []
        text = json.dumps(without_notices(result)).casefold()
        assert not any(word in text for word in EXCLUDED_WORDS)
    state = readiness(conn)
    assert state["features"] == {"courts": True, "justice-statistics": True}
    assert {p["live"]["status"] for p in state["providers"].values()} == {"unverified-live"}


def test_the_mcp_tools_answer_the_same_journeys(tmp_path, monkeypatch):
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "courts-acceptance.duckdb")
    env = Env(duckdb.connect(path))
    assert env.run(h.SOURCES, "courts-mcp")["status"] == "complete"
    h.seed_us_code(env.conn)
    env.conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.REVIEW_SCOPES)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = tool_map(server.mcp)
    assert tools["link_court_citations"].fn(namespace=h.NS)["links"]
    answer = tools["dockets_citing_provision"].fn(namespace=h.NS, provision="42 U.S.C. § 1983")
    assert answer["status"] == "answered" and answer["dockets"][0]["record_key"] == h.DOCKET
    stats = tools["justice_statistics_for_place"].fn(namespace=h.NS, place="eurostat-geo:FR")
    assert stats["status"] == "answered" and stats["sources"][0]["vintage"]["source_id"] == "eurostat-crime-iccs"
    refused = tools["compare_justice_statistics"].fn(namespace=h.NS, places=["us-state:EX", "eurostat-geo:FR"])
    assert refused["comparison"]["status"] == "refused_no_comparability_note"
