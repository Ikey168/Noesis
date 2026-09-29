"""Offline condition-to-series-and-definitions acceptance for the Clinical Evidence surveillance feature (#2031).

Pinned, authored RKI, WHO GHO, Eurostat and Destatis fixtures replay through the real source-pack runtime (the
ECDC Atlas export through the operator import) into one cited surveillance dossier for a condition and a geography.
One test per acceptance row: the journey, conflicts, idempotency and restart, and composition. Nothing here is live
evidence.
"""

from __future__ import annotations

import pytest

from src.kb.clinical_publications import PublicationLinker
from src.kb.surveillance import NEVER_SENTENCE
from src.kb.surveillance_monitoring import SurveillanceMonitor
from src.kb.surveillance_places import SurveillancePlaces
from src.kb.surveillance_vintages import compare, pin, pin_status, reporting_delay
from tests.unit.clinical import surveillance_harness as h

TABLES = (
    "surveillance_releases",
    "surveillance_series",
    "surveillance_vintages",
    "surveillance_values",
    "surveillance_breaks",
    "surveillance_case_definition_revisions",
    "surveillance_delay_notes",
    "surveillance_geo_resolutions",
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline tests must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def journey(path=None):
    env = h.Env(path)
    receipt = env.load_all()
    assert receipt["status"] == "complete", receipt.get("failures")
    h.import_boundaries(env.conn)
    h.align_terms(env)
    h.resolve(env)
    env.documents = h.seed_documents(env)
    env.trial = h.seed_trial(env)
    PublicationLinker(env.conn, now=env.clock).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-1"
    )
    return env


def feature(env, native):
    return env.conn.execute(
        "SELECT feature_id FROM geospatial_features WHERE native_id=?", [native]
    ).fetchone()[0]


def counts(env):
    return {
        t: env.conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES
    }


def dossier(env, native="cntr.DE", **kwargs):
    return SurveillancePlaces(env.conn, initialize=False).boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, native),
        condition="tuberculosis",
        **kwargs,
    )


def test_condition_and_geography_to_a_cited_surveillance_dossier():
    env = journey()
    answer = dossier(env)
    # Series per source with kind, unit and interval; each resolved series cites its resolution and boundary revision.
    own = {
        (c["provider"], c["kind"], c["unit"]["label"], c["interval"])
        for c in answer["series"]
    }
    assert own == {
        ("ecdc-atlas", "observation", "per 100 000 population", "year"),
        ("ecdc-atlas", "observation", "cases", "year"),
        ("who-gho", "observation", "per 100 000 population", "year"),
        ("who-gho", "estimate", "per 100 000 population", "year"),
        ("eurostat-health", "observation", "deaths", "year"),
    }
    assert all(
        c["resolution"]["boundary_vintage"]["feature_revision_id"]
        for c in answer["series"]
    )
    contained = {
        (c["provider"], c["geography"]["code"]) for c in answer["contained_units"]
    }
    assert contained >= {
        ("destatis-health", "09"),
        ("rki-open-data", "09162"),
        ("eurostat-health", "DE2"),
    }
    # Reporting and reference dates on every value; a missing one is named.
    for column in answer["series"] + answer["contained_units"]:
        for value in column["values"]:
            assert "reference_period" in value and "reporting_date" in value
            assert value["unknown"] == [
                k for k in ("reference_period", "reporting_date") if value[k] is None
            ]
    # Case-definition revisions shown as breaks.
    rki = [c for c in answer["contained_units"] if c["provider"] == "rki-open-data"]
    assert any(
        b["kind"] == "case-definition" and (b["from"], b["to"]) == ("2019", "2099")
        for c in rki
        for b in c["breaks"]
    )
    # MeSH/ICD alignment with the explained steps and the unmapped term listed.
    expansion = answer["expansion"]
    assert "D014376" in expansion["mesh_ids"] and "A15-A19" in expansion["icd_codes"]
    assert [g["label"] for g in expansion["unmapped_terms"]] == [
        "Legionnaires' disease"
    ]
    # Vintage comparison with both vintages cited, and the reporting-delay view with the source's note.
    env.eurostat_update()
    env.acquire("r2", ["eurostat"])
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    compared = compare(env.conn, h.NS, germany["series_id"], scopes=h.READ_ONLY)
    (change,) = compared["changes"]
    assert (
        change["reference_period"],
        change["left"]["value"],
        change["right"]["value"],
    ) == ("2098", "288", "290")
    assert compared["left"]["vintage_id"] != compared["right"]["vintage_id"]
    assert (
        compared["notice"]
        == "differences between published vintages; no cause is inferred"
    )
    (young,) = [
        c
        for c in rki
        if c["geography"]["code"] == "09162"
        and c["dimensions"] == {"age_group": "A15-A34"}
    ]
    delay = reporting_delay(env.conn, h.NS, young["series_id"], scopes=h.READ_ONLY)
    assert delay["delay_notes"][0]["incomplete_recent"] == {
        "interval": "week",
        "count": 3,
    }
    # Boundary projection and as-of selection by reporting date.
    places = SurveillancePlaces(env.conn, initialize=False)
    before_release = places.boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, "krs.09162"),
        reporting_as_of="2099-01-06",
    )
    assert {c["reason"] for c in before_release["series"]} == {
        "historical_vintage_unavailable"
    }
    as_of = places.boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, "krs.09162"),
        reporting_as_of="2099-01-25",
    )
    reported = {
        (v["reference_period"], v["reporting_date"])
        for c in as_of["series"]
        for v in c["values"]
    }
    assert reported == {
        ("2098-12-20", "2098-12-28"),
        ("2098-12-22", "2098-12-29"),
        ("2099-01-02", "2099-01-05"),
        (None, "2099-01-12"),
    }
    # Explicit-citation links only.
    links = PublicationLinker(env.conn, initialize=False).series_links(
        h.NS, germany["series_id"], scopes=h.READ_ONLY
    )
    assert [p["to"]["document_id"] for p in links["publications"]] == [
        env.documents["eurostat-citing"]
    ]
    assert [t["to"]["identifier"] for t in links["trials"]] == ["NCT09900017"]
    # A user-threshold monitor fires, and a case-definition change is an event of its own.
    monitor = SurveillanceMonitor(env.conn, now=env.clock)
    created = monitor.create(
        h.NS,
        "tb-germany",
        watch={"condition": "tuberculosis", "feature_id": feature(env, "lan.09")},
        principal_id="alice",
        scopes=h.SCOPES,
        thresholds=[{"value": "2", "unit": "cases"}],
    )
    result = monitor.run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    kinds = {n["kind"] for n in result["notifications"]}
    assert {"threshold-exceeded", "case-definition-changed", "new-vintage"} <= kinds
    exceeded = [n for n in result["notifications"] if n["kind"] == "threshold-exceeded"]
    assert all("user-configured threshold" in n["message"] for n in exceeded)


