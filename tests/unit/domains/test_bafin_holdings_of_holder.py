"""BaFin holdings by holder across issuers (#2884).

Authored fictional fixtures read with both fictional issuers in scope
(Musterwerke AG ``DE000MSTR014`` and Beispiel Maschinenbau SE ``DE000BSPL026``):
the organisation Fiktiva Holding SE notifies in both, and the natural person
Erika Musterfrau (``Dr. Erika Musterfrau`` in one export) notifies in both. The
organisation is one record across issuers; the person is keyed per issuer and
is read across issuers only in person-research mode once a reviewer accepts the
cross-issuer candidate.
"""

from __future__ import annotations

import copy

import duckdb
import pytest

from src.domains.market.bafin_identity import (
    BafinIdentity,
    organisation_key,
    person_key,
)
from src.domains.market.bafin_notices import BafinError, end_of_day_ms
from src.domains.market.bafin_queries import (
    CONTRACT,
    BafinQueries,
    export_dossier_bundle,
    export_holdings_bundle,
)
from src.evidence_bundle.verifier import verify_bundle
from tests.unit import bafin_harness as h

HOLDING = organisation_key("Fiktiva Holding SE")
ERIKA_MSTR = person_key(h.ISSUER, "Dr. Erika Musterfrau")
ERIKA_BSPL = person_key(h.OTHER_ISSUER, "Erika Musterfrau")


@pytest.fixture()
def research(monkeypatch):
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")


@pytest.fixture()
def default_mode(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)


@pytest.fixture()
def world():
    conn = h.connection()
    h.acquire_cross_issuer(conn)
    return conn


def by_issuer(answer):
    return {g["issuer_key"]: g for g in answer["issuers"]}


def sources(rows):
    return [r["source"]["source_id"] for r in rows]


def accept_cross_issuer(conn, *, revert=False):
    """A reviewer upgrades the automatic name candidate with attribute evidence and accepts it."""
    identity = BafinIdentity(conn)
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    candidate = next(
        c
        for c in proposed["candidates"]
        if set(c["records"]) == {ERIKA_MSTR, ERIKA_BSPL}
    )
    assert candidate["state"] == "proposed" and candidate["basis"] == "similar-name"
    upgraded = identity.propose_link(
        h.NS,
        ERIKA_MSTR,
        target_key=ERIKA_BSPL,
        target_entity=None,
        evidence={
            "kind": "name",
            "value": "Erika Musterfrau",
            "target_source": "VR-2026-0012 names the notifier",
            "attributes": [
                {
                    "kind": "authored-attribute",
                    "value": "the same fictional person",
                    "source": "synthetic reviewer note",
                }
            ],
        },
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    assert upgraded["candidate_id"] == candidate["candidate_id"]
    identity.service.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "the notifications name the same person",
        principal_id="reviewer",
        scopes=h.SCOPES,
    )
    if revert:
        identity.service.revert(
            h.NS,
            candidate["candidate_id"],
            "checked again",
            principal_id="reviewer",
            scopes=h.SCOPES,
        )
    return candidate["candidate_id"]


def test_an_organisation_holder_is_read_per_issuer_cited_to_notice_revisions(
    world, default_mode
):
    q = BafinQueries(world)
    answer = q.holdings_of_holder(
        h.NS, "2026-05-01", scopes=h.SCOPES, name="Fiktiva Holding SE"
    )
    assert answer["contract"] == CONTRACT and answer["query"] == "holdings_of_holder"
    assert answer["holder"]["record_key"] == HOLDING
    groups = by_issuer(answer)
    assert sorted(groups) == [h.OTHER_ISSUER, h.ISSUER]
    musterwerke = groups[h.ISSUER]["holdings"][0]
    assert musterwerke["source"]["source_id"] == "VR-2026-0007"
    assert musterwerke["role"] == "notifier" and len(musterwerke["corrections"]) == 1
    assert [m["name"] for m in musterwerke["chain"]][-1] == "Fiktiva Invest GmbH"
    assert {p["basis"]: p["percentage"] for p in musterwerke["positions"]} == {
        "s33": "5.21",
        "s38": "0",
        "s39": "5.21",
    }
    assert all("WpHG" in p["provision"] for p in musterwerke["positions"])
    assert sources(groups[h.OTHER_ISSUER]["holdings"]) == ["VR-2026-0002"]
    # Every holding is cited to the notice revision it rests on, and nothing is summed.
    cited = {
        r["notice_id"]: r["revision_id"]
        for g in answer["issuers"]
        for r in g["holdings"] + g["notified_below_lowest_threshold"]
    }
    assert answer["pins"] == dict(sorted(cited.items()))
    assert all(r.startswith("bafin-rev:") for r in answer["pins"].values())
    assert (
        "total" not in str([list(g) for g in answer["issuers"]])
        and "never summed" in answer["semantics_detail"]
    )
    # By record key the answer is the same.
    same = q.holdings_of_holder(h.NS, "2026-05-01", scopes=h.SCOPES, party=HOLDING)
    assert same["issuers"] == answer["issuers"]


