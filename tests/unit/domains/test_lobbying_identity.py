"""Reviewable identity decisions for registrants and clients (#1973)."""

from __future__ import annotations

import pytest

from src.kb.lobbying import LobbyingError, LobbyingStore
from src.kb.lobbying_identity import LobbyingIdentity, issuers
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit import lobbying_harness as h

REGISTER_TABLES = (
    "lobbying_exports",
    "lobbying_entries",
    "lobbying_revisions",
    "lobbying_export_members",
)


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_all(connection)
    yield connection
    connection.close()


def registers(conn):
    return {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in REGISTER_TABLES
    }


def pairs(result):
    return {tuple(c["records"]): c for c in result["candidates"]}


def test_candidates_rest_on_stated_identifiers_across_registers_and_to_clients(conn):
    result = LobbyingIdentity(conn).propose(
        "global", principal_id="analyst", scopes=h.SCOPES
    )
    found = pairs(result)
    # The Lobbyregister entry states the EU TR number: the TR number is the EU entry's own identifier.
    eu_de = found[
        ("lobbying:de-lobbyregister:R009901", "lobbying:eu-tr:000000000101-01")
    ]
    assert eu_de["basis"] == "exact-identifier" and eu_de["state"] == "proposed"
    assert (
        eu_de["review_state"] == "unreviewed-candidate"
        and len(eu_de["register_revisions"]) == 2
    )
    # A client string with a stated TR number is a candidate for the registrant it names.
    assert (
        "lobbying:eu-tr:000000000101-01",
        "lobbying:eu-tr:000000000202-02:client:fictional-grid-association",
    ) in found
    # Two records that both merely state another register's number are a cross-reference, not an exact identity.
    assert found[
        (
            "lobbying:de-lobbyregister:R009901",
            "lobbying:eu-tr:000000000202-02:client:fictional-grid-association",
        )
    ]["basis"] == ("cross-referenced-identifier")
    # Names alone never produce a candidate here: the UK registrant stays unmatched.
    assert not [k for k in found if "lobbying:uk-orcl:ORCL0099" in k]
    again = LobbyingIdentity(conn).propose(
        "global", principal_id="analyst", scopes=h.SCOPES
    )
    assert again["proposed"] == [] and pairs(again).keys() == found.keys()


def test_issuing_country_is_respected_for_company_numbers():
    assert issuers("gb-coh", {"country": "GB"}, {"country": None}) == "same"
    assert issuers("gb-coh", {"country": "GB"}, {"country": "FR"}) == "different"
    assert issuers("lei", {"country": "DE"}, {"country": "FR"}) == "global"


