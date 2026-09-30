"""PubChem, ECHA CLP, ECHA REACH and CompTox acquisition through the source-pack runtime (CH03-CH06)."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.source_packs import SourcePackConformance, SourcePackError, validate_source_pack
from src.ingestion.substance_sources import (
    EXCLUDED_FIELDS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    SubstanceSourceAdapter,
    fixture_transport,
    selection_entries,
)
from src.kb.substances_records import DATA_POINT_LABEL
from tests.unit.chemicals import harness as h

NS = h.NS


@pytest.fixture()
def env():
    item = h.Env()
    yield item
    item.conn.close()


def _records(env, record_type, provider=None, subject=None):
    return [r for r in env.store.records(NS, record_type=record_type, provider=provider)
            if subject is None or r["subject_key"] == subject]


def test_pack_validates_pins_fixtures_and_records_access_decisions():
    raw = json.loads(h.PACK.read_text())
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    pack = validate_source_pack(raw)
    assert not list(Draft7Validator(schema).iter_errors(pack))
    result = SourcePackConformance(h.ROOT).offline(raw)
    assert result["valid"] and len(result["sources"]) == 4
    assert {s["substances"]["provider"] for s in pack["sources"]} == set(PROVIDER_CONTRACTS)
    comptox = next(s for s in pack["sources"] if s["substances"]["provider"] == "comptox")
    assert comptox["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_COMPTOX_API_KEY"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"licence", "terms_url", "attribution", "rate_limits", "revision_behaviour", "authentication"} <= set(
            contract)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert "synthesis" in EXCLUDED_FIELDS and "preparation" in EXCLUDED_FIELDS
    assert "fixture-credential" not in json.dumps(pack) and "x-api-key" not in json.dumps(pack)


def test_selections_are_explicit_and_checked():
    source = copy.deepcopy(h.manifest()["sources"][0])
    provider, entries = selection_entries(source)
    assert entries
    bad = copy.deepcopy(source)
    bad["substances"]["selection"][0]["cas"] = "80-05-8"
    with pytest.raises(SourcePackError) as caught:
        selection_entries(bad)
    assert caught.value.code == "invalid_manifest"
    moved = copy.deepcopy(source)
    moved["endpoint"] = "https://example.org/rest"
    with pytest.raises(SourcePackError):
        selection_entries(moved)


def test_pubchem_identity_synonyms_conflicts_and_exclusions(env):
    result = env.run(source_ids=["pubchem-compounds"])
    assert result["status"] == "complete"
    outcomes = env.store.runs(NS)[0]["outcomes"]
    assert [o["outcome"] for o in outcomes].count("not_found") == 1
    bpa = next(o for o in outcomes if o["selection"].get("cid") == 6623)
    assert bpa["excluded_fields_dropped"] == ["properties:Preparation"]
    ids = env.store.identifiers(NS, "pubchem:cid:6623")
    by_scheme = {}
    for row in ids:
        by_scheme.setdefault(row["scheme"], set()).add(row["value"])
    assert by_scheme["cid"] == {"6623"} and by_scheme["inchikey"] == {"IISBACLAFKSPIT-UHFFFAOYSA-N"}
    assert by_scheme["cas"] == {"80-05-7", "27100-33-0"}  # depositors disagree: both kept, none chosen
    assert all(r["conflict"] for r in ids if r["scheme"] == "cas")
    assert by_scheme["ec"] == {"201-245-8"} and by_scheme["dtxsid"] == {"DTXSID7020182"}
    assert "BPA" in by_scheme["synonym"]
    (compound,) = _records(env, "substance", "pubchem", "pubchem:cid:6623")
    revision = env.store.revisions(NS, compound["record_id"])[0]
    assert revision["statement"]["source"]["record_version"] == "2026-09-12"
    assert revision["evidence_origin"] == "fixture" and revision["document_id"].startswith("spdoc:")
    assert not h.forbidden_keys([r["statement"] for r in env.store.revisions(NS, compound["record_id"])])


def test_echa_clp_atp_revisions_harmonised_vs_notified_and_group_entry(env):
    assert env.run(source_ids=["echa-clp-classifications"])["status"] == "complete"
    harmonised = [r for r in _records(env, "classification", "echa-clp", "echa:substance:100.001.133")
                  if r["record_key"].startswith("harmonised:")]
    (record,) = harmonised
    revisions = env.store.revisions(NS, record["record_id"])
    assert [r["effective_from"] for r in revisions] == ["2009-01-20", "2018-03-01"]
    assert [r["legal_act"]["celex"] for r in revisions] == ["32008R1272", "32016R1179"]
    assert revisions[0]["statement"]["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 2"
    assert revisions[1]["statement"]["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 1B"
    notified = [r for r in _records(env, "classification", "echa-clp", "echa:substance:100.001.133")
                if r["record_key"].startswith("notified:")]
    assert len(notified) == 2
    published = env.store.revisions(NS, notified[0]["record_id"])[0]["statement"]["as_published"]
    assert published["kind"] == "notified" and published["quoted"] and published["notifiers"] in {3120, 41}
    (group,) = [s for s in env.store.subjects(NS) if s["subject_key"] == "echa:index:082-001-00-6"]
    assert group["kind"] == "group"
    assert {r["scheme"] for r in env.store.identifiers(NS, group["subject_key"])} == {"index", "preferred-name"}
    assert not _records(env, "classification", "echa-clp", "echa:substance:100.000.304")  # no harmonised entry


def test_echa_reach_registrations_svhc_annex_xiv_and_xvii(env):
    assert env.run(source_ids=["echa-reach-lists"])["status"] == "complete"
    (svhc,) = _records(env, "candidate_listing", subject="echa:substance:100.001.133")
    events = env.store.revisions(NS, svhc["record_id"])
    assert [(r["event"], r["effective_from"]) for r in events] == [("inclusion", "2017-01-12"),
                                                                   ("amendment", "2017-07-07")]
    assert events[0]["statement"]["as_published"]["reason"] == "Toxic for reproduction (Article 57c)"
    (xiv,) = _records(env, "authorisation", subject="echa:substance:100.003.829")
    published = env.store.revisions(NS, xiv["record_id"])[0]["statement"]["as_published"]
    assert (published["sunset_date"], published["latest_application_date"]) == ("2015-02-21", "2013-08-21")
    (xvii,) = _records(env, "restriction", subject="echa:substance:100.001.133")
    revision = env.store.revisions(NS, xvii["record_id"])[0]
    assert revision["statement"]["as_published"]["conditions"].startswith("Shall not be placed on the market in "
                                                                          "thermal paper")
    assert revision["legal_act"]["celex"] == "32016R2235" and revision["effective_from"] == "2020-01-02"
    assert revision["statement"]["source"]["locator"] == "/entries/0/events/0"
    (reg,) = _records(env, "registration", subject="echa:substance:100.000.304")
    reg_published = env.store.revisions(NS, reg["record_id"])[0]["statement"]["as_published"]
    assert "dossierContents" not in json.dumps(reg_published) and reg_published["status"] == "Active"
    outcome = next(o for o in env.store.runs(NS)[0]["outcomes"] if o["selection"]["echa_id"] == "100.000.304")
    assert outcome["excluded_fields_dropped"] == ["registrations:dossierContents"]


def test_removals_and_amendments_are_new_dated_records_and_history_stays(env):
    assert env.run(source_ids=["echa-reach-lists"])["status"] == "complete"
    assert env.run("second", source_ids=["echa-reach-lists"], overrides=h.LATER["second"])["status"] == "complete"
    assert env.run("third", source_ids=["echa-reach-lists"], overrides=h.LATER["third"])["status"] == "complete"
    (svhc,) = _records(env, "candidate_listing", subject="echa:substance:100.000.526")
    assert [r["event"] for r in env.store.revisions(NS, svhc["record_id"])] == ["inclusion", "removal"]
    (xiv,) = _records(env, "authorisation", subject="echa:substance:100.003.829")
    assert [r["legal_act"]["celex"] for r in env.store.revisions(NS, xiv["record_id"])] == ["32011R0143",
                                                                                           "32020R0171"]
    # A later response without the entry never deletes it (absence is not removal).
    assert env.run("fourth", source_ids=["echa-reach-lists"])["status"] == "complete"
    assert len(env.store.revisions(NS, svhc["record_id"])) == 2


def test_comptox_data_points_quoted_with_data_version_and_secret_ref(env):
    assert env.run(source_ids=["comptox-toxval"])["status"] == "complete"
    points = _records(env, "data_point", subject="comptox:dtxsid:DTXSID7020182")
    assert len(points) == 2
    statement = env.store.revisions(NS, points[0]["record_id"])[0]["statement"]
    assert statement["label"] == DATA_POINT_LABEL
    assert {"endpoint", "value", "unit", "study_reference", "data_source"} <= set(statement["as_published"])
    assert statement["source"]["data_version"].startswith("ToxValDB")
    assert not _records(env, "data_point", subject="comptox:dtxsid:DTXSID9020584")
    source = next(s for s in env.runtime._manifest(env.value["pack_id"])[0]["sources"]
                  if s["source_id"] == "comptox-toxval")
    adapter = SubstanceSourceAdapter(source, transport=fixture_transport(h.pages("comptox-toxval")), secret=None)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "substances", "parameters": {}}, cursor=None)
    assert caught.value.code == "authentication_failed"


def test_failures_are_classified_and_nothing_partial_is_stored(env):
    native = h.pages("echa-clp-classifications")
    native[1]["status"] = 503
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    source = next(s for s in installed["sources"] if s["source_id"] == "echa-clp-classifications")
    adapters = {"echa-clp-classifications": env.runtime.factory.compile(source, transport=fixture_transport(native))}
    result = env.run("broken", source_ids=["echa-clp-classifications"], adapters=adapters)
    assert result["status"] != "complete"
    assert env.store.runs(NS)[-1]["status"] == "failed"
    drift = h.pages("echa-reach-lists")
    drift[2]["body"] = {"entries": [{"entryId": "x", "events": [{"type": "rebranded", "date": "2020-01-01"}]}]}
    source = next(s for s in installed["sources"] if s["source_id"] == "echa-reach-lists")
    adapter = SubstanceSourceAdapter(source, transport=fixture_transport(drift))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "substances", "parameters": {}}, cursor=None)
    assert caught.value.code == "schema_drift"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "substances", "parameters": {"q": "all"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
