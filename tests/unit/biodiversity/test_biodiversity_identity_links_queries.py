"""BD06-BD09 (#2520, #2522, #2524, #2525): reviewable identity, links and as-of answers."""

import pytest

from src.kb import biodiversity_queries as bq
from src.kb.biodiversity_identity import BiodiversityIdentity
from src.kb.biodiversity_links import BiodiversityLinks
from src.kb.biodiversity_records import BiodiversityError
from tests.unit.biodiversity import harness
from tests.unit.biodiversity.harness import ALL, NS


@pytest.fixture(scope="module")
def world():
    env = harness.Env().loaded()
    identity = BiodiversityIdentity(env.conn, now=env.tick)
    proposals = identity.propose(NS, principal_id="alice", scopes=ALL)
    return env, identity, proposals


def _match(identity, a, b):
    return next(m for m in identity.matches(NS, scopes=ALL) if {m["left_key"], m["right_key"]} == {a, b})


def test_candidates_are_graded_and_nothing_is_accepted(world):
    _, identity, proposals = world
    matches = identity.matches(NS, scopes=ALL)
    assert matches and {m["state"] for m in matches} == {"proposed"}
    assert _match(identity, "col:PDOM1", "gbif:5231190")["basis"] == "cross-reference"
    lutra = _match(identity, "gbif:2433753", "iucn:12419")
    assert lutra["basis"] == "name-authorship" and lutra["evidence_class"] == "deterministic"
    synonym = _match(identity, "col:CCRN1", "gbif:2482492")
    assert synonym["basis"] == "synonym" and synonym["evidence_class"] == "lower-evidence"
    kinds = {c["kind"] for c in proposals["conflicts"]}
    assert kinds == {"split-or-lump", "status-differs"}
    lumps = [set(c["subjects"]) for c in proposals["conflicts"] if c["kind"] == "split-or-lump"]
    assert {"gbif:2482492", "col:CCOR1", "col:CCRN1"} in lumps  # corone against the lumped cornix too
    assert {"col:CCRN1", "gbif:2482468", "gbif:2482492"} in lumps
    assert _match(identity, "col:CCRN1", "gbif:2482468")["conflicts"]


def test_review_records_reviewer_time_checklists_and_can_be_reverted(world):
    env, identity, _ = world
    match = _match(identity, "col:LLUT1", "gbif:2433753")
    with pytest.raises(BiodiversityError):
        identity.review(NS, match["match_id"], "accept", "", principal_id="bob", scopes=ALL)
    accepted = identity.review(NS, match["match_id"], "accept", "same name and authorship", principal_id="bob",
                               scopes=ALL)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["reviewed_at_ms"]
    col_side = next(v for v in accepted["checklists"].values() if v["subject_key"] == "col:LLUT1")
    assert col_side["checklist"]["version"] == "2026-08-12" and accepted["decision_id"]
    reverted = identity.revert(NS, match["match_id"], "re-check", principal_id="bob", scopes=ALL)
    assert reverted["state"] == "reverted"
    again = identity.review(NS, match["match_id"], "accept", "confirmed", principal_id="bob", scopes=ALL)
    assert again["state"] == "accepted" and identity.members(NS, "gbif:2433753") == ["col:LLUT1", "gbif:2433753"]
    # unmatched provider taxa stay queryable by native key
    assert identity.find(NS, "2482468") == ("native_key", ["gbif:2482468"])


def test_occurrences_link_to_places_at_published_precision_and_datasets(world):
    env, _, _ = world
    mitte = env.place("Mitte (fixture)", harness.MITTE)
    berlin = env.place("Berlin (fixture)", harness.BERLIN)
    germany = env.place("Deutschland (fixture)", {"type": "Point", "coordinates": [10.45, 51.16]},
                        source_ids={"iso3166-1": "DE"})
    links = BiodiversityLinks(env.conn, now=env.tick)
    result = links.link_places(NS, principal_id="alice", scopes=ALL,
                               place_ids=[mitte["place_id"], berlin["place_id"], germany["place_id"]])
    by_record = {}
    for link in result["links"]:
        by_record.setdefault(link["detail"].get("dataset_key") or link["record_id"], []).append(link)
    store = env.store
    rid = {r["record_key"]: r["record_id"] for r in store.records(NS, record_type="occurrence")}

    def relation(key, place):
        return next((x["relation"] for x in links.links(NS, scopes=ALL, record_id=rid[key])
                     if x["target_id"] == place["place_id"]), None)

    assert relation("4011001", mitte) == "within"
    assert relation("4011002", mitte) == "uncertain" and relation("4011003", mitte) == "uncertain"
    assert relation("4022001", mitte) is None  # generalised to 10 km: below the district's level
    assert relation("4022001", berlin) == "within" and relation("4022002", germany) == "code"
    assert relation("4022002", berlin) is None  # withheld coordinates are never geocoded
    assert result["relations"]["below-generalisation"] >= 1
    placed = next(x for x in links.links(NS, scopes=ALL, record_id=rid["4011001"]) if x["relation"] == "within")
    assert placed["detail"]["receipt_id"].startswith("spatial-receipt:") and placed["revision_id"]
    dataset = next(x for x in links.links(NS, scopes=ALL, record_id=rid["4022001"]) if x["target_kind"] == "dataset")
    assert dataset["relation"] == "published-by"
    env.place_ids = {"mitte": mitte["place_id"], "berlin": berlin["place_id"], "germany": germany["place_id"]}