def test_publication_cutoffs_apply_and_a_fall_below_the_threshold_is_shown_as_stated(
    world, default_mode
):
    q = BafinQueries(world)
    early = q.holdings_of_holder(
        h.NS, "2026-03-04", scopes=h.SCOPES, name="Fiktiva Holding SE"
    )
    # VR-2026-0001 (Musterwerke) is published on 2026-03-05: not yet visible.
    assert sorted(by_issuer(early)) == [h.OTHER_ISSUER]
    assert early["cutoffs"]["publicly_available_by_ms"] == end_of_day_ms("2026-03-04")
    before_correction = q.holdings_of_holder(
        h.NS, "2026-03-19", scopes=h.SCOPES, name="Fiktiva Holding SE"
    )
    assert (
        by_issuer(before_correction)[h.ISSUER]["holdings"][0]["percentages"]["s33"]
        == "5.12"
    )
    late = q.holdings_of_holder(
        h.NS, "2026-05-20", scopes=h.SCOPES, name="Fiktiva Holding SE"
    )
    beispiel = by_issuer(late)[h.OTHER_ISSUER]
    assert beispiel["holdings"] == []
    below = beispiel["notified_below_lowest_threshold"][0]
    assert below["source"]["source_id"] == "VR-2026-0013"
    assert below["percentages"]["s33"] == "2.4"  # as stated, never 0 % or dropped
    assert below["earlier_notifications"]  # VR-2026-0002 stays in the record
    # Acquired before the 2026-05-20 export: the fall below 3 % is not seen.
    acquired = q.holdings_of_holder(
        h.NS,
        "2026-05-20",
        scopes=h.SCOPES,
        name="Fiktiva Holding SE",
        acquired_by_ms=h.ms("2026-05-01"),
    )
    assert sources(by_issuer(acquired)[h.OTHER_ISSUER]["holdings"]) == ["VR-2026-0002"]


def test_a_chain_member_is_read_through_the_notifiers_notification(world, default_mode):
    answer = BafinQueries(world).holdings_of_holder(
        h.NS, "2026-05-01", scopes=h.SCOPES, name="Fiktiva Invest GmbH"
    )
    assert sorted(by_issuer(answer)) == [h.ISSUER]
    item = by_issuer(answer)[h.ISSUER]["holdings"][0]
    assert item["role"] == "chain member"
    assert item["chain_member"]["name"] == "Fiktiva Invest GmbH"
    assert item["notifier"]["name"] == "Fiktiva Holding SE"


def test_a_person_joins_another_issuer_only_once_the_candidate_is_accepted(
    world, research
):
    q = BafinQueries(world)

    def read():
        return q.holdings_of_holder(
            h.NS,
            "2026-05-20",
            scopes=h.SCOPES,
            name="Erika Musterfrau",
            kind="natural_person",
            isin=h.ISSUER,
        )

    alone = read()
    assert alone["holder"]["record_key"] == ERIKA_MSTR
    assert sorted(by_issuer(alone)) == [h.ISSUER]
    assert sources(by_issuer(alone)[h.ISSUER]["holdings"]) == ["VR-2026-0011"]
    assert alone["unjoined_candidates"] == []
    # Person-research mode proposes the cross-issuer candidate: it is listed apart, unjoined.
    BafinIdentity(world).propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    proposed = read()
    assert sorted(by_issuer(proposed)) == [h.ISSUER]
    assert proposed["identity"]["joined_records"] == [ERIKA_MSTR]
    [unjoined] = proposed["unjoined_candidates"]
    assert unjoined["record_key"] == ERIKA_BSPL and unjoined["state"] == "proposed"
    assert unjoined["cross_issuer"] is True
    assert [g["issuer_key"] for g in unjoined["issuers"]] == [h.OTHER_ISSUER]
    # Accepted: both issuers' holdings, each cited to its notice revision, never summed.
    candidate_id = accept_cross_issuer(world)
    joined = read()
    groups = by_issuer(joined)
    assert sorted(groups) == [h.OTHER_ISSUER, h.ISSUER]
    assert joined["identity"]["accepted_candidates"] == [candidate_id]
    assert joined["identity"]["joined_records"] == sorted([ERIKA_MSTR, ERIKA_BSPL])
    assert joined["unjoined_candidates"] == []
    beispiel = groups[h.OTHER_ISSUER]["holdings"][0]
    assert beispiel["source"]["source_id"] == "VR-2026-0012"
    assert beispiel["record_key"] == ERIKA_BSPL
    assert {p["basis"]: p["percentage"] for p in beispiel["positions"]} == {
        "s33": "5.05",
        "s38": "0.5",
        "s39": "5.55",
    }
    assert beispiel["thresholds_reached"]["s39"] == ["5"]
    assert len(beispiel["earlier_notifications"]) == 1  # VR-2026-0010
    assert set(joined["pins"]) == {
        groups[h.ISSUER]["holdings"][0]["notice_id"],
        beispiel["notice_id"],
    }
    # Reading from the other issuer's record gives the same holdings.
    other = q.holdings_of_holder(h.NS, "2026-05-20", scopes=h.SCOPES, party=ERIKA_BSPL)
    assert other["issuers"] == joined["issuers"]


