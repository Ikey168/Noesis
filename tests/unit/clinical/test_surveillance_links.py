"""Series linked to publications and trials by explicit dataset citation only (I09)."""

from __future__ import annotations

import pytest

from src.kb.clinical_publications import PublicationLinker, cites, normalise_identifier
from src.kb.clinical_records import ClinicalRecordError
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture
def env():
    loaded = h.Env()
    loaded.load_all()
    loaded.documents = h.seed_documents(loaded)
    loaded.trial = h.seed_trial(loaded)
    return loaded


def linker(env):
    return PublicationLinker(env.conn, now=env.clock)


def test_only_explicit_dataset_citations_become_links(env):
    result = linker(env).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-1"
    )
    by_doc = {}
    for link in result["links"]:
        by_doc.setdefault(link.get("document_id") or link.get("trial"), set()).add(
            link["evidence_kind"]
        )
    assert by_doc == {
        env.documents["rki-citing"]: {"dataset-citation"},
        env.documents["eurostat-citing"]: {"dataset-citation"},
        "NCT09900017": {"trial-declared-dataset"},
    }
    # A shared condition or geography is never a link, not even a candidate.
    linked = {
        link.get("document_id") for link in result["links"] + result["candidates"]
    }
    assert env.documents["condition-only"] not in linked
    candidates = {
        (c.get("document_id") or c.get("trial"), c["evidence_kind"])
        for c in result["candidates"]
    }
    assert candidates == {
        (env.documents["gho-mention"], "abstract-mention"),
        ("NCT09900017", "abstract-mention"),
    }
    rki = [
        link
        for link in result["links"]
        if link.get("document_id") == env.documents["rki-citing"]
    ]
    assert len(rki) == 3  # every RKI series of the cited release
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    links = linker(env).series_links(h.NS, germany["series_id"], scopes=h.READ_ONLY)
    (publication,) = links["publications"]
    assert publication["to"]["document_id"] == env.documents["eurostat-citing"]
    assert (
        publication["to"]["revision_id"]
        and publication["evidence"]["citations"][0]["field"] == "references[0]"
    )
    (trial,) = links["trials"]
    assert trial["to"] == {"registry": "ctgov", "identifier": "NCT09900017"}
    assert (
        trial["evidence"]["declarations"][0]["declared_by"]
        == "declared_references[0].citation"
    )


def test_linking_is_idempotent_and_candidates_stay_candidates_until_reviewed(env):
    first = linker(env).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-1"
    )
    again = linker(env).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-2"
    )
    assert not [
        link for link in again["links"] + again["candidates"] if link["created"]
    ]
    candidate = next(
        c
        for c in first["candidates"]
        if c.get("document_id") == env.documents["gho-mention"]
    )
    (notif,) = env.series(provider="who-gho", kind="observation")
    assert (
        linker(env).series_links(h.NS, notif["series_id"], scopes=h.READ_ONLY)[
            "publications"
        ]
        == []
    )
    reviewed = linker(env).review_candidate(
        h.NS,
        candidate["link_id"],
        "accept",
        "the paper uses the indicator",
        principal_id="rita",
        scopes=h.SCOPES,
        observation_id="review-1",
    )
    assert (
        reviewed["status"] == "accepted"
        and reviewed["review"]["annotation_origin"] == "human"
    )
    assert (
        len(
            linker(env).series_links(h.NS, notif["series_id"], scopes=h.READ_ONLY)[
                "publications"
            ]
        )
        == 1
    )
    with pytest.raises(ClinicalRecordError):
        linker(env).review_candidate(
            h.NS,
            candidate["link_id"],
            "reject",
            "again",
            principal_id="rita",
            scopes=h.SCOPES,
            observation_id="review-2",
        )


def test_claims_are_a_separate_view_with_their_document_revision(env):
    linker(env).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-1"
    )
    env.conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE)"
    )
    env.conn.execute(
        "INSERT INTO argument_claims VALUES ('claim-1', 'Tuberculosis mortality declined (fictional).', "
        "?, 'paper', 0.8)",
        [env.documents["eurostat-citing"]],
    )
    (germany,) = env.series(provider="eurostat-health", geography_code="DE")
    view = linker(env).series_claims(h.NS, germany["series_id"], scopes=h.READ_ONLY)
    (claim,) = view["claims"]
    assert (
        claim["document_id"] == env.documents["eurostat-citing"]
        and claim["document_revision_id"]
    )
    assert (
        view["view"] == "science.literature-claims" and "no conclusion" in view["note"]
    )


def test_unlinked_series_and_cited_datasets_not_held_are_coverage_gaps(env):
    linker(env).link_series(
        h.NS, principal_id="alice", scopes=h.SCOPES, observation_id="link-1"
    )
    gaps = linker(env).coverage_gaps(h.NS, scopes=h.SCOPES)["surveillance"]
    unlinked = {g["provider"] for g in gaps["unlinked_series"]}
    assert unlinked == {"who-gho", "destatis-health", "ecdc-atlas"}
    assert {(g["kind"], g["identifier"]) for g in gaps["cited_datasets_not_held"]} == {
        ("eurostat-dataset", "hlth_cd_asdr2"),
        ("doi", "10.5281/zenodo.9900077"),
    }


def test_identifiers_match_whole_tokens_normalised_on_both_sides():
    assert cites("see HLTH_CD_ARO.", "eurostat-dataset", "hlth_cd_aro")
    assert not cites("see hlth_cd_aro2", "eurostat-dataset", "hlth_cd_aro")
    assert cites(
        "https://doi.org/10.5281/ZENODO.9900001",
        "doi",
        "https://doi.org/10.5281/zenodo.9900001",
    )
    assert not cites("10.5281/zenodo.99000012", "doi", "10.5281/zenodo.9900001")
    assert normalise_identifier("doi", "https://doi.org/10.1/X") == "10.1/x"


def test_linking_needs_write_scopes(env):
    with pytest.raises(ClinicalRecordError) as caught:
        linker(env).link_series(
            h.NS, principal_id="x", scopes=h.READ_ONLY, observation_id="o"
        )
    assert caught.value.code == "unauthorized"