def test_sources_stay_side_by_side_kinds_separate_and_unknown_reporting_dates_named():
    env = journey()
    answer = dossier(env)
    conflict = next(
        r
        for r in answer["side_by_side"]
        if (r["reference_period"], r["kind"], r["unit"])
        == ("2098", "observation", "per 100 000 population")
    )
    assert conflict["differ"] is True
    assert {v["provider"]: v["value"] for v in conflict["values"]} == {
        "ecdc-atlas": "5.4",
        "who-gho": "5.6",
    }
    assert "nothing merged" in conflict["note"]
    estimate = next(c for c in answer["series"] if c["kind"] == "estimate")
    observed = next(
        c
        for c in answer["series"]
        if c["kind"] == "observation" and c["provider"] == "who-gho"
    )
    assert estimate["series_id"] != observed["series_id"]
    assert not [r for r in answer["side_by_side"] if r["kind"] == "estimate"]
    gho = {v["reference_period"]: v for v in observed["values"]}
    assert gho["2097"]["reporting_date"] is None and gho["2097"]["unknown"] == [
        "reporting_date"
    ]
    bayern = SurveillancePlaces(env.conn, initialize=False).boundary_series(
        h.NS,
        scopes=h.READ_ONLY,
        feature_id=feature(env, "lan.09"),
        condition="tuberculosis",
    )
    deaths = next(r for r in bayern["side_by_side"] if r["reference_period"] == "2097")
    assert {v["provider"]: v["value"] for v in deaths["values"]} == {
        "destatis-health": "40",
        "eurostat-health": "60",
    }


