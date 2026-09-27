"""Declared interests and meetings linked to legislative dossiers (#1982)."""

from __future__ import annotations

import pytest

from src.domains.political.legislative_dossiers import (
    DossierError,
    LegislativeDossierStore,
)
from src.kb.lobbying import LobbyingError
from src.kb.lobbying_links import LobbyingDossierLinks, stage_keys
from tests.unit import lobbying_harness as h


@pytest.fixture()
def world():
    conn = h.connection()
    h.load_all(conn)
    dossiers = h.dossiers(conn)
    yield conn, dossiers, h.REVIEW_SCOPES | dossiers["scopes"]
    conn.close()


def by_kind(links):
    return {
        kind: [link for link in links if link["link_kind"] == kind]
        for kind in ("explicit-field", "unreviewed-candidate", "reviewed-assertion")
    }


def test_explicit_register_fields_link_with_both_revisions_cited(world):
    conn, dossiers, scopes = world
    links = LobbyingDossierLinks(conn)
    eu = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    grouped = by_kind(eu["links"])
    registers = {
        (link["entry_id"], link["revision_id"]) for link in grouped["explicit-field"]
    }
    assert (
        len(grouped["explicit-field"]) == 4
    )  # both EU TR revisions of the association, the EP and EC meetings
    assert len({entry for entry, _ in registers}) == 3
    for link in grouped["explicit-field"]:
        assert (
            link["reference"]["key"] == "eu-procedure:2099/0101(cod)"
            and link["dossier_revision"] == 1
        )
        assert (
            link["stage"]["citation"]["revision_id"]
            == dossiers["eu"]["stages"][0]["citation"]["revision_id"]
        )
        assert link["evidence"]["dossier_identifier"] == "2099/0101-cod"
    de = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["de"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    (printed,) = by_kind(de["links"])["explicit-field"]
    assert printed["reference"]["key"] == "de-drucksache:21/9901"
    again = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert again["linked"] == [] and again["candidates"] == []


def test_identifiers_only_match_within_their_jurisdiction_and_stage():
    eu = {
        "jurisdiction": "EU",
        "stages": [
            {
                "stage_id": "s",
                "stage": "vote",
                "procedure_identifiers": ["21/9901"],
                "normalized_document_id": "21/9901",
                "normalized_instrument_id": None,
                "citation": {},
            }
        ],
    }
    assert (
        stage_keys(eu) == {}
    )  # a Drucksache-shaped number in an EU dossier is not a German printed paper
    de_plenary = {**eu, "jurisdiction": "DE"}
    assert (
        stage_keys(de_plenary) == {}
    )  # plenary protocols share the number shape; only proposals/amendments
    de_proposal = {
        "jurisdiction": "DE",
        "stages": [{**eu["stages"][0], "stage": "proposal"}],
    }
    assert set(stage_keys(de_proposal)) == {"de-drucksache:21/9901"}


def test_shared_words_are_candidates_until_reviewed_and_reviews_can_be_reverted(world):
    conn, dossiers, scopes = world
    links = LobbyingDossierLinks(conn)
    result = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    (candidate,) = by_kind(result["links"])["unreviewed-candidate"]
    assert candidate["reference"] is None and set(
        candidate["evidence"]["shared_words"]
    ) == {"fictional", "tariffs"}
    with pytest.raises(LobbyingError):
        links.review(
            "global",
            candidate["link_id"],
            "accept",
            "same file",
            principal_id="bob",
            scopes=h.SCOPES,
        )
    accepted = links.review(
        "global",
        candidate["link_id"],
        "accept",
        "the MEP's agenda names the file",
        principal_id="bob",
        scopes=scopes,
        evidence={"agenda": "fixture agenda item 3"},
    )
    assert (
        accepted["link_kind"] == "reviewed-assertion" and accepted["reviewer"] == "bob"
    )
    assert accepted["history"][-1]["evidence"] == {"agenda": "fixture agenda item 3"}
    assert accepted["reviewed_at_ms"]
    reverted = links.revert(
        "global",
        candidate["link_id"],
        "agenda misread",
        principal_id="bob",
        scopes=scopes,
    )
    assert reverted["state"] == "reverted"
    assert candidate["link_id"] not in {
        link["link_id"]
        for link in links.links(
            "global",
            h.DOSSIER_NS,
            dossiers["eu"]["dossier_id"],
            scopes=scopes,
            states=("linked", "accepted"),
        )
    }
    explicit = by_kind(result["links"])["explicit-field"][0]
    with pytest.raises(LobbyingError) as refused:
        links.revert(
            "global", explicit["link_id"], "no", principal_id="bob", scopes=scopes
        )
    assert refused.value.code == "invalid_state"
    # Re-running discovery neither re-proposes the reverted candidate nor re-links it.
    rerun = links.link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    assert rerun["candidates"] == [] and links.link(
        "global", candidate["link_id"], scopes=scopes
    )["state"] == ("reverted")


def test_a_reviewer_can_propose_an_assertion_that_links_only_once_accepted(world):
    conn, dossiers, scopes = world
    links = LobbyingDossierLinks(conn)
    revision = conn.execute(
        "SELECT revision_id FROM lobbying_revisions WHERE entry_id=? ORDER BY revision_no DESC LIMIT 1",
        [h.entry_id(conn, h.EU_CONSULTANCY)],
    ).fetchone()[0]
    proposed = links.propose(
        "global",
        revision,
        f"{revision}#interest-0",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        evidence={"reason": "client list names the association working on this file"},
        principal_id="alice",
        scopes=scopes,
    )
    assert proposed["state"] == "candidate" and proposed["basis"] == "reviewer-proposed"
    with pytest.raises(LobbyingError):
        links.propose(
            "global",
            revision,
            "nope",
            h.DOSSIER_NS,
            dossiers["eu"]["dossier_id"],
            evidence={"reason": "x"},
            principal_id="alice",
            scopes=scopes,
        )


def test_links_appear_in_the_dossier_timeline_and_dependencies_without_changing_stages(
    world,
):
    conn, dossiers, scopes = world
    LobbyingDossierLinks(conn).link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    store = LegislativeDossierStore(conn)
    plain = store.timeline(
        h.DOSSIER_NS, dossiers["eu"]["dossier_id"], principal_id="alice", scopes=scopes
    )
    assert "lobbying_entries" not in plain
    with_lobbying = store.timeline(
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
        lobbying_namespace="global",
    )
    assert with_lobbying["entries"] == plain["entries"]
    kinds = sorted(e["event_kind"] for e in with_lobbying["lobbying_entries"])
    assert kinds == ["declared_interest", "declared_interest", "meeting", "meeting"]
    assert all(
        e["source_revision"]["export_id"] and e["cited_dossier_revision"] == 1
        for e in with_lobbying["lobbying_entries"]
    )
    deps = store.dependencies(
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
        lobbying_namespace="global",
    )
    assert {d["dependency"]["kind"] for d in deps["lobbying_links"]} == {"source"}
    with pytest.raises(DossierError) as denied:
        store.timeline(
            h.DOSSIER_NS,
            dossiers["eu"]["dossier_id"],
            principal_id="alice",
            scopes=dossiers["scopes"],
            lobbying_namespace="global",
        )
    assert denied.value.code == "unauthorized"


def test_a_new_dossier_revision_keeps_earlier_links_on_the_revision_they_cited(world):
    conn, dossiers, scopes = world
    links = LobbyingDossierLinks(conn)
    dossier_id = dossiers["eu"]["dossier_id"]
    links.link_dossier(
        "global", h.DOSSIER_NS, dossier_id, principal_id="alice", scopes=scopes
    )
    store, template = dossiers["store"], dossiers["templates"]["eu"]
    document = h._add(
        store,
        template,
        source_id="eu-eurlex-regulatory",
        identity="COM-2099-0101",
        document_type="proposal",
        title="Proposal for a Regulation on fictional grid tariffs (corr.)",
        content="Corrected proposal (fixture).",
        political={"procedure_id": "2099/0101-cod", "fixture": True},
        observed_at=5000,
    )
    second = LegislativeDossierStore(conn, now=lambda: 6000).save(
        h.DOSSIER_NS,
        "grid-tariffs",
        "EU",
        "2099/0101-cod",
        [h._ref(conn, document)],
        principal_id="alice",
        scopes=scopes,
    )
    assert second["revision"] == 2
    before = {
        link["link_id"]: link
        for link in links.links("global", h.DOSSIER_NS, dossier_id, scopes=scopes)
    }
    links.link_dossier(
        "global", h.DOSSIER_NS, dossier_id, principal_id="alice", scopes=scopes
    )
    after = links.links("global", h.DOSSIER_NS, dossier_id, scopes=scopes)
    for link in after:
        if link["link_id"] in before:
            assert link == before[link["link_id"]]  # nothing re-pointed
    assert {
        link["dossier_revision"]
        for link in after
        if link["link_kind"] == "explicit-field"
    } == {1, 2}
    first_view = links.dossier_entries(
        "global", h.DOSSIER_NS, dossier_id, 1, scopes=scopes
    )
    assert {e["cited_dossier_revision"] for e in first_view} == {1}


def test_stronger_register_evidence_supersedes_a_pending_word_candidate(world):
    conn, dossiers, scopes = world
    links = LobbyingDossierLinks(conn)
    dossier_id = dossiers["eu"]["dossier_id"]
    first = links.link_dossier(
        "global", h.DOSSIER_NS, dossier_id, principal_id="alice", scopes=scopes
    )
    (candidate,) = by_kind(first["links"])["unreviewed-candidate"]
    # The MEP later adds the procedure reference to the same declaration: a new revision of the same meeting.
    assert h.apply(conn, "ep-meetings", "ep_meetings_2099-02-20.csv")["amended"] == 1
    second = links.link_dossier(
        "global", h.DOSSIER_NS, dossier_id, principal_id="alice", scopes=scopes
    )
    assert len(second["linked"]) == 1 and second["candidates"] == []
    superseded = links.link("global", candidate["link_id"], scopes=scopes)
    assert (
        superseded["state"] == "superseded"
        and "explicit register field" in superseded["history"][-1]["reason"]
    )
    new = links.link("global", second["linked"][0], scopes=scopes)
    assert (
        new["entry_id"] == candidate["entry_id"]
        and new["link_kind"] == "explicit-field"
    )


def test_full_and_short_eli_forms_match_the_same_act():
    from src.ingestion.lobbying_sources import reference_key

    forms = [
        "http://data.europa.eu/eli/reg/2099/1/oj",
        "https://data.europa.eu/eli/reg/2099/1/oj/",
        "/eli/reg/2099/1/oj",
        "eli/reg/2099/1/oj",
        "ELI:reg/2099/1/oj",
    ]
    assert {reference_key("eli", form) for form in forms} == {"eli:reg/2099/1/oj"}
    for identifier in (
        "http://data.europa.eu/eli/reg/2099/1/oj",
        "instrument:eu:http://data.europa.eu/eli/reg/2099/1/oj",
        "instrument:eu:eli/reg/2099/1/oj",
    ):
        dossier = {
            "jurisdiction": "EU",
            "stages": [
                {
                    "stage_id": "s",
                    "stage": "publication",
                    "procedure_identifiers": [identifier],
                    "normalized_document_id": "celex-x",
                    "normalized_instrument_id": identifier,
                    "citation": {},
                }
            ],
        }
        keys = stage_keys(dossier)
        assert "eli:reg/2099/1/oj" in keys, identifier
        assert len(keys["eli:reg/2099/1/oj"]) == 1


def test_a_dossier_storing_a_full_eli_uri_links_a_short_form_register_reference(world):
    conn, dossiers, scopes = world
    store, template = dossiers["store"], dossiers["templates"]["eu"]
    eli = "http://data.europa.eu/eli/reg/2099/1/oj"
    document = h._add(
        store,
        template,
        source_id="eu-eurlex-regulatory",
        identity="32099R0001",
        document_type="regulation",
        title="Regulation (EU) 2099/1 on fictional meters",
        content="Regulation on fictional meters (fixture).",
        political={"procedure_id": eli, "instrument_id": eli, "fixture": True},
        observed_at=4000,
    )
    saved = LegislativeDossierStore(conn, now=lambda: 4500).save(
        h.DOSSIER_NS,
        "meters",
        "EU",
        eli,
        [h._ref(conn, document)],
        principal_id="alice",
        scopes=scopes | {f"document:{document}:read"},
    )
    body = (
        (h.FIXTURES / "ec_meetings_2099-02-12.json")
        .read_text()
        .replace(
            '"legislativeFiles": [{"scheme": "eu-procedure", "value": "2099/0101(COD)"}]',
            '"legislativeFiles": [{"scheme": "eli", "value": "eli/reg/2099/1/oj"}]',
        )
    )
    h.apply(conn, "ec-meetings", "ec-eli.json", body=body)
    result = LobbyingDossierLinks(conn).link_dossier(
        "global",
        h.DOSSIER_NS,
        saved["dossier_id"],
        principal_id="alice",
        scopes=scopes | {f"document:{document}:read"},
    )
    explicit = by_kind(result["links"])["explicit-field"]
    assert [link["reference"]["key"] for link in explicit] == ["eli:reg/2099/1/oj"]
    assert explicit[0]["evidence"]["dossier_identifier"] == eli
