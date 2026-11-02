"""ICIJ Offshore Leaks in Corporate Ownership (#2888): bounded import, quoting, persons, identity, dossier, no verdicts.

Everything runs offline over the authored fixture tests/fixtures/ownership/icij-offshore-leaks.json (fictional
nodes in the ICIJ CSV shape). This is offline evidence only; the source stays unverified-live.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import urllib.request
import zipfile

import duckdb
import pytest

from src.ingestion import offshore_leaks as ol
from src.ingestion.bulk import get_bulk_adapter
from src.ingestion.source_packs import (
    SourcePackError,
    replay_native_fixture,
    validate_source_pack,
)
from src.kb.offshore_leaks import (
    build_dossier_with_offshore_leaks,
    feature_enabled,
    project_bulk_subset,
    propose_identity,
)
from src.kb.ownership_dossier import export_dossier
from src.kb.ownership_graph import OwnershipGraph
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, ROOT, SCOPES, UK_KEYS

FIXTURE = json.loads((ROOT / "tests/fixtures/ownership/icij-offshore-leaks.json").read_text())
E1, E2, E3, E4, E9 = (ol.node_key(n) for n in ("9990000101", "9990000102", "9990000103", "9990000104",
                                                "9990000199"))
DOE, NOMINEES, ROE, NORTHWIND, POE = (ol.node_key(n) for n in ("9990000201", "9990000202", "9990000204",
                                                               "9990000301", "9990000302"))


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)


def source():
    pack = validate_source_pack(json.loads((ROOT / "config/source_packs/corporate-ownership.json").read_text()))
    return next(s for s in pack["sources"] if s["source_id"] == "icij-offshore-leaks")


def replayed():
    pages = replay_native_fixture(source(), FIXTURE)
    part = pages[0]["ownership_part"]
    return [r for p in pages for r in p["ownership_part"]["records"]], part["coverage"]


def by_key(records):
    return {r["record_key"]: r for r in records}


def csv_rows(name):
    page = next(p for p in FIXTURE["native_pages"] if p["request"].endswith("/" + name))
    return ol.read_csv(("\n".join(page["body"]) + "\n").encode(), ol.NODE_FILES.get(name, "relationship"), name)


# ------------------------------------------------------------------ mapping


def test_fixture_replays_offline_into_bounded_quoted_records():
    records, coverage = replayed()
    keyed = by_key(records)
    # Seeds, their relationships and the other ends only; unrelated nodes are never imported.
    assert {E1, E2, E3, E4, DOE, NOMINEES, ROE, NORTHWIND} <= set(keyed)
    assert E9 not in keyed and POE not in keyed
    assert coverage["seeds"] == ["9990000101", "9990000103", "9990000201"] and coverage["truncated"] is False
    # Kinds: entity nodes are legal entities; officers are persons unless a legal-form token says otherwise.
    assert keyed[E1]["kind"] == "legal_entity" and keyed[E1]["jurisdiction"] == "VG"
    assert keyed[NOMINEES]["kind"] == "legal_entity"
    assert "legal-form token 'LIMITED'" in keyed[NOMINEES]["native"]["classification_basis"]
    assert keyed[DOE]["kind"] == "person" and keyed[DOE]["owner_scoped"] is True
    # Roles quote ICIJ's label verbatim; leak roles are never ownership or control assertions.
    roles = {(r["officer"]["key"], r["entity_key"]): r for r in records if r["kind"] == "officer_role"}
    assert roles[(DOE, E1)]["role"] == "shareholder of" and roles[(DOE, E1)]["owner_scoped"] is True
    assert roles[(NOMINEES, E3)]["role"] == "Nominee Shareholder of"
    assert roles[(NORTHWIND, E1)]["native"]["rel_type"] == "intermediary_of"
    assert roles[(DOE, E2)]["appointed_on"] == "2011-06-01" and roles[(DOE, E2)]["resigned_on"] == "2014-01-15"
    assert not [r for r in records if r["kind"] == "ownership_assertion"]
    # Every record is cited to the leak dataset and node id and carries ICIJ's caveat.
    for item in records:
        assert item["source"]["provider"] == "icij-offshore-leaks"
        assert item["source"]["note"] == ol.CAVEAT and "legitimate uses" in item["source"]["note"]
        assert "|node:" in item["source"]["provider_record_id"] or "|rel:" in item["source"]["provider_record_id"]
        assert item["source"]["url"].startswith("https://offshoreleaks.icij.org/nodes/")
    assert keyed[E1]["source"]["provider_record_id"] == "Panama Papers|node:9990000101"
    # Unknowns stay unknown; published text is kept.
    assert keyed[E3]["founding_date"] is None and "founding_date" in keyed[E3]["unknowns"]
    assert keyed[E3]["native"]["incorporation_date_as_published"] == "sometime in 2014"
    assert keyed[E3]["jurisdiction"] is None and keyed[E3]["native"]["jurisdiction_as_published"] == "XXX"


def test_links_and_addresses_are_quoted_and_person_addresses_withheld():
    records, coverage = replayed()
    keyed = by_key(records)
    same = [link for link in keyed[E1]["native"]["icij_links"] if link["rel_type"] == "same_as"]
    assert same and same[0]["link"] == "same name and registration date as"
    assert same[0]["identity_basis"] == "source-stated-link" and same[0]["other_key"] == E4
    similar = [link for link in keyed[DOE]["native"]["icij_links"] if link["rel_type"] == "similar"]
    assert similar[0]["identity_basis"] == "similar-name"
    connected = [link for link in keyed[E3]["native"]["icij_links"] if link["rel_type"] == "connected_to"]
    assert connected and connected[0]["identity_basis"] is None
    assert [a["address"] for a in keyed[E1]["addresses"]] == ["PO Box 0001, Fixture Chambers, Road Town, Tortola",
                                                              "1 Fixture Lane, Road Town, Tortola"]
    assert coverage["person_addresses_withheld"] == 1
    assert "5 Example Street" not in json.dumps(records)


def test_dates_and_jurisdictions_are_never_guessed():
    assert ol.parse_date("23-MAR-2006") == "2006-03-23"
    assert ol.parse_date("2006") == "2006" and ol.parse_date("20060323") == "2006-03-23"
    assert ol.parse_date("about 2006") is None and ol.parse_date("") is None
    assert ol.jurisdiction_code("British Virgin Islands") == "VG"
    assert ol.jurisdiction_code("Undetermined") is None


def test_selection_is_always_bounded():
    for bad in ({}, {"datasets": ["Panama Papers"]}, {"node_ids": ["1"], "max_nodes": 5000},
                {"node_ids": [str(i) for i in range(201)]}, {"node_ids": ["1"], "hops": 2},
                {"node_ids": ["1"], "node_kinds": {"1": "trust"}}):
        with pytest.raises(SourcePackError):
            ol.validate_selection(bad)
    files = {name: csv_rows(name) for name in ol.FILES if name != "nodes-others.csv"}
    capped = ol.select_subgraph(lambda n: files.get(n, []), {"datasets": ["Panama Papers"], "max_nodes": 2})
    assert capped["truncated"] is True and len(capped["nodes"]) <= 2
    filtered = ol.select_subgraph(lambda n: files.get(n, []), {"jurisdictions": ["SEY"], "max_nodes": 10, "hops": 0})
    assert set(filtered["nodes"]) == {"9990000104"} and not filtered["relationships"]


def test_schema_drift_fails_closed():
    with pytest.raises(SourcePackError) as excinfo:
        ol.read_csv(b"node_id,title\n1,x\n", "entity", "nodes-entities.csv")
    assert excinfo.value.code == "schema_drift"


def test_no_verdict_is_ever_produced():
    records, coverage = replayed()
    assert ol.forbidden_keys(records) == [] and ol.forbidden_keys(coverage) == []
    for key in ("wrongdoing", "aml_flag", "sanctions", "risk_score", "verdict", "suspicious"):
        with pytest.raises(ol.OffshoreLeaksError) as excinfo:
            ol.guard({"records": [{"native": {key: True}}]})
        assert excinfo.value.code == "verdict_forbidden" and excinfo.value.paths


def test_person_research_mode_lifts_owner_scoping_under_a_policy(monkeypatch):
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    records, coverage = replayed()
    keyed = by_key(records)
    assert coverage["person_research"] is True
    assert not keyed[DOE].get("owner_scoped") and keyed[DOE]["native"]["person_policy"] == ol.PERSON_POLICY
    assert coverage["person_addresses_withheld"] == 1  # addresses stay withheld in the mode too
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH")
    with pytest.raises(ol.OffshoreLeaksError) as excinfo:
        ol.check_person_policy([keyed[DOE]])
    assert excinfo.value.code == "minimisation_violation"


def test_live_runs_are_refused_toward_the_bulk_runner():
    adapter = ol.OffshoreLeaksAdapter(source())
    with pytest.raises(SourcePackError) as excinfo:
        adapter.fetch_page({"operation": "ownership", "parameters": {}}, cursor=None)
    assert excinfo.value.code == "bulk_release_required"
    assert ol.LIVE_VERIFICATION["status"] == "unverified-live"


# --------------------------------------------- acquisition, identity, dossier


def acquired_env():
    env = harness.Env().ready()  # the default acquisition: the offshore-leaks feature is off
    service = OwnershipIdentityService(env.conn, now=env.now)
    proposed = service.propose(NS, principal_id=harness.PRINCIPAL, scopes=SCOPES,
                               market=harness.seed_market(env.conn), lei_namespace=NS)
    for item in proposed["candidates"]:
        if item["state"] != "proposed":
            continue
        decoy = harness.DECOY in (item["left_key"], item["right_key"])
        icij = any(k.startswith("icij-offshore-leaks:") for k in (item["left_key"], item["right_key"]))
        if not icij:
            service.review(NS, item["candidate_id"], "reject" if decoy else "accept", "fixture review",
                           principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)
    return env, service


def test_feature_is_off_by_default_and_the_default_acquisition_skips_it():
    env = harness.Env().ready()
    assert feature_enabled(env.conn) is False
    assert not OwnershipStore(env.conn).by_key(NS, E1, principal_id=harness.PRINCIPAL, scopes=SCOPES)


def test_entity_dossier_shows_offshore_links_cited_to_dataset_and_node():
    env, service = acquired_env()
    result = env.acquire("icij", source_ids=["icij-offshore-leaks"])
    assert result["statuses"] == {"ownership": "complete"}
    offered = propose_identity(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, now=env.now)
    bases = {(o["left_key"], o["right_key"]): o["basis"] for o in offered["offered"]}
    assert bases[tuple(sorted((E1, E4)))] == "source-stated-link"
    assert bases[tuple(sorted((E3, UK_KEYS["ch"])))] == "unqualified-identifier"
    assert offered["withheld"]["person_links"] == 1  # DOE ~ ROE stays unoffered without person-research mode
    # Nothing is accepted automatically.
    icij = [c for c in service.candidates(NS, scopes=SCOPES) if any(
        k.startswith("icij-offshore-leaks:") for k in (c["left_key"], c["right_key"]))]
    assert icij and {c["state"] for c in icij} == {"proposed"}
    match = next(c for c in icij if {c["left_key"], c["right_key"]} == {E3, UK_KEYS["ch"]})
    assert match["evidence"][0]["ibcRUC_as_published"] == "09990002"
    service.review(NS, match["candidate_id"], "accept", "registry number and name agree; reviewed",
                   principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)

    dossier = build_dossier_with_offshore_leaks(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL,
                                                scopes=SCOPES, as_of="2025-06-01", evidence_kind="offline-fixture")
    assert dossier["status"] == "assembled"
    section = dossier["offshore_leaks"]
    assert section["status"] == "assembled" and section["caveat"] == ol.CAVEAT
    [leaked] = section["records"]
    assert leaked["node_id"] == "9990000103"
    assert leaked["citation"]["dataset"] == "Paradise Papers - Fixture Registry"
    assert leaked["citation"]["provider_record_id"] == "Paradise Papers - Fixture Registry|node:9990000103"
    roles = {r["role_as_published"]: r for r in section["roles"]}
    assert set(roles) == {"Nominee Shareholder of", "Director"}
    assert roles["Nominee Shareholder of"]["node_ids"] == {"officer": "9990000202", "entity": "9990000103"}
    assert all(r["citation"]["record_id"] and r["caveat"] == ol.CAVEAT for r in section["roles"])
    assert any(link["rel_type"] == "connected_to" for link in section["quoted_links"])
    assert dossier["source_caveats"]["icij-offshore-leaks"] == ol.CAVEAT
    assert ol.forbidden_keys(section) == []
    # Leak roles never enter the parent or control graph.
    graph = OwnershipGraph(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    holders = {g["holder"] for g in graph.direct_parents(UK_KEYS["ch"], "2025-06-01")["groups"]}
    assert not any(h.startswith("icij-offshore-leaks:") for h in holders)

    exported = export_dossier(env.conn, dossier, "offshore", principal_id=harness.PRINCIPAL, scopes=SCOPES)
    from src.kb.authored_reports import AuthoredReportStore

    report = AuthoredReportStore(env.conn).inspect(NS, exported["report_id"], principal_id=harness.PRINCIPAL,
                                                   scopes=SCOPES)
    offshore = next(s for s in report["content"]["sections"] if s["id"] == "offshore-leaks")
    assert offshore["assertions"] and all(a["kind"] == "sourced" and a["citations"] and a["dependencies"]
                                          for a in offshore["assertions"])
    assert all("node" in a["text"] and ol.CAVEAT in a["text"] for a in offshore["assertions"])
    assert ol.CAVEAT in report["content"]["limitations"]

    # Another analyst sees that person-level links exist, not who they name.
    other = build_dossier_with_offshore_leaks(env.conn, NS, "lei", harness.UK, principal_id="other-analyst",
                                              scopes=SCOPES, as_of="2025-06-01")
    assert other["offshore_leaks"]["redacted_person_links"] == 1
    assert {r["role_as_published"] for r in other["offshore_leaks"]["roles"]} == {"Nominee Shareholder of"}
    assert "ROE, Taylor" not in json.dumps(other["offshore_leaks"])


def test_icij_similar_links_can_never_be_accepted(monkeypatch):
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    env = harness.Env().ready()
    env.acquire("icij", source_ids=["icij-offshore-leaks"])
    offered = propose_identity(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, now=env.now)
    similar = next(o for o in offered["offered"] if {o["left_key"], o["right_key"]} == {DOE, ROE})
    assert similar["basis"] == "similar-name"
    service = OwnershipIdentityService(env.conn, now=env.now)
    with pytest.raises(OwnershipError) as excinfo:
        service.review(NS, similar["candidate_id"], "accept", "names look alike", principal_id=harness.REVIEWER,
                       scopes=harness.REVIEW_SCOPES)
    assert excinfo.value.code == "insufficient_evidence"


# ------------------------------------------------------------------ bulk


def test_bulk_release_subset_projects_with_the_archive_digest(tmp_path):
    from tests.unit.ingestion.test_bulk_runner import FakeWeb, runner

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for page in FIXTURE["native_pages"]:
            archive.writestr("full-oldb/" + page["request"].rsplit("/", 1)[1], "\n".join(page["body"]) + "\n")
    body = buf.getvalue()
    web = FakeWeb({ol.BULK_URL: (body, {"ETag": "fx", "Last-Modified": "Mon, 02 Nov 2026 08:00:00 GMT"})})
    adapter = get_bulk_adapter("icij-offshore-leaks")
    with pytest.raises(ValueError):
        adapter.validate_params({"datasets": ["Panama Papers"]})  # a filter without max_nodes is unbounded
    receipt = runner(adapter, tmp_path, web).run({"node_ids": ["9990000101"]})
    assert receipt["status"] == "complete", receipt
    conn = duckdb.connect()
    scopes = SCOPES | {"operator"}
    result = project_bulk_subset(conn, NS, receipt["release_dir"], principal_id=harness.PRINCIPAL, scopes=scopes)
    assert result["counts"]["inserted"] > 0 and result["caveat"] == ol.CAVEAT
    view = OwnershipStore(conn).by_key(NS, E1, principal_id=harness.PRINCIPAL, scopes=scopes)
    assert view["record"]["source"]["raw_sha256"] == hashlib.sha256(body).hexdigest()
    assert view["record"]["source"]["revision"] == receipt["release_id"]
    assert OwnershipStore(conn).by_key(NS, E3, principal_id=harness.PRINCIPAL, scopes=scopes) is None  # not a seed
    again = project_bulk_subset(conn, NS, receipt["release_dir"], principal_id=harness.PRINCIPAL, scopes=scopes)
    assert again["counts"]["inserted"] == 0 and again["counts"]["revised"] == 0  # idempotent replay


# ------------------------------------------------------------ composition


def test_feature_resolves_against_existing_providers_only():
    from src.composition.adapter import adapt_all
    from src.composition.contracts import validate_composition_manifest
    from src.composition.resolver import resolve
    from src.composition.shadow import provider_descriptors

    manifest = json.loads((ROOT / "packs/corporate-ownership/manifest.json").read_text())
    feature = next(f for f in manifest["optional_features"] if f["id"] == "offshore-leaks")
    assert feature["default"] is False and "inclusion does not imply" in feature["description"]
    bundles = adapt_all()
    assert validate_composition_manifest(bundles["corporate-ownership"]) == []
    root = {"pack": "corporate-ownership", "version": bundles["corporate-ownership"]["version"],
            "features": ["offshore-leaks"]}
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    assert result.plan["features"]["corporate-ownership"] == ["offshore-leaks"]


def test_structure_dossier_offshore_leaks_section_is_cited_and_gated(monkeypatch):
    """The holding-structure dossier (#2890) picks up the ICIJ section through its extension point."""
    from src.kb import offshore_leaks_links
    from src.kb.ownership_structure_dossier import build_structure_dossier

    env, service = acquired_env()
    env.acquire("icij", source_ids=["icij-offshore-leaks"])
    propose_identity(env.conn, NS, principal_id=harness.PRINCIPAL, scopes=SCOPES, now=env.now)
    match = next(c for c in service.candidates(NS, scopes=SCOPES)
                 if {c["left_key"], c["right_key"]} == {E3, UK_KEYS["ch"]})
    service.review(NS, match["candidate_id"], "accept", "registry number and name agree; reviewed",
                   principal_id=harness.REVIEWER, scopes=harness.REVIEW_SCOPES)

    def build():
        return build_structure_dossier(env.conn, NS, "lei", harness.UK, principal_id=harness.PRINCIPAL,
                                       scopes=SCOPES, as_of="2025-06-01", include=("offshore_leaks",),
                                       sections={"offshore_leaks": {"namespace": NS}})

    assert build()["sections"]["offshore_leaks"]["status"] == "inert"  # the feature is off by default
    monkeypatch.setattr(offshore_leaks_links, "feature_enabled", lambda conn: True)
    section = build()["sections"]["offshore_leaks"]
    assert section["status"] == "assembled" and section["caveat"] == ol.CAVEAT
    assert any("9990000103" in line["statement"] for line in section["lines"])
    assert all(line["citation"]["dataset"] == "Paradise Papers - Fixture Registry" for line in section["lines"])
    assert all(line["data"]["caveat"] == ol.CAVEAT for line in section["lines"])
    assert ol.forbidden_keys(section) == []