def test_a_reverted_decision_no_longer_joins(world, research):
    accept_cross_issuer(world, revert=True)
    answer = BafinQueries(world).holdings_of_holder(
        h.NS, "2026-05-20", scopes=h.SCOPES, party=ERIKA_MSTR
    )
    assert sorted(by_issuer(answer)) == [h.ISSUER]
    assert answer["identity"]["accepted_candidates"] == []


def test_outside_person_research_mode_a_person_stays_within_its_issuer(
    world, monkeypatch
):
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    accept_cross_issuer(world)
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)
    answer = BafinQueries(world).holdings_of_holder(
        h.NS, "2026-05-20", scopes=h.SCOPES, party=ERIKA_MSTR
    )
    assert sorted(by_issuer(answer)) == [h.ISSUER]
    assert answer["identity"]["person_research"] is False
    assert answer["unjoined_candidates"] == []


def test_the_answer_exports_as_a_verifiable_evidence_bundle(world, research):
    accept_cross_issuer(world)
    answer = BafinQueries(world).holdings_of_holder(
        h.NS, "2026-05-20", scopes=h.SCOPES, party=ERIKA_MSTR
    )
    bundle = export_holdings_bundle(world, answer, created_at_ms=1)
    verified = verify_bundle(bundle)
    assert verified.valid, verified.errors
    evidence = [o for o in bundle["objects"] if o["type"] == "evidence"]
    assert len(evidence) == len(answer["pins"]) == 2
    forged = copy.deepcopy(answer)
    forged["pins"]["bafin-notice:x"] = "bafin-rev:does-not-exist"
    with pytest.raises(BafinError):
        export_holdings_bundle(world, forged, created_at_ms=1)
    with pytest.raises(BafinError):
        export_dossier_bundle(world, answer, created_at_ms=1)
    holders = BafinQueries(world).holders_above_thresholds(
        h.NS, h.ISSUER, "2026-05-20", scopes=h.SCOPES
    )
    with pytest.raises(BafinError):
        export_holdings_bundle(world, holders, created_at_ms=1)


def test_requests_are_validated_and_scoped(world, default_mode):
    q = BafinQueries(world)
    for arguments in (
        {},
        {"party": HOLDING, "name": "Fiktiva Holding SE"},
        {"party": "bafin:named:fiktiva invest"},
        {"name": "Erika Musterfrau", "kind": "natural_person"},
        {"name": "Fiktiva Holding SE", "isin": h.ISSUER},
        {"name": "Fiktiva Holding SE", "kind": "trust"},
    ):
        with pytest.raises(BafinError) as raised:
            q.holdings_of_holder(h.NS, "2026-05-01", scopes=h.SCOPES, **arguments)
        assert raised.value.code == "invalid_request", arguments
    with pytest.raises(BafinError) as raised:
        q.holdings_of_holder(
            h.NS, "2026-05-01", scopes={"market:bafin:read"}, party=HOLDING
        )
    assert raised.value.code == "unauthorized"
    unknown = q.holdings_of_holder(
        h.NS, "2026-05-01", scopes=h.READ_ONLY, name="Nobody Holding AG"
    )
    assert unknown["issuers"] == [] and unknown["pins"] == {}
    with pytest.raises(BafinError) as raised:
        BafinQueries(duckdb.connect(":memory:")).holdings_of_holder(
            h.NS, "2026-05-01", scopes=h.SCOPES, party=HOLDING
        )
    assert raised.value.code == "not_ready"
