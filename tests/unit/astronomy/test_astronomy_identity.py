"""Astronomy identity (#2149, AS07): source-stated links, reviewable candidates and launch sites."""

from __future__ import annotations

import pytest

from src.kb.astronomy_identity import (
    AstronomyIdentity,
    designation_group,
    exoplanet_group,
    orbital_group,
    organisation_name_key,
)
from src.kb.astronomy_records import AstronomyError
from src.kb.astronomy_store import AstronomyStore
from src.kb.entities import register_canonical_entity
from tests.unit.astronomy import harness as h


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    return conn


def records(conn, **cut):
    return [v["record"] for v in AstronomyStore(conn).visible(h.NS, **cut)["records"]]


def test_designation_linkage_is_source_stated_and_follows_the_cutoff(world):
    from src.kb.astronomy_records import cutoffs

    group = designation_group(records(world), "K99A12B")  # packed input
    assert group == {"2099 AB12", "2098 QX7", "(999901)", "name:fictaria"}
    # Before the identification was published, the provisional designation stands alone.
    early = records(
        world,
        public_cutoff_ms=cutoffs("2099-03-01")["published_by_ms"],
        acquired_by_ms=h.ms("2099-03-01"),
    )
    assert designation_group(early, "2099 AB12") == {"2099 AB12"}
    assert designation_group(records(world), "Fictaria") == group


def test_a_toi_that_became_a_named_planet_links_by_the_archive_cross_identifier(world):
    group = exoplanet_group(records(world), "Fict-101 b")
    assert {"planet:fict101b", "toi:9990101"} <= group
    assert exoplanet_group(records(world), "TOI-99901.01") == group
    # Host-level identifiers never join two planets of one host.
    assert "koi:k9990302" not in exoplanet_group(records(world), "Fict-303 b")
    assert {"koi:k9990301", "planet:fict303b"} <= exoplanet_group(
        records(world), "K99903.01"
    )


def test_cospar_norad_pairs_link_deterministically_and_conflicts_stay_visible(world):
    agreed = orbital_group(records(world), "99901")
    assert {"norad:99901", "cospar:2099-001A", "jcat:S99901"} <= agreed[
        "keys"
    ] and agreed["conflicts"] == []
    disputed = orbital_group(records(world), "99904")
    assert (
        disputed["conflicts"]
        and {"cospar:2099-003A", "cospar:2099-003B"} <= disputed["keys"]
    )


def test_similarly_named_providers_never_merge_and_reviews_are_reversible(world):
    conn = world
    register_canonical_entity(
        conn,
        "ent-fictspace-launch-services",
        "Fictspace Launch Services, Inc.",
        "organization",
    )
    identity = AstronomyIdentity(conn)
    result = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    by_keys = {(c["left_key"], c["right_key"]): c for c in result["candidates"]}
    exact = by_keys[("astronomy:org:FICTSPACE", "ent-fictspace-launch-services")]
    assert exact["basis"] == "exact-name" and exact["state"] == "proposed"
    similar = by_keys[("astronomy:org:FICTSPACE", "astronomy:org:FICTSYS")]
    assert similar["basis"] == "similar-name"
    with pytest.raises(AstronomyError) as error:
        identity.review(
            h.NS,
            similar["candidate_id"],
            "accept",
            "names look alike",
            principal_id="rev",
            scopes=h.SCOPES,
        )
    assert error.value.code == "insufficient_evidence"
    # The Fictspace Launch Systems name is at most a similar name for the Services entity, never acceptable.
    assert (
        by_keys[("astronomy:org:FICTSYS", "ent-fictspace-launch-services")]["basis"]
        == "similar-name"
    )
    assert (
        organisation_name_key("Fictspace Launch Services Inc.")
        == "fictspace launch services"
    )
    accepted = identity.review(
        h.NS,
        exact["candidate_id"],
        "accept",
        "same company",
        principal_id="rev",
        scopes=h.SCOPES,
    )
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    assert (
        identity.organisation(h.NS, "FICTSPACE")["canonical_entity"]
        == "ent-fictspace-launch-services"
    )
    assert identity.organisation(h.NS, "FICTSYS")["state"] == "source string"
    reverted = identity.revert(
        h.NS, exact["candidate_id"], "wrong entity", principal_id="rev", scopes=h.SCOPES
    )
    assert reverted["state"] == "reverted"
    # Re-proposing with unchanged evidence never reactivates the reverted decision.
    identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert identity._row(h.NS, exact["candidate_id"])["state"] == "reverted"
    assert identity.organisation(h.NS, "FICTSPACE")["state"] == "source string"


def test_catalogue_conflicts_and_shared_host_tois_are_review_candidates(world):
    identity = AstronomyIdentity(world)
    candidates = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
        "candidates"
    ]
    conflict = [c for c in candidates if c["basis"] == "catalogue-conflict"]
    assert len(conflict) == 1 and conflict[0]["state"] == "proposed"
    # TOI 99901.01 is stated on the planet row, so it links without review and is never a candidate.
    assert not [
        c for c in candidates if "toi:9990101" in c["left_key"] + c["right_key"]
    ]
    with pytest.raises(AstronomyError):
        identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.READ_ONLY)


def test_launch_sites_resolve_to_places_only_through_a_geospatial_review(world):
    from src.kb.geospatial import GeospatialStore

    place = GeospatialStore(world).register_place(
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
    identity = AstronomyIdentity(world)
    sites = identity.propose(
        h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, geo_namespace=h.GEO_NS
    )["sites"]
    assert [s["site_code"] for s in sites] == ["FKSC"] and sites[0]["candidates"] == [
        place["place_id"]
    ]
    assert (
        identity.site(h.NS, "FKSC")["state"] == "proposed"
    )  # never selected automatically
    identity.review_site(
        h.NS,
        "FKSC",
        "accept",
        selected_place_id=place["place_id"],
        reason="GCAT site",
        principal_id="rev",
        scopes=h.SCOPES,
    )
    assert identity.site(h.NS, "FKSC") | {} == {
        **identity.site(h.NS, "FKSC"),
        "state": "resolved",
        "place_id": place["place_id"],
    }
