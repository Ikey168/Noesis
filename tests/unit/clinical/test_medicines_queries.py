"""Label diffs (MR08), citation links (MR09) and as-of answers and timelines (MR10) for medicines, offline."""

from __future__ import annotations

import pytest

from src.kb.clinical_medicines import BOUNDARY, diff_sections, record
from src.kb.clinical_records import COUNT_SEMANTICS, ClinicalRecordError, ClinicalRecordStore
from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical.harness import NS
from tests.unit.clinical.medicines_harness import REVIEWER, SCOPES, Env


@pytest.fixture(scope="module")
def env():
    env = Env()
    env.result = env.journey()
    return env


def labels(env, provider):
    rows = ClinicalRecordStore(env.conn, initialize=False).find(NS, scopes=SCOPES, kinds={"label-revision"},
                                                                provider=provider)
    return {r["record"]["document"]["version"]: r for r in rows}


# ---------------------------------------------------------------------- MR08


def _label(sections, version):
    return record("label-revision", provider="dailymed", native_id=fb.SET_ID, jurisdiction="US", authority="FDA",
                  source_url="https://dailymed.nlm.nih.gov/x",
                  native_version={"version": version, "date": None, "basis": "spl-version"},
                  product={"provider": "dailymed", "native_id": fb.SET_ID},
                  document={"kind": "spl", "id": fb.SET_ID, "version": version}, sections=sections)


def _section(code, text, title=None):
    return {"code": code, "code_system": "loinc", "title": title, "text": text, "locator": {"xpath": f"/{code}"}}


def test_diff_reports_added_removed_changed_unchanged_and_unaligned_sections():
    before = _label([_section("1", "same"), _section("2", "old"), _section("3", "gone"), _section(None, "loose")],
                    "1")
    after = _label([_section("1", "same"), _section("2", "new"), _section("4", "fresh"),
                    _section("5", "a", "A"), _section("5", "b", "B")], "2")
    result = diff_sections(before, after)
    assert [(c["change"], c["code"]) for c in result["changes"]] == [
        ("changed", "2"), ("removed", "3"), ("added", "4"), ("added", "5")]
    changed = result["changes"][0]
    assert changed["before"]["text"] == "old" and changed["after"]["text"] == "new"
    assert changed["before"]["locator"] == {"xpath": "/2"}
    assert result["changes"][1]["after"] is None and result["changes"][2]["before"] is None
    assert result["unchanged"] == ["1"]
    assert {(u["side"], u["code"]) for u in result["unaligned"]} == {("from", None), ("to", "5")}
    assert diff_sections(before, after) == result  # deterministic
    other = record("label-revision", provider="dailymed", native_id="x", jurisdiction="US", authority="FDA",
                   source_url="https://dailymed.nlm.nih.gov/y",
                   native_version={"version": "1", "date": None, "basis": "spl-version"},
                   product={"provider": "dailymed", "native_id": "x"},
                   document={"kind": "spl", "id": "another", "version": "1"}, sections=[])
    with pytest.raises(ClinicalRecordError):
        diff_sections(before, other)


def test_stored_diffs_quote_both_revisions_and_are_reused(env):
    spl = labels(env, "dailymed")
    service = env.service()
    first = service.diff(NS, spl["8"]["record_id"], spl["7"]["record_id"], scopes=SCOPES)
    assert first["from_revision"]["version"] == "7" and first["to_revision"]["version"] == "8"
    assert first["from_revision"]["record_id"] == spl["7"]["record_id"]
    changes = {c["code"]: c for c in first["changes"]}
    assert changes["34066-1"]["change"] == "added"
    assert changes["34066-1"]["after"]["text"] == "Noetiglutide can cause acute pancreatitis (fixture text)."
    assert changes["34071-1"]["change"] == "removed"
    assert changes["43685-7"]["before"]["text"].startswith("Pancreatitis: noetiglutide has not been studied")
    assert "34067-9" in first["unchanged"]
    assert [n["code"] for n in first["not_compared"]] == ["34068-7"]
    again = service.diff(NS, spl["7"]["record_id"], spl["8"]["record_id"], scopes=SCOPES)
    assert again["record_id"] == first["record_id"] and again["reused"] is True
    stored = ClinicalRecordStore(env.conn, initialize=False).get(NS, first["record_id"], scopes=SCOPES)["record"]
    assert stored["record_kind"] == "label-section-change" and not {"significance", "summary"} & set(stored)


def test_what_changed_uses_regulator_section_numbers_for_the_smpc(env):
    answer = env.service().what_changed(NS, "noetiglutide", scopes=SCOPES, document_id="EMEA/H/C/009001:smpc")
    (diff,) = answer["diffs"]
    assert [(c["change"], c["code"]) for c in diff["changes"]] == [("changed", "4.4"), ("added", "4.8")]
    assert {n["code"] for n in diff["not_compared"]} == {"4.2", "4.9"}


# ---------------------------------------------------------------------- MR09


def test_regulatory_records_link_only_to_what_they_cite(env):
    linked = env.result["linked"]
    trials = {(t["from"]["provider"], t["trial"]): t for t in linked["trials"]}
    assert trials[("ema", "NCT09000001")]["status"] == "accepted"
    assert trials[("dailymed", "NCT09000001")]["status"] == "accepted"
    assert trials[("ema", "2015-900001-10")]["status"] == "accepted"
    store = ClinicalRecordStore(env.conn, initialize=False)
    link = store.get(NS, trials[("ema", "NCT09000001")]["link_id"], scopes=SCOPES)["record"]
    assert link["evidence_kind"] == "regulator-cited-reference"
    citation = link["evidence"]["citations"][0]
    assert "NCT09000001" in citation["citing_text"] and citation["locator"]["section"] == "5.1"
    (publication,) = linked["publications"]
    assert publication["from"]["provider"] == "ema"
    # The Drugs@FDA application and the DSC cite nothing: no trial or publication link is inferred from the name.
    assert not [t for t in linked["trials"] if t["from"]["provider"] in {"openfda", "fda-dsc"}]


