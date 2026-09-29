"""H07: trial-to-publication links from declared identifiers, paper families, retractions and coverage gaps."""

import pytest

from src.ingestion.connectors.paper import trial_registry
from src.kb.clinical_publications import PublicationLinker
from src.kb.clinical_records import ClinicalRecordError, ClinicalRecordStore
from tests.unit.clinical.harness import NS, Env, load

FIXTURES_XML = "pubmed_efetch.xml"


@pytest.fixture
def env():
    env = Env()
    env.acquire("r1")
    env.seed_publications()
    env.seed_retraction()
    return env


def _links(result, identifier):
    return {(link["document_id"], link["evidence_kind"]) for link in result["links"]
            if link["trial"]["identifier"] == identifier}


def test_paper_connector_reads_declared_registry_identifiers():
    parsed = trial_registry.parse_pubmed_databanks(load(FIXTURES_XML))
    assert {(a["kind"], a["value"]) for a in parsed["99000001"]["accessions"]} == {
        ("nct", "NCT09000001"), ("eudract", "2015-900001-10")}
    assert parsed["99000003"]["accessions"] == []
    assert "Randomized Controlled Trial" in parsed["99000003"]["publication_types"]
    annotations = trial_registry.parse_europepmc_accessions(load("europepmc_annotations.json"))
    assert annotations["MED:99000004"][0]["value"] == "NCT09000003"
    relation = trial_registry.rxiv_related_resources(load("medrxiv_details.json")["collection"][0])
    assert relation[0]["predicate"] == "IsPreprintOf" and relation[0]["target_identifier"] == "10.5555/noetic2.2021"
    assert trial_registry.mentions({"title": "x", "content": "see NCT09000003."})[0]["evidence_kind"] == "abstract-mention"


def test_links_record_their_evidence_kind(env):
    result = env.link()
    pubmed1 = env.documents["pubmed:99000001"]
    assert {(pubmed1, "registry-declared-reference"), (pubmed1, "secondary-source-identifier")} <= _links(
        result, "NCT09000001")
    assert (env.documents["pubmed:99000001"], "secondary-source-identifier") in _links(result, "2015-900001-10")
    assert (env.documents["europepmc:MED:99000004"], "secondary-source-identifier") in _links(result, "NCT09000003")
    store = ClinicalRecordStore(env.conn)
    link = store.get(NS, result["links"][0]["link_id"], scopes=env.scopes())["record"]
    assert link["to"]["document_id"] and link["to"]["revision_id"]  # an existing documents revision
    assert link["evidence"]


def test_preprint_and_publication_attach_as_one_family_with_the_retraction_visible(env):
    result = env.link()
    preprint, paper = env.documents["medrxiv:10.5555/noetic2.preprint"], env.documents["pubmed:99000002"]
    family = next(f for f in result["families"] if f["trial"]["identifier"] == "NCT09000002")
    assert set(family["members"]) == {preprint, paper}
    assert (preprint, "paper-family") in _links(result, "NCT09000002")
    assert not [c for c in result["candidates"] if c["trial"]["identifier"] == "NCT09000002"]  # family, not mention
    rid = ClinicalRecordStore(env.conn).trial_id(NS, "ctgov", "NCT09000002")
    view = PublicationLinker(env.conn).publications(NS, rid, principal_id="alice", scopes=env.scopes())
    assert view["retracted"] is True
    assert view["retractions"][0]["target_document_id"] == paper
    stages = {p["document_id"]: p.get("stage") for p in view["publications"]}
    assert stages == {preprint: "preprint", paper: "version-of-record"}


def test_without_family_access_retraction_status_is_unknown_not_false(env):
    env.link()
    rid = ClinicalRecordStore(env.conn).trial_id(NS, "ctgov", "NCT09000002")
    limited = {"knowledge:clinical:read", f"namespace:{NS}:read"}
    view = PublicationLinker(env.conn).publications(NS, rid, principal_id="mallory", scopes=limited)
    assert view["retracted"] is None and view["family_access_restricted"]


def test_unlinked_trials_and_unregistered_publications_are_gaps_not_links():
    env = Env()
    env.acquire("r1")
    gaps = PublicationLinker(env.conn).coverage_gaps(NS, scopes=env.scopes())
    assert {t["identifier"] for t in gaps["unlinked_trials"]} == {"NCT09000001", "NCT09000002", "NCT09000003",
                                                                 "2023-509001-12-00", "2015-900001-10"}
    assert gaps["declared_not_harvested"][0]["reference"]["pmid"] == "99000001"
    env.seed_publications()
    env.seed_retraction()
    env.link()
    gaps = PublicationLinker(env.conn).coverage_gaps(NS, scopes=env.scopes())
    assert gaps["unlinked_trials"] == []
    assert [p["identifiers"]["pmid"] for p in gaps["unregistered_publications"]] == ["99000003"]
    links = ClinicalRecordStore(env.conn).find(NS, scopes=env.scopes(), kinds={"registry-link"})
    assert env.documents["pubmed:99000003"] not in {r["record"]["to"].get("document_id") for r in links}


def test_text_mentions_are_candidates_that_need_independent_review(env):
    # Remove the declared accession so only the abstract mention remains.
    env.conn.execute("UPDATE documents SET metadata=json_object('source_pack_native_json', "
                     "json_extract_string(metadata, '$.source_pack_native_json')) WHERE document_id=?",
                     [env.documents["europepmc:MED:99000004"]])
    result = env.link()
    candidates = [c for c in result["candidates"] if c["trial"]["identifier"] == "NCT09000003"]
    assert candidates and candidates[0]["status"] == "candidate"
    linker = PublicationLinker(env.conn)
    with pytest.raises(ClinicalRecordError):
        linker.review_candidate(NS, candidates[0]["link_id"], "accept", "checked", principal_id="bob",
                                scopes=env.scopes(), observation_id="review-0")  # no review scope
    reviewed = linker.review_candidate(NS, candidates[0]["link_id"], "accept", "abstract names the NCT",
                                       principal_id="bob", scopes=env.scopes("knowledge:clinical:review"),
                                       observation_id="review-1")
    assert reviewed["status"] == "accepted" and reviewed["review"]["principal_id"] == "bob"