def test_reacquisition_restart_new_release_and_monitor_replay_are_idempotent(tmp_path):
    path = str(tmp_path / "surveillance.duckdb")
    env = journey(path)
    before = counts(env)
    env.acquire("r1-again")
    assert env.import_ecdc()["status"] == "unchanged"
    assert counts(env) == before  # the same releases add no revision
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    (first,) = env.store().vintage_rows(h.NS, germany["series_id"])
    pin(
        env.conn,
        h.NS,
        "dossier:tb",
        [first["vintage_id"]],
        principal_id="alice",
        scopes=h.SCOPES,
    )
    monitor = SurveillanceMonitor(env.conn, now=env.clock)
    created = monitor.create(
        h.NS,
        "tb-de",
        watch={"series_id": germany["series_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    events = monitor.run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )["notifications"]
    assert [n["kind"] for n in events] == ["new-vintage"]
    env.conn.close()
    # Restart: a fresh process on the same database (the pack install is idempotent) replays the same state.
    env = h.Env(path)
    env.clock.value += 10_000_000
    env.acquire("r1-after-restart")
    assert counts(env) == before
    monitor = SurveillanceMonitor(env.conn, now=env.clock)
    replay = monitor.run(
        created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
    )
    assert replay["status"] == "replayed" and replay["notifications"] == []
    # A new release adds a vintage and marks the pinned view stale.
    env.eurostat_update()
    env.acquire("r2", ["eurostat"])
    assert len(env.store().vintage_rows(h.NS, germany["series_id"])) == 2
    status = pin_status(env.conn, h.NS, "dossier:tb", scopes=h.READ_ONLY)
    assert (
        status["stale"] is True
        and status["pins"][0]["vintage_id"] == first["vintage_id"]
    )
    after = monitor.run(
        created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES
    )
    assert [n["kind"] for n in after["notifications"]] == ["value-revised"]


def test_the_feature_off_leaves_clinical_unchanged_and_on_binds_tools_that_carry_the_never_sentence(
    tmp_path, monkeypatch
):
    import asyncio

    import duckdb

    from src.composition.adapter import adapt_all
    from src.composition.readiness import CompositionView
    from src.composition.resolver import resolve
    from src.composition.shadow import provider_descriptors
    from tools.knowledge_engine_mcp import server
    from tools.knowledge_engine_mcp.clinical import (
        SURVEILLANCE_TOOLS,
        SURVEILLANCE_WRITES,
    )

    bundles = adapt_all()

    def plan(features):
        roots = [
            {
                "pack": "clinical-evidence",
                "version": bundles["clinical-evidence"]["version"],
                "features": features,
            },
            {"pack": "science", "version": bundles["science"]["version"]},
        ]
        return resolve(roots, list(bundles.values()), provider_descriptors()).plan

    off, on = plan([]), plan(["surveillance"])
    clinical_off = {
        (b["capability"], b["provider"])
        for b in off["bindings"]
        if "clinical-evidence" in b["consumers"]
    }
    assert not {p for _, p in clinical_off} & {"clinical.surveillance"}
    assert {(c, p) for c, p in clinical_off} <= {
        (b["capability"], b["provider"]) for b in on["bindings"]
    }
    assert {f"noesis-knowledge-engine.{t}" for t in SURVEILLANCE_TOOLS} <= set(
        CompositionView(on, provider_descriptors(), bundles.values()).tools
    )
    # With the feature off the clinical journey is unchanged: it acquires the clinical-record sources only and no
    # surveillance record is written.
    from tests.unit.clinical import harness as clinical_harness

    clinical = clinical_harness.Env()
    receipt = clinical.acquire("r1")
    assert receipt["status"] == "complete"
    assert {s["source_id"] for s in receipt["sources"]} == {
        s["source_id"]
        for s in clinical.manifest["sources"]
        if s["mapping"]["target_schema"] == "noesis-clinical-record-v1"
    }
    assert not clinical.conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE "
        "'surveillance%'"
    ).fetchone()[0]
    # Every surveillance read answer carries the never-sentence.
    path = str(tmp_path / "mcp.duckdb")
    env = journey(path)
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    ids = {"series_id": germany["series_id"], "feature_id": feature(env, "cntr.DE")}
    env.conn.close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", {"operator"}))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    arguments = {
        "surveillance_series_values": {"series_id": ids["series_id"]},
        "surveillance_definition_history": {
            "definition_key": "rki-open-data:tuberkulose"
        },
        "expand_surveillance_condition": {"condition": "tuberculosis"},
        "surveillance_boundary_series": {"feature_id": ids["feature_id"]},
        "compare_surveillance_vintages": {"series_id": ids["series_id"]},
        "surveillance_reporting_delay": {"series_id": ids["series_id"]},
        "surveillance_series_links": {"series_id": ids["series_id"]},
        "surveillance_series_claims": {"series_id": ids["series_id"]},
    }
    boundary = tools["surveillance_boundary_series"].fn(
        namespace=h.NS, feature_id=ids["feature_id"]
    )
    arguments["replay_surveillance_query"] = {"receipt": boundary["receipt"]}
    for name in sorted(
        SURVEILLANCE_TOOLS - SURVEILLANCE_WRITES - {"poll_surveillance_monitor"}
    ):
        result = tools[name].fn(namespace=h.NS, **arguments.get(name, {}))
        assert result.get("ok") is not False, (name, result)
        assert result["boundary"] == NEVER_SENTENCE, name
