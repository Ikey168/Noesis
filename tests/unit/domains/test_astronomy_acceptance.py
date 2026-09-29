"""Astronomy and Space offline acceptance (#2149, AS12): object to cited history, no network, no credentials.

Authored fixtures naming fictional objects replay through the real adapter and
projector; identity review, as-of queries across every revision boundary,
citation links to fictional Science paper records and monitor events follow.
Sockets are blocked for the whole journey. Nothing here is live coverage.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.kb.astronomy_citations import AstronomyCitations
from src.kb.astronomy_identity import AstronomyIdentity
from src.kb.astronomy_monitoring import AstronomyMonitor
from src.kb.astronomy_queries import AstronomyQueries
from src.kb.entities import register_canonical_entity
from src.kb.geospatial import GeospatialStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit.astronomy import harness as h

# Keys any answer would carry if Noesis produced an orbit, ephemeris, conjunction, risk verdict or disposition.
COMPUTED = {
    "ephemeris",
    "state_vector",
    "propagated_position",
    "conjunction",
    "close_approach",
    "miss_distance",
    "risk_verdict",
    "hazard_assessment",
    "noesis_disposition",
    "validated",
    "tle",
    "operational_advice",
    "averaged_elements",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(
            "the offline acceptance journey opened a network connection"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_object_to_cited_designations_vintages_dispositions_launches_alerts_and_monitors():
    # The bundle composes with both optional features.
    bundles = adapt_all()
    plan = resolve(
        [
            {
                "pack": "astronomy",
                "version": bundles["astronomy"]["version"],
                "features": ["astronomy-launches", "astronomy-space-weather"],
            }
        ],
        list(bundles.values()),
        provider_descriptors(),
    )
    assert plan.ok and plan.plan["features"]["astronomy"] == [
        "astronomy-launches",
        "astronomy-space-weather",
    ]

    conn = h.connection()
    # Poll 1: everything published up to early February (plus the first SWPC poll), then a monitor baseline.
    h.acquire_all(conn, until="2099-02-06")
    h.acquire(conn, "swpc", "2099-09-01T13")
    monitor = AstronomyMonitor(conn)
    subs = {
        watch: monitor.create(
            h.NS,
            watch,
            watch=watch,
            target=target,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )["subscription_id"]
        for watch, target in (
            ("small_body", "2099 AB12"),
            ("exoplanet", "TOI-99902.01"),
            ("orbital_object", "99901"),
            ("launch_provider", "FICTSPACE"),
            ("space_weather", None),
        )
    }
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    assert all(
        monitor.run(s, principal_id=h.PRINCIPAL, scopes=h.SCOPES)["baseline"]
        for s in subs.values()
    )

    # Poll 2: the rest of the fixture timeline.
    h.acquire_all(conn)
    h.acquire(
        conn,
        "swpc",
        "2099-09-02T07",
        run_id="swpc-2",
        observed_at_ms=h.ms("2099-09-03"),
    )

    # Identity review: a canonical organisation, a similarly named provider, and a launch site place.
    register_canonical_entity(
        conn,
        "ent-fictspace-launch-services",
        "Fictspace Launch Services Inc",
        "organization",
    )
    place = GeospatialStore(conn).register_place(
        h.GEO_NS,
        "Fictland Space Centre",
        "spaceport",
        names=[
            {"value": "Fictland Space Centre", "language": "en", "kind": "canonical"}
        ],
        source_ids={"fixture": "fksc"},
        parent_ids=[],
        principal_id="geo",
        scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
        geometry={"type": "Point", "coordinates": [-30.0, -10.0]},
    )
    identity = AstronomyIdentity(conn)
    proposed = identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, geo_namespace=h.GEO_NS
    )
    org = next(
        c
        for c in proposed["candidates"]
        if c["right_key"] == "ent-fictspace-launch-services"
        and c["left_key"] == "astronomy:org:FICTSPACE"
    )
    identity.review(
        h.NS,
        org["candidate_id"],
        "accept",
        "GCAT name and register name agree",
        principal_id="rev",
        scopes=h.SCOPES,
    )
    identity.review_site(
        h.NS,
        "FKSC",
        "accept",
        selected_place_id=place["place_id"],
        reason="GCAT site",
        principal_id="rev",
        scopes=h.SCOPES,
    )

    # Citations to fictional Science paper records.
    h.seed_papers(conn, lambda: h.ms("2099-07-01"))
    links = AstronomyCitations(conn).link(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES
    )
    assert (
        links["unresolved_references"] >= 2
    )  # the Fict-303 references and MPC circulars stay visible

    q = AstronomyQueries(conn)
    answers = []

    # Small body: the provisional designation, later identified with the numbered body, cited.
    before = q.small_body_history(h.NS, "2099 AB12", "2099-04-01", scopes=h.SCOPES)
    after = q.small_body_history(h.NS, "2099 AB12", "2099-04-30", scopes=h.SCOPES)
    assert (
        before["identifications"] == []
        and after["identifications"][0]["identified_with"] == "2098 QX7"
    )
    assert after["identifications"][0]["announced_in"] == "MPEC 2099-G42"
    assert {p["state"] for p in after["identifications"][0]["papers"]} == {"unresolved"}
    # Two orbit solution vintages per publisher, with epochs and arcs, side by side.
    feb = q.orbit_solution_as_of(h.NS, "2099 AB12", "2099-03-01", scopes=h.SCOPES)
    may = q.orbit_solution_as_of(h.NS, "(999901)", "2099-05-10", scopes=h.SCOPES)
    assert {k: v["current"]["solution_id"] for k, v in feb["solutions"].items()} == {
        "JPL": "3",
        "MPC": "E2099-B17",
    }
    assert {k: v["current"]["solution_id"] for k, v in may["solutions"].items()} == {
        "JPL": "12",
        "MPC": "MPO999123",
    }
    assert may["solutions"]["JPL"]["current"]["epoch"]["jd"] == "2487824.5"
    assert may["solutions"]["MPC"]["current"]["arc"]["stated"] == "2098-2099"
    risk = q.impact_risk_listing_as_of(h.NS, "2099 AB12", "2099-04-30", scopes=h.SCOPES)
    assert risk["listings"][0]["listing_status"] == "removed"
    answers += [before, after, feb, may, risk]

    # Exoplanets: a candidate dispositioned false positive, and a retracted planet, across two dates each.
    fp_before = q.exoplanet_status_as_of(
        h.NS, "TOI-99902.01", "2099-03-01", scopes=h.SCOPES
    )
    fp_after = q.exoplanet_status_as_of(
        h.NS, "TOI-99902.01", "2099-05-15", scopes=h.SCOPES
    )
    assert fp_before["dispositions"][0]["disposition"] == "candidate"
    assert (
        fp_before["dispositions"][0]["later_changes"][0]["disposition"]
        == "false_positive"
    )
    assert fp_after["dispositions"][0]["disposition"] == "false_positive"
    retracted_before = q.exoplanet_status_as_of(
        h.NS, "Fict-303 c", "2099-03-01", scopes=h.SCOPES
    )
    retracted_after = q.exoplanet_status_as_of(
        h.NS, "Fict-303 c", "2099-06-15", scopes=h.SCOPES
    )
    assert [r["disposition"] for r in retracted_before["dispositions"]] == ["confirmed"]
    assert {
        r["source_table"]: r["disposition"] for r in retracted_after["dispositions"]
    } == {"ps": "confirmed", "removed": "retracted"}
    assert retracted_after["dispositions"][0]["papers"][0]["state"] == "unresolved"
    named = q.exoplanet_status_as_of(h.NS, "Fict-101 b", "2099-07-01", scopes=h.SCOPES)
    assert "linked" in {
        p["state"] for r in named["dispositions"] for p in r.get("papers", [])
    }
    answers += [fp_before, fp_after, retracted_before, retracted_after, named]

    # Launches: a failure, a partial failure and a success whose payload decayed, in GCAT and SATCAT.
    launches = q.launches(h.NS, scopes=h.SCOPES, provider="FICTSPACE")
    rows = {r["launch_tag"]: r for r in launches["launches"]}
    assert {t: r["outcome"]["outcome"] for t, r in rows.items()} == {
        "2099-001": "success",
        "2099-002": "failure",
        "2099-003": "partial",
    }
    assert (
        rows["2099-001"]["providers"][0]["canonical_entity"]
        == "ent-fictspace-launch-services"
    )
    assert (
        rows["2099-001"]["site"]["state"] == "resolved"
        and rows["2099-001"]["site"]["place_id"] == (place["place_id"])
    )
    history = q.orbital_object_history(h.NS, "2099-001A", scopes=h.SCOPES)
    assert history["disagreements"]["decay_date"] == {
        "celestrak-satcat": "2099-06-21",
        "gcat": "2099-06-20",
    }
    answers += [launches, history]

    # Space weather: a watch, its warning, an extension and a cancellation in a window.
    alerts = q.space_weather_alerts(h.NS, "2099-09-01", "2099-09-02", scopes=h.SCOPES)
    kinds = {r["serial"]: r["product_kind"] for r in alerts["products"]}
    assert kinds == {
        "9001": "watch",
        "9002": "warning",
        "9003": "extended_warning",
        "9004": "cancel_watch",
    }
    answers.append(alerts)

    # Unknowns stay visible.
    unknown = q.small_body_history(h.NS, "2099 YZ1", "2099-06-01", scopes=h.SCOPES)
    assert unknown["status"] == "unknown" and unknown["reason"]
    early = q.exoplanet_status_as_of(h.NS, "Fict-303 c", "2099-01-01", scopes=h.SCOPES)
    assert early["status"] == "not_yet_published"
    answers += [unknown, early]

    # Monitor events after poll 2.
    SubscriptionStore(conn).commit_watermark(h.NS, 2, kind="ingestion")
    events = {
        w: {
            n["event"]
            for n in monitor.run(s, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
                "notifications"
            ]
        }
        for w, s in subs.items()
    }
    assert events["small_body"] == {
        "designation_identified",
        "orbit_solution_published",
        "risk_listing_changed",
    }
    assert events["exoplanet"] == {"disposition_changed"} and events[
        "orbital_object"
    ] >= {"object_decayed"}
    assert events["launch_provider"] == {"launch_outcome_published"}
    assert events["space_weather"] == {
        "space_weather_issued",
        "space_weather_cancelled",
    }

    # No answer carries a computed orbit, ephemeris, conjunction, risk verdict or disposition; every answer
    # states its knowledge cutoff and an integer n.
    for answer in answers:
        assert not keys(answer) & COMPUTED, answer["query"]
        assert answer["knowledge_cutoff"]["published_by_ms"] and isinstance(
            answer["n"], int
        )
        assert "None" not in json.dumps(answer)
