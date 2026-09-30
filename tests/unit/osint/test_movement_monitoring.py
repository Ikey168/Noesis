"""Registry revision, identity and listing monitors through subscriptions; movement subscriptions refused (#2282)."""

from __future__ import annotations

import pytest

from src.osint.movement_monitoring import MovementMonitor
from src.osint.movements import MovementError, MovementIdentity, MovementLinks
from tests.unit.osint.movement_harness import (
    ALL,
    IMO,
    MMSI_NEW,
    NS,
    Env,
    faa_request,
    load_fisheries_gfw,
    pages,
    seed_sanctions,
)


def _kinds(result):
    return sorted((n["kind"], n["record_key"]) for n in result["notifications"])


def test_registry_identity_and_listing_changes_notify_once_and_cite_both_revisions():
    env = Env().loaded()
    load_fisheries_gfw(env.conn)
    seed_sanctions(env.conn)
    identity = MovementIdentity(env.conn)
    candidates = identity.propose(NS, principal_id="analyst", scopes=ALL, fisheries_namespace="global")["candidates"]
    link = next(c for c in candidates if {c["left_key"], c["right_key"]} ==
                {f"movements:vessel:imo:{IMO}", f"movements:vessel:mmsi:{MMSI_NEW}"})
    identity.review(NS, link["candidate_id"], "accept", "stated together", principal_id="reviewer", scopes=ALL)
    MovementLinks(env.conn).link(NS, principal_id="analyst", scopes=ALL, sanctions_namespace="legal")
    monitor = MovementMonitor(env.conn)
    created = monitor.create(NS, "watch-1", principal_id="analyst", scopes=ALL, identifiers=["N901EX", f"IMO {IMO}"],
                             sanctions_namespace="legal")
    sid = created["subscription_id"]

    baseline = monitor.run(sid, principal_id="analyst", scopes=ALL)
    kinds = _kinds(baseline)
    assert baseline["baseline"] and ("registry_published", "faa:N901EX") in kinds
    assert ("identity_match_accepted", link["candidate_id"]) in kinds
    assert sum(1 for kind, _ in kinds if kind == "listing_listed") == 2  # the vessel and the aircraft listings
    assert baseline["receipts"] and all(r["status"] == "complete" for r in baseline["receipts"])

    body = pages("movements-faa-registry")[0]["body"].replace("Valid", "Deregistered").replace(
        "</table>", '<tr><td data-label="Cancel Date">06/01/2099</td></tr></table>')
    assert env.run("movements-2", source_ids=["movements-faa-registry"],
                   overrides={faa_request("N901EX"): {"body": body}})["status"] == "complete"
    changed = monitor.run(sid, principal_id="analyst", scopes=ALL)
    (dereg,) = changed["notifications"]
    assert dereg["kind"] == "registry_deregistered" and dereg["prior"]["state"] == "registered"
    assert dereg["new"]["citation"]["revision_id"] != dereg["prior"]["citation"]["revision_id"]
    assert "06/01/2099" not in dereg["message"] and "2099-06-01" in dereg["message"]

    seed_sanctions(env.conn, delisted=True)
    delisted = monitor.run(sid, principal_id="analyst", scopes=ALL)
    assert [n["kind"] for n in delisted["notifications"]] == ["listing_delisted"]
    assert delisted["notifications"][0]["new"]["citation"]["source_revision"]

    replay = monitor.run(sid, principal_id="analyst", scopes=ALL)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    events = monitor.poll(sid, principal_id="analyst", scopes=ALL)["events"]
    assert len(events) == len(kinds) + 2


def test_position_and_call_subscriptions_are_refused_and_monitors_are_bounded():
    env = Env()
    monitor = MovementMonitor(env.conn)
    for watch in (["positions"], ["registry", "calls"], ["movements"]):
        with pytest.raises(MovementError) as refused:
            monitor.create(NS, "w", principal_id="a", scopes=ALL, identifiers=["N901EX"], watch=watch)
        assert refused.value.code == "movement_subscription_refused"
    with pytest.raises(MovementError) as over:
        monitor.create(NS, "w", principal_id="a", scopes=ALL, watch=["registry"],
                       identifiers=[f"{9000000 + i:07d}" for i in range(26)])
    assert over.value.code == "over_bound"
    with pytest.raises(MovementError) as person:
        monitor.create(NS, "w", principal_id="a", scopes=ALL, identifiers=["Jane Q Example"], watch=["registry"])
    assert person.value.code == "person_identifier_refused"