def test_ownership_and_lei_candidates_and_a_reviewed_match_can_be_reverted(conn):
    source = {"provider": "companies-house", "provider_record_id": "09990099:profile"}
    OwnershipStore(conn).apply(
        "global",
        [
            record(
                "legal_entity",
                "companies-house:gb-coh:09990099",
                source,
                name="Example Public Affairs Ltd",
                jurisdiction="GB",
                identifiers=[{"scheme": "gb-coh", "value": "09990099"}],
            ),
            record(
                "legal_entity",
                "companies-house:gb-coh:09990100",
                {**source, "provider_record_id": "2"},
                name="Unrelated Ltd",
                jurisdiction="FR",
                identifiers=[{"scheme": "gb-coh", "value": "09990100"}],
            ),
        ],
        run_id="o",
        observed_at_ms=1,
        principal_id="p",
    )
    before = registers(conn)
    identity = LobbyingIdentity(conn)
    result = identity.propose(
        "global", principal_id="analyst", scopes=h.SCOPES, ownership_namespace="global"
    )
    candidate = pairs(result)[
        ("companies-house:gb-coh:09990099", "lobbying:uk-orcl:ORCL0099")
    ]
    assert candidate["basis"] == "exact-identifier"
    assert (
        candidate["evidence"][0]["left"]["revision_id"]
        and candidate["evidence"][0]["right"]["revision"] == 1
    )
    key = "lobbying:uk-orcl:ORCL0099"
    assert identity.identity("global", key, scopes=h.SCOPES)["state"] == "unmatched"
    accepted = identity.view(
        identity.service.review(
            "global",
            candidate["candidate_id"],
            "accept",
            "same company number",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    )
    assert (
        accepted["review_state"] == "reviewed-match"
        and accepted["reviewer"] == "reviewer"
    )
    assert identity.identity("global", key, scopes=h.SCOPES)["state"] == "matched"
    # Register links never regroup ownership entities.
    assert "lobbying:uk-orcl:ORCL0099" not in OwnershipIdentityService(conn).clusters(
        "global"
    )
    reverted = identity.view(
        identity.service.revert(
            "global",
            candidate["candidate_id"],
            "wrong register",
            principal_id="reviewer",
            scopes=h.REVIEW_SCOPES,
        )
    )
    assert reverted["state"] == "reverted"
    assert identity.identity("global", key, scopes=h.SCOPES)["state"] == "unmatched"
    assert (
        registers(conn) == before
    )  # no register record changed through propose, review or revert


def test_lei_records_are_matched_only_with_lei_read_access(conn):
    from src.kb.lei import LeiStore

    LeiStore(conn)
    conn.execute(
        "INSERT INTO lei_revisions VALUES ('lei-rev-1','global','5299000FIXTURE000017',NULL,'x','{}','r',1)"
    )
    conn.execute(
        "INSERT INTO lei_current VALUES ('global','5299000FIXTURE000017','lei-rev-1')"
    )
    identity = LobbyingIdentity(conn)
    with pytest.raises(LobbyingError):
        identity.propose(
            "global",
            principal_id="a",
            scopes=h.SCOPES - {"knowledge:companies:read"},
            lei_namespace="global",
        )
    result = identity.propose(
        "global", principal_id="a", scopes=h.SCOPES, lei_namespace="global"
    )
    lei = [
        c
        for c in result["candidates"]
        if "gleif:lei:5299000FIXTURE000017" in c["records"]
    ]
    assert {c["basis"] for c in lei} == {"exact-identifier"}
    assert {r for c in lei for r in c["records"]} >= {
        "lobbying:eu-tr:000000000101-01",
        "lobbying:de-lobbyregister:R009901",
    }


def test_a_name_alone_is_never_acceptable_and_stated_attributes_corroborate(conn):
    identity = LobbyingIdentity(conn)
    alone = identity.propose_link(
        "global",
        "lobbying:uk-orcl:ORCL0099",
        target_key="canonical:example-pa",
        target_entity="example-pa",
        evidence={
            "kind": "name",
            "value": "Example Public Affairs Ltd",
            "target_source": "analyst note",
        },
        principal_id="a",
        scopes=h.SCOPES,
    )
    with pytest.raises(OwnershipError) as refused:
        identity.service.review(
            "global",
            alone["candidate_id"],
            "accept",
            "looks similar",
            principal_id="r",
            scopes=h.REVIEW_SCOPES,
        )
    assert refused.value.code == "insufficient_evidence"
    corroborated = identity.propose_link(
        "global",
        "lobbying:uk-orcl:ORCL0099",
        target_key="canonical:example-pa",
        target_entity="example-pa",
        evidence={
            "kind": "name",
            "value": "Example Public Affairs Ltd",
            "target_source": "entity page",
            "attributes": [{"kind": "country", "value": "GB"}],
        },
        principal_id="a",
        scopes=h.SCOPES,
    )
    assert (
        corroborated["change"] == "upgraded"
    )  # stronger evidence upgrades the pending candidate
    view = next(
        c
        for c in identity.candidates("global", scopes=h.SCOPES)
        if c["candidate_id"] == alone["candidate_id"]
    )
    assert view["basis"] == "name-jurisdiction"
    with pytest.raises(LobbyingError):
        identity.propose_link(
            "global",
            "lobbying:uk-orcl:ORCL0099",
            target_key="x",
            target_entity="x",
            evidence={
                "kind": "lei",
                "value": "5299000FIXTURE000017",
                "target_source": "s",
            },
            principal_id="a",
            scopes=h.SCOPES,
        )


def test_three_registers_keep_three_records_for_one_organisation(conn):
    identity = LobbyingIdentity(conn)
    result = identity.propose("global", principal_id="a", scopes=h.SCOPES)
    candidate = pairs(result)[
        ("lobbying:de-lobbyregister:R009902", "lobbying:eu-tr:000000000202-02")
    ]
    identity.service.review(
        "global",
        candidate["candidate_id"],
        "accept",
        "DE entry states the TR number",
        principal_id="reviewer",
        scopes=h.REVIEW_SCOPES,
    )
    store = LobbyingStore(conn)
    keys = {e["record_key"] for e in store.entries("global", kind="registrant")}
    assert {
        "lobbying:eu-tr:000000000202-02",
        "lobbying:de-lobbyregister:R009902",
        "lobbying:uk-orcl:ORCL0099",
    } <= keys


def test_source_identity_is_left_to_that_stores_alias_review(conn):
    from src.kb.source_identity import SourceIdentityStore

    sources = SourceIdentityStore(conn)
    source = sources.register(
        "osint",
        "organization",
        "Fictional Grid Association (news desk)",
        principal_id="p",
        scopes={"knowledge:source-identity:write"},
        idempotency_key="grid",
    )
    source_id = source["source_id"]
    review = {"knowledge:source-identity:review", "knowledge:source-identity:read"}
    sources.decide_alias(
        "osint",
        source_id,
        "domain",
        "grid-association.example",
        reason="publisher domain seen",
        reviewer_id="r",
        scopes=review,
    )
    identity = LobbyingIdentity(conn)
    entry = h.entry_id(conn, h.EU_ASSOC)
    with pytest.raises(LobbyingError):
        identity.source_identity_candidates(
            "global", entry, source_namespace="osint", scopes=h.SCOPES
        )
    scopes = h.SCOPES | {"knowledge:source-identity:read"}
    out = identity.source_identity_candidates(
        "global", entry, source_namespace="osint", scopes=scopes
    )
    (candidate,) = out["candidates"]
    assert (
        candidate["source_id"] == source_id
        and candidate["state"] == "candidate"
        and out["links"] == []
    )
    operation = candidate["review_operation"]
    assert (
        operation["tool"] == "decide_source_alias"
        and operation["value"] == "lobbying:eu-tr:000000000101-01"
    )
    sources.decide_alias(
        "osint",
        source_id,
        operation["alias_type"],
        operation["value"],
        reason="registrant is this source",
        reviewer_id="r",
        scopes=review,
    )
    linked = identity.source_identity_candidates(
        "global", entry, source_namespace="osint", scopes=scopes
    )
    assert [m["source_id"] for m in linked["links"]] == [source_id]
    sources.decide_alias(
        "osint",
        source_id,
        operation["alias_type"],
        operation["value"],
        reason="reverted: separate desk",
        reviewer_id="r",
        scopes=review,
        action="split",
    )
    assert (
        identity.source_identity_candidates(
            "global", entry, source_namespace="osint", scopes=scopes
        )["links"]
        == []
    )
