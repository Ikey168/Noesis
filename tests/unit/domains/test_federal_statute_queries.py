"""As-of selection, provision comparison, decision citations and dossier/EU links for federal statutes (#2105)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from src.kb.legal import LegalError, LegalStore
from src.kb.legal_federal import FederalStatutes
from tests.unit import federal_statutes_harness as h

SCHEMAS = h.ROOT / "contracts/schemas/jsonschema"


@pytest.fixture(scope="module")
def loaded():
    conn = h.connection()
    h.load_all(conn)
    yield conn, FederalStatutes(LegalStore(conn))
    conn.close()


def as_of(federal, provision, day):
    return federal.provision_as_of(h.NS, "MPHG", provision, day, scopes=h.READ_ONLY)


def texts(result):
    return [[p["text"] for p in c["passages"]] for c in result["candidates"]]


def test_source_stated_observed_conflict_and_gap(loaded):
    _, federal = loaded
    validator = jsonschema.Draft7Validator(
        json.loads((SCHEMAS / "noesis-legal-provision-selection-v1.json").read_text())
    )
    gap = as_of(federal, "§5/abs2", "2029-01-01")
    assert (
        gap["status"] == "no_version_on_record"
        and gap["label"] == "no version on record for this date"
    )
    stated = as_of(federal, "§ 5 Abs. 2 MPHG", "2030-03-15")
    assert stated["status"] == "source_stated" and stated["provision"] == "§5/abs2"
    assert (
        stated["selected"]["validity_from"] == "2029-06-01"
        and "zehn" in texts(stated)[0][0]
    )
    assert (
        stated["selected"]["label"]
        == "source states validity from 2029-06-01 to 2030-03-31"
    )
    later = as_of(federal, "§5/abs2", "2030-06-01")
    assert later["status"] == "source_stated" and "fünf" in texts(later)[0][0]
    conflict = as_of(federal, "§1/abs2", "2030-06-01")
    assert conflict["status"] == "conflict" and conflict["selected"] is None
    assert [c["validity_basis"] for c in conflict["candidates"]] == [
        "source_stated",
        "observed",
    ]
    assert texts(conflict) == [
        ["(2) Es gilt nicht für Staatsanleihen und Kommunalanleihen."],
        ["(2) Es gilt nicht für Staatsanleihen."],
    ]
    observed = as_of(federal, "§5/abs2", "2031-02-01")
    assert observed["status"] == "observed"
    assert observed["label"] == "observed on 2030-06-01, validity not stated"
    missing = as_of(federal, "§8a", "2030-03-15")
    assert missing["status"] == "provision_not_in_version"
    for result in (gap, stated, conflict, observed, missing):
        assert not list(validator.iter_errors(result)), result["status"]
        assert "in force" not in json.dumps(result).replace(
            "Neither is a statement that the provision is in force", ""
        ).replace("tritt am", "")


def test_an_observation_before_a_stated_interval_is_no_conflict(loaded):
    _, federal = loaded
    result = as_of(federal, "§1/abs2", "2030-04-15")
    assert result["status"] == "source_stated" and len(result["candidates"]) == 1


def test_whole_statute_select_as_of_uses_the_same_rules(loaded):
    conn, federal = loaded
    work = federal.statute_work(h.NS, "MPHG")
    result = LegalStore(conn).select_as_of(
        h.NS, work["work_id"], "2031-02-01", scopes=h.READ_ONLY
    )
    assert result["status"] == "observed" and result["selected_version_id"]


def test_provision_diff_between_dates_names_the_source_linked_act(loaded):
    _, federal = loaded
    result = federal.compare_provision(
        h.NS, "MPHG", "§5/abs2", "2030-03-02", "2031-02-01", scopes=h.READ_ONLY
    )
    validator = jsonschema.Draft7Validator(
        json.loads((SCHEMAS / "noesis-legal-provision-comparison-v1.json").read_text())
    )
    assert not list(validator.iter_errors(result))
    assert [c["change"] for c in result["changes"]] == ["changed"]
    assert (
        "zehn" in result["changes"][0]["before"]
        and "fünf" in result["changes"][0]["after"]
    )
    (act,) = result["amendment_acts_stated_by_the_source"]
    assert (
        act["bgbl_key"] == "bgbl-1/2030/nr-45"
        and act["instruction_targets_provision"] is True
    )
    observed = sorted(
        (
            v
            for v in federal.versions(h.NS, result["work_id"])
            if v["validity_basis"] == "observed"
        ),
        key=lambda v: v["first_observed_at_ms"],
    )
    by_versions = federal.compare_provision(
        h.NS,
        "MPHG",
        "§5/abs2",
        observed[0]["version_id"],
        observed[1]["version_id"],
        scopes=h.READ_ONLY,
    )
    stated_refs = {
        a["bgbl_key"]: a["status"]
        for a in by_versions["amendment_acts_stated_by_the_source"]
    }
    assert stated_refs == {"bgbl-1/2030/nr-45": "acquired"}
    backwards = federal.compare_provision(
        h.NS,
        "MPHG",
        "§5/abs2",
        observed[1]["version_id"],
        observed[0]["version_id"],
        scopes=h.READ_ONLY,
    )
    # The earlier Stand note names a 2029 act that was never acquired: reported, not dropped.
    assert [
        (a["bgbl_key"], a["status"])
        for a in backwards["amendment_acts_stated_by_the_source"]
    ] == [("bgbl-1/2029/nr-310", "not_acquired")]
    with pytest.raises(LegalError) as caught:
        federal.compare_provision(
            h.NS,
            "MPHG",
            "§5/abs2",
            "legal-version:nope",
            "2031-02-01",
            scopes=h.READ_ONLY,
        )
    assert caught.value.code == "not_found"
    with pytest.raises(LegalError) as conflicted:
        federal.compare_provision(
            h.NS, "MPHG", "§1/abs2", "2030-03-02", "2030-06-01", scopes=h.READ_ONLY
        )
    assert conflicted.value.code == "no_single_version"


def test_decisions_citing_a_provision_with_versions_for_the_decision_date(loaded):
    _, federal = loaded
    result = federal.decisions_citing(h.NS, "MPHG", "§ 5 Abs. 2", scopes=h.READ_ONLY)
    by_docket = {
        d["identifiers"]["docket_number"]: d["citations"] for d in result["decisions"]
    }
    assert set(by_docket) == {"XI ZR 11/30", "8 C 1.31"}
    current = by_docket["XI ZR 11/30"]
    assert {
        c["locator"].get("field") or c["locator"].get("paragraph_number")
        for c in current
    } == {"norm", "1"}
    assert all(c["selection"]["status"] == "observed" for c in current)
    (old,) = by_docket["8 C 1.31"]
    assert (
        old["version_hint"] == "a.F."
        and old["selection"]["status"] == "a_f_earlier_version"
    )
    assert old["selection"]["validity_basis"] == "source_stated"
    assert old["selected_version_id"] != old["selection"]["decision_date_version_id"]
    text_offsets = old["locator"]
    assert (text_offsets["start"], text_offsets["end"]) == (15, 35)
    only_bgh = federal.decisions_citing(
        h.NS, "MPHG", "§5/abs2", scopes=h.READ_ONLY, court="BGH"
    )
    assert [d["issuing_body"] for d in only_bgh["decisions"]] == ["BGH 11. Zivilsenat"]
    window = federal.decisions_citing(
        h.NS, "MPHG", scopes=h.READ_ONLY, date_from="2031-03-01"
    )
    assert {d["identifiers"]["docket_number"] for d in window["decisions"]} == {
        "8 C 1.31"
    }
    # A citation of a statute that is in the set but not acquired still exists, with the version unresolved.
    bgb = federal.decisions_citing(h.NS, "BGB", "§823/abs2", scopes=h.READ_ONLY)
    assert (
        bgb["decisions"][0]["citations"][0]["selection"]["status"]
        == "statute_not_acquired"
    )


def test_a_f_without_an_earlier_differing_version_stays_flagged():
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    h.apply(conn, "rii", h.rii_pages(), at="2031-06-01")
    result = FederalStatutes(LegalStore(conn)).decisions_citing(
        h.NS, "MPHG", "§5/abs2", scopes=h.READ_ONLY
    )
    flagged = [
        c
        for d in result["decisions"]
        for c in d["citations"]
        if c["version_hint"] == "a.F."
    ]
    assert (
        flagged[0]["selection"]["status"] == "a_f_undetermined"
        and flagged[0]["selected_version_id"] is None
    )


def test_amendment_acts_with_dossier_and_implementation_links(loaded):
    conn, federal = loaded
    before = federal.link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    assert (
        before["linked"] == []
        and "no legislative dossier" in before["unlinked"][0]["reason"]
    )
    h.dip_dossier(conn)
    linked = federal.link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    assert [link["bgbl_key"] for link in linked["linked"]] == ["bgbl-1/2030/nr-45"]
    assert "BGBl I 2030 Nr. 45" in linked["linked"][0]["evidence"]
    again = federal.link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    assert again["linked"][0]["link_id"] == linked["linked"][0]["link_id"]
    assert conn.execute("SELECT count(*) FROM legal_dossier_links").fetchone()[0] == 1
    (act,) = federal.list_amendment_acts(
        h.NS, scopes=h.READ_ONLY, statute="MPHG", provision="§5/abs2"
    )["acts"]
    assert (
        act["bgbl_citation"] == "BGBl. 2030 I Nr. 45"
        and act["dossier_links"][0]["relation"] == "enacted_as"
    )
    (implements,) = act["implements"]
    assert implements["celex"] == "32030L0077" and implements["status"] == "resolved"
    assert implements["locator"]["path"] == "note/1"
    statute = federal.statute_work(h.NS, "MPHG")
    (stated,) = federal.implementation_links(h.NS, statute["work_id"])[:1]
    assert stated["celex"] == "32029L1234" and stated["status"] == "unresolved"
    with pytest.raises(LegalError) as caught:
        federal.link_amendment_dossiers(
            h.NS,
            scopes=h.SCOPES - {"knowledge:political:dossier:read"},
            principal_id="alice",
        )
    assert caught.value.code == "unauthorized"


def test_unmatched_dossiers_keep_acts_unlinked_with_the_reason():
    conn = h.connection()
    h.load_all(conn)
    h.dip_dossier(conn, quote="Verkündung: BGBl I 2030 Nr. 99")
    federal = FederalStatutes(LegalStore(conn))
    result = federal.link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    assert (
        result["linked"] == []
        and "states this BGBl citation" in result["unlinked"][0]["reason"]
    )
    (act,) = federal.list_amendment_acts(h.NS, scopes=h.READ_ONLY)["acts"]
    assert act["dossier_links"][0]["status"] == "unlinked"


def test_reads_before_any_source_ran_are_not_ready_and_scopes_are_enforced():
    conn = h.connection()
    federal = FederalStatutes(LegalStore(conn, initialize=False))
    with pytest.raises(LegalError) as caught:
        federal.provision_as_of(h.NS, "MPHG", "§5", "2030-01-01", scopes=h.READ_ONLY)
    assert caught.value.code == "not_ready"
    assert federal.resolve(h.NS, "§ 5 WpHG", scopes=h.READ_ONLY)["status"] == "resolved"
    h.load_all(conn)
    with pytest.raises(LegalError) as unauthorized:
        FederalStatutes(LegalStore(conn)).provision_as_of(
            h.NS, "MPHG", "§5", "2030-01-01", scopes={"knowledge:legal:read"}
        )
    assert unauthorized.value.code == "unauthorized"
    with pytest.raises(LegalError) as unknown:
        FederalStatutes(LegalStore(conn)).provision_as_of(
            h.NS, "BGB", "§ 823 BGB", "2030-01-01", scopes=h.READ_ONLY
        )
    assert unknown.value.code == "statute_not_acquired"