def test_faers_counts_link_by_reviewed_identity_and_stay_reporting_counts(env):
    faers = {f["from"]["provider"]: f for f in env.result["linked"]["faers"]}
    assert set(faers) == {"openfda", "dailymed", "ema"} and all(f["status"] == "accepted" for f in faers.values())
    store = ClinicalRecordStore(env.conn, initialize=False)
    link = store.get(NS, faers["ema"]["link_id"], scopes=SCOPES)["record"]
    assert link["evidence"]["count_semantics"] == COUNT_SEMANTICS
    assert link["evidence_kind"] == "reviewed-substance-identity"
    counts = store.find(NS, scopes=SCOPES, kinds={"regulatory-record"}, provider="openfda",
                        native_id=link["to"]["identifier"])[0]["record"]
    assert counts["count_semantics"] == COUNT_SEMANTICS


def test_a_rejected_identity_withdraws_the_faers_link_and_absent_citations_link_nothing():
    env = Env()
    env.acquire("r1")
    env.acquire_medicines("m1", ["medicines-drugsfda-submissions", "medicines-fda-dsc"])
    env.propose()
    first = env.link_medicines("l1")
    assert first["trials"] == [] and first["publications"] == []
    (faers,) = first["faers"]
    match = env.match("openfda:faers:noetiglutide:patient.reaction.reactionmeddrapt.exact")
    env.identity().review(NS, match["match_id"], "reject", "FAERS product is a different formulation (test)",
                          principal_id="bob", scopes=REVIEWER)
    second = env.link_medicines("l2")
    assert [f["status"] for f in second["faers"]] == ["rejected"]
    record_ = ClinicalRecordStore(env.conn, initialize=False).get(NS, faers["link_id"], scopes=SCOPES)
    assert record_["record"]["status"] == "rejected" and record_["revision"] == 2
    timeline = env.service().timeline(NS, "noetiglutide", scopes=SCOPES)
    assert timeline["faers_reporting_counts"] == []


# ---------------------------------------------------------------------- MR10


def test_status_as_of_per_jurisdiction_with_citation(env):
    answer = env.service().status_as_of(NS, "noetiglutide", "2019-12-31", scopes=SCOPES)
    by = {a["jurisdiction"]: a for a in answer["jurisdictions"]}
    assert by["US"]["status"] == "approved" and by["US"]["event"]["effective_date"] == "2019-09-05"
    assert by["US"]["citation"]["disclaimer"].startswith("Do not rely on openFDA")
    assert by["EU"]["status"] is None and "no dated authorisation event" in by["EU"]["note"]
    later = env.service().status_as_of(NS, "noetiglutide", "2026-09-01", scopes=SCOPES, jurisdiction="EU")
    (eu,) = later["jurisdictions"]
    assert eu["status"] == "authorised" and eu["citation"]["source_url"].startswith("https://www.ema.europa.eu")
    assert [c["event"]["kind"] for c in eu["later_changes_before_date"]] == ["variation", "variation"]
    assert answer["boundary"] == BOUNDARY


def test_label_as_of_returns_the_revision_in_force(env):
    service = env.service()
    early = {a["provider"]: a for a in service.label_as_of(NS, "noetiglutide", "2025-01-01", scopes=SCOPES)["labels"]}
    assert early["dailymed"]["document"]["version"] == "7" and early["ema"]["citation"] is None
    later = {a["provider"]: a for a in service.label_as_of(NS, "noetiglutide", "2026-07-01", scopes=SCOPES)["labels"]}
    assert later["dailymed"]["document"]["version"] == "8" and later["ema"]["document"]["version"] == "4"
    assert later["ema"]["revisions_on_record"] == ["3", "4"]
    assert any(s["code"] == "4.8" for s in later["ema"]["sections"])


def test_communications_name_the_identity_match_used(env):
    answer = env.service().communications(NS, "Noetiglu (fixture)", scopes=SCOPES)
    (item,) = answer["communications"]
    assert item["issued"] == "2026-04-15" and item["identity_match"]["rxcui"] == "9990001"
    assert item["citation"]["source_url"].endswith(fb.DSC_PATH)


def test_timeline_is_chronological_with_label_diffs_and_cited_trials(env):
    timeline = env.service().timeline(NS, "noetiglutide", scopes=SCOPES, view_id="timeline-1")
    dates = [e["date"] for e in timeline["events"]]
    assert dates == sorted(dates)
    kinds = {(e["kind"], e["authority"]) for e in timeline["events"]}
    assert {("authorisation", "EMA"), ("authorisation", "FDA"), ("label-revision", "EMA"),
            ("label-revision", "FDA"), ("safety-communication", "FDA")} <= kinds
    spl8 = next(e for e in timeline["events"] if e["kind"] == "label-revision" and e["document"]["version"] == "8")
    assert "34066-1" in {c["code"] for c in spl8["section_changes"]["changes"]}
    assert {t["to"]["identifier"] for t in timeline["linked_trials"]} == {"NCT09000001", "2015-900001-10"}
    assert timeline["undated_events"] and all(e["date"] is None for e in timeline["undated_events"])
    assert ClinicalRecordStore(env.conn, initialize=False).view_status("timeline-1")["current"] is True


def test_a_medicine_with_no_record_is_reported_as_having_none_on_record(env):
    answer = env.service().timeline(NS, "unknownumab", scopes=SCOPES)
    assert answer["on_record"] is False and answer["events"] == []
    assert "No regulatory record" in answer["message"]
