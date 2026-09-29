"""H11: monitoring through knowledge subscriptions; changes make strength views stale; no new scheduler."""

import copy
import json

from src.kb.clinical_evidence import EvidenceMapService
from src.kb.clinical_monitoring import ClinicalMonitor, refresh_schedule
from tests.unit.clinical.harness import NS, Env, load

SEARCH_P2 = ("/api/v2/studies?countTotal=true&pageSize=2&pageToken=FIXTURE-TOKEN-2&query.cond=type+2+diabetes"
             "&query.intr=noetiglutide")
LABEL = "/drug/label.json?search=openfda.generic_name%3A%22noetiglutide%22&limit=3"


def _changed_t3():
    page = load("ctgov_search_page2.json")
    study = copy.deepcopy(page["studies"][0])
    status = study["protocolSection"]["statusModule"]
    status.update(overallStatus="COMPLETED", lastUpdatePostDateStruct={"date": "2026-09-26", "type": "ACTUAL"},
                  resultsFirstPostDateStruct={"date": "2026-09-26", "type": "ACTUAL"})
    study["hasResults"] = True
    study["resultsSection"] = {"outcomeMeasuresModule": {"outcomeMeasures": [
        {"type": "PRIMARY", "title": "Change From Baseline in HbA1c", "timeFrame": "Week 40",
         "groups": [], "classes": [], "analyses": []}]}}
    return {**page, "studies": [study]}


def _new_publication_and_retraction(env):
    from src.ingestion.crossref_notices import CrossrefNoticeCollection
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.connectors.paper import trial_registry
    from services.ingest.common.document_model import Document

    doc = Document(document_id="paper:fixture-noetic3-results", source_type="paper", source_id="pubmed",
                   language="en", ingested_at=env.clock, url="https://pubmed.ncbi.nlm.nih.gov/99000005/",
                   title="NOETIC-3 results (fixture)", content="The primary outcome was change in HbA1c at week 40.",
                   metadata={"source_api": "pubmed", "external_id": "99000005", "doi": "10.5555/noetic3.2026"})
    payload = trial_registry.with_registry_identifiers(doc, [{"kind": "nct", "value": "NCT09000003",
                                                              "declared_by": "pubmed-databank:ClinicalTrials.gov"}])
    assert not DocumentStore(env.conn).upsert([payload]).invalid
    env.documents["pubmed:99000005"] = doc.document_id
    raw = json.dumps({"message": {"items": [{
        "DOI": "10.5555/noetic1.2019.retraction", "title": ["Retraction (fixture)"], "language": "en",
        "update-to": [{"DOI": "10.5555/noetic1.2019", "type": "retraction", "source": "publisher",
                       "record-id": "fixture-2", "updated": {"date-parts": [[2026, 9, 20]]}}]}],
        "next-cursor": "c2"}}).encode()
    CrossrefNoticeCollection(env.conn, "clinical-fixture-notices-2", from_date="2026-09-01", until_date="2026-09-30",
                             targets={"10.5555/noetic1.2019": env.documents["pubmed:99000001"]}, rows=20, max_pages=1,
                             transport=lambda **_: {"status": 200, "content": raw}).step()
    for (notice_document,) in env.conn.execute("SELECT notice_document_id FROM crossref_notices").fetchall():
        env.documents["notice:" + notice_document] = notice_document


def _setup():
    env = Env()
    env.journey()
    view = env.build_map()
    monitor = ClinicalMonitor(env.conn, now=env.now)
    created = monitor.create(NS, view["view_id"], "watch-1", principal_id="alice", scopes=env.scopes())
    first = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=env.scopes())
    return env, view, monitor, created, first


def test_monitor_fires_on_each_change_kind_citing_revisions_and_views_go_stale():
    env, view, monitor, created, first = _setup()
    assert first["status"] == "evaluated" and {n["kind"] for n in first["notifications"]} == {"map_membership"}
    env.web.set(SEARCH_P2, _changed_t3())
    env.web.set(LABEL, fixture="openfda_label_v8.json")
    env.acquire("r2", ["ctgov-question-search", "openfda-products"])
    _new_publication_and_retraction(env)
    env.link(observation="link-2")
    second = monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=env.scopes())
    kinds = {n["kind"] for n in second["notifications"]}
    assert {"trial_status_change", "new_registry_version", "results_posted", "label_revision",
            "new_linked_publication", "retraction"} <= kinds
    for notification in second["notifications"]:
        cite = notification["cites"]
        assert cite["record_id"] and cite["before_revision"] is not None and cite["after_revision"] is not None
    status = next(n for n in second["notifications"] if n["kind"] == "trial_status_change")
    assert "recruiting -> completed" in status["message"]
    assert second["view_freshness"]["stale"] is True and second["next_step"] == "recompute the evidence map"
    stale = EvidenceMapService(env.conn).inspect(NS, view["view_id"], scopes=env.scopes())
    assert stale["freshness"]["stale"] is True
    reasons = {r for i in stale["freshness"]["invalidations"] for r in i["reasons"]}
    assert "status_change" in reasons
    # Replaying a committed watermark creates no new events.
    replay = monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=env.scopes())
    assert replay["status"] == "replayed" and replay["notifications"] == []
    recomputed = EvidenceMapService(env.conn, now=env.now).rebuild(NS, view["view_id"], principal_id="alice",
                                                                    scopes=env.scopes())
    assert recomputed["revision"] == 2 and recomputed["freshness"]["current"] is True
    t3 = next(t for t in recomputed["trials"] if t["identifier"] == "NCT09000003")
    assert t3["results"]["state"] == "posted" and t3["status"]["normalized"] == "completed"
    t1 = next(t for t in recomputed["trials"] if t["identifier"] == "NCT09000001")
    assert t1["retracted"] is True
    assert recomputed["strength"]["summary"]["rule"] == "S2"  # T3 is now the D1 trial with results


def test_failed_refresh_degrades_coverage_without_removing_anything():
    env, view, monitor, created, _ = _setup()
    env.web.set(SEARCH_P2, None, status=503)
    env.web.set("/api/v2/studies?countTotal=true&pageSize=2&query.cond=type+2+diabetes&query.intr=noetiglutide",
                None, status=503)
    env.acquire("r2", ["ctgov-question-search"])
    result = monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=env.scopes())
    assert result["coverage"] == {"complete": False, "stale_providers": ["ctgov"]}
    assert [n["kind"] for n in result["notifications"]] == ["stale_source"]
    assert EvidenceMapService(env.conn).inspect(NS, view["view_id"], scopes=env.scopes())["freshness"]["current"]


def test_refreshes_run_through_the_source_pack_schedule_not_a_new_scheduler():
    env, _, _, created, _ = _setup()
    assert created["refresh"]["schedule"] is None and created["refresh"]["source_pack"] == "clinical-evidence"
    env.runtime.set_schedule_owned("clinical-evidence", {"kind": "interval", "interval_s": 86400},
                                   principal_id="operator", owner="composition:clinical-evidence")
    schedule = refresh_schedule(env.conn)
    assert schedule["schedule"]["schedule"] == {"kind": "interval", "interval_s": 86400}
    assert "composition:clinical-evidence" in schedule["owners"]
    assert "adds no scheduler" in schedule["note"]
    tables = {r[0] for r in env.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert not {t for t in tables if t.startswith("clinical_") and ("schedul" in t or "job" in t)}