def test_datasets_and_assessments_cite_publications_by_exact_doi_only(world):
    env, _, _ = world
    env.seed_papers()
    result = BiodiversityLinks(env.conn, now=env.tick).link_citations(NS, principal_id="alice", scopes=ALL)
    resolved = [x for x in result["links"] if x["relation"] == "cites"]
    assert [x["target_id"] for x in resolved] == ["bio-doc:urban-sparrows"]
    unresolved = {x["detail"]["cited_text"] for x in result["links"] if x["relation"] == "unresolved"}
    assert any("internal report" in (t or "") for t in unresolved)  # no identifier: kept, not guessed
    assert any("IUCN Red List" in (t or "") for t in unresolved)


def test_occurrences_for_a_taxon_join_other_keys_only_through_accepted_matches(world):
    env, identity, _ = world
    by_col = bq.occurrences(env.conn, NS, scopes=ALL, taxon="col:PDOM1")
    assert by_col["status"] == "no occurrence on record" and by_col["counts"]["records_returned"] == 0
    match = _match(identity, "col:PDOM1", "gbif:5231190")
    identity.review(NS, match["match_id"], "accept", "published cross-reference", principal_id="bob", scopes=ALL)
    joined = bq.occurrences(env.conn, NS, scopes=ALL, taxon="Passer domesticus")
    assert {r["gbif_id"] for r in joined["occurrences"]} == {"4011001", "4011002", "4011003"}
    assert joined["taxon"]["accepted_matches"][0]["basis"] == "cross-reference"
    row = joined["occurrences"][0]
    assert row["citation"]["revision_id"] and row["citation"]["retrieved_at_ms"] and row["dataset"]["doi"]
    assert row["licence"]["id"] in {"CC-BY-4.0", "CC0-1.0"} and "abundance" in joined["counts"]["label"]
    assert not harness.forbidden_keys(joined)


def test_occurrences_for_a_place_report_uncertain_records_and_respect_as_of(world):
    env, _, _ = world
    mitte = bq.occurrences(env.conn, NS, scopes=ALL, place_id=env.place_ids["mitte"])
    assert [r["gbif_id"] for r in mitte["occurrences"]] == ["4011001"]
    assert {r["gbif_id"] for r in mitte["uncertain"]} == {"4011002", "4011003"}
    assert mitte["excluded"].get("below-generalisation") == 1
    germany = bq.occurrences(env.conn, NS, scopes=ALL, place_id=env.place_ids["germany"], taxon="gbif:2433753")
    assert {r["gbif_id"] for r in germany["occurrences"]} == {"4022001", "4022002"}
    assert germany["non_commercial"] == ["4022001", "4022002"]
    withheld = next(r for r in germany["occurrences"] if r["gbif_id"] == "4022002")
    assert withheld["coordinates"] is None and withheld["generalisation"]["coordinates_withheld"]
    before = bq.occurrences(env.conn, NS, scopes=ALL, place_id=env.place_ids["mitte"], as_of="2026-01-01")
    assert before["status"] == "no occurrence on record"


def test_status_history_by_scope_with_published_changes_and_licence(world):
    env, _, _ = world
    history = bq.status_history(env.conn, NS, "iucn:12419", scopes=ALL)
    (global_scope,) = history["scopes"]
    assert [a["year_published"] for a in global_scope["assessments"]] == ["1996", "2015", "2021"]
    assert global_scope["current"]["assessment_id"] == "900003"
    (change,) = global_scope["changes"]
    assert change["from"]["category"] == "VU" and change["to"]["category"] == "NT" and "trend" in change["notice"]
    past = bq.status_history(env.conn, NS, "iucn:12419", scopes=ALL, as_of="2016-06-01")
    assert past["scopes"][0]["current"]["assessment_id"] == "900002"
    sparrow = bq.status_history(env.conn, NS, "iucn:103818789", scopes=ALL)
    assert [s["scope"]["kind"] for s in sparrow["scopes"]] == ["global", "regional"]
    exported = bq.status_history(env.conn, NS, "iucn:103818789", scopes=ALL, purpose="export")
    item = exported["scopes"][0]["assessments"][0]
    assert "criteria" not in item and "assessment_date" not in item and exported["licence"]["tier"] == "reference-only"
    none = bq.status_history(env.conn, NS, "gbif:2482468", scopes=ALL)
    assert none["status"] == "not assessed on record" and "Not Evaluated" in none["not_assessed_notice"]


def test_lookup_taxa_shows_identities_matches_and_status_changes(world):
    env, _, _ = world
    found = bq.lookup_taxa(env.conn, NS, "Corvus cornix", scopes=ALL)
    assert set(found["subjects"]) == {"col:CCRN1", "gbif:2482468"}
    assert found["status_changes"][0]["to"]["status"] == "synonym"
    assert any(p["conflicts"] for p in found["open_proposals"])
    with pytest.raises(BiodiversityError):
        bq.occurrences(env.conn, NS, scopes=harness.READ | {"namespace:other:read"})
