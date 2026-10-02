"""Offline device-to-regulatory-history acceptance for the Clinical Evidence ``clinical.devices`` provider (MD13).

The pinned ``clinical-evidence`` 0.1.4 fixtures for the nine medical-devices sources (openFDA 510(k), PMA,
classification, recall and MAUDE; AccessGUDID; EUDAMED actors, devices and certificates) replay through the real
source-pack runtime (fixture adapters compiled from the installed pack) with sockets blocked and the
``medical-devices-fda``, ``medical-devices-gudid`` and ``medical-devices-eudamed`` features selected. A device and a
manufacturer reach cited clearances, supplements, recalls and EU certificates as of a date, adverse-event report
counts with caveats, reviewable identity across FDA, GUDID and EUDAMED, cross-pack links and monitors; a subject
with no record says so. Every device, organisation and report is fictional; personal fields exist only in the
native responses and none survives acquisition. Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.medical_devices_sources import (
    MAUDE_CAVEATS,
    MedicalDevicesAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.medical_devices_identity import MedicalDeviceIdentity
from src.kb.medical_devices_links import MedicalDeviceLinks
from src.kb.medical_devices_monitoring import MedicalDeviceMonitor
from src.kb.medical_devices_queries import MedicalDeviceQueries
from src.kb.medical_devices_records import (
    MedicalDeviceStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import medical_devices_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "clinical-evidence"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
# Personal data present only in the native fixture responses; none may survive acquisition (MD01).
PERSONAL = ("Jane Fictional", "1 Example Way", "1 EXAMPLE WAY", "01999", "5550100", "support@example.invalid",
            "x@example.invalid", "Erika", "NURSE", "patient_age", "Female")
EXCLUDED_WORDS = ("safety signal", "causal", "incidence rate", "we recommend", "is safe", "is unsafe")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


class Env:
    def __init__(self) -> None:
        self.conn = h.connection()
        self.clock = 4_102_444_800_000
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select(PACK_ID, bundles[PACK_ID]["version"],
                           features=["medical-devices-fda", "medical-devices-gudid", "medical-devices-eudamed"])
        coordinator.activate("medical-devices-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source_id in h.SOURCES:
            runtime.accept_license(PACK_ID, source_id, principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "records", "source_ids": list(h.SOURCES),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in h.SOURCES},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: None)


def v2_adapters() -> dict:
    return {s: MedicalDevicesAdapter(h.source(s), transport=fixture_transport(h.native_pages(s, v2=True)))
            for s in h.V2}


def _without_disclaimers(value):
    """An answer without the sentences that state the exclusions and caveats themselves."""
    skip = {"boundary", "exclusions", "caveats", "count_semantics", "label", "note", "notice", "message"}
    if isinstance(value, dict):
        return {k: _without_disclaimers(v) for k, v in value.items() if k not in skip}
    if isinstance(value, list):
        return [_without_disclaimers(v) for v in value]
    return value


def personal_data_anywhere(conn) -> list[str]:
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_device_and_manufacturer_to_cited_clearances_recalls_and_report_counts_with_caveats():
    env = Env()
    assert all(feature_enabled(env.conn, f) for f in ("medical-devices-fda", "medical-devices-gudid",
                                                      "medical-devices-eudamed"))
    first = env.run("medical-devices")
    assert first["status"] == "complete", first
    store = MedicalDeviceStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 20
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # MD01: nothing personal reached documents, records or receipts
    ready = readiness(env.conn, h.NS)
    assert all(ready["features"].values()) and {s["live"] for s in ready["sources"].values()} == {"unverified-live"}

    # --- reviewable identity across FDA, GUDID and EUDAMED; manufacturer to Corporate Ownership --------------
    entities = h.seed_ownership(env.conn)
    identity = MedicalDeviceIdentity(env.conn)
    proposed = identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert {c["state"] for c in proposed["candidates"]} == {"proposed"}  # nothing auto-accepted
    for item in proposed["candidates"]:
        identity.review(h.NS, item["candidate_id"], "reject" if item["low_evidence"] else "accept",
                        "name or product code only" if item["low_evidence"] else "published identifiers agree",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    assert set(identity.device_cluster(h.NS, h.GUDID_PUMP, scopes=h.SCOPES)) == {h.GUDID_PUMP, h.PUMP_CLEARANCE,
                                                                                h.EU_DEVICE}
    unmatched = {u["key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)}
    assert "medical-devices:eudamed:actor:DE-MF-000099901" in unmatched and h.GUDID_PUMP not in unmatched

    # --- cross-pack links by identifier only; missing packs reported ----------------------------------------
    seeded = h.seed_clinical(env.conn)
    h.seed_safety(env.conn)
    links = MedicalDeviceLinks(env.conn).link(h.NS, scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                                              safety_namespace=h.PRODUCTS_NS)["links"]
    targets = {(link["record_key"], link["target_pack"], link["status"]) for link in links}
    assert (h.RECALL, "products", "linked") in targets
    assert (f"medical-devices:gudid:di:{h.LEAD_DI}", "clinical-evidence", "linked") in targets
    assert any(link["target_key"] == seeded["trial"] for link in links)
    assert {link["target_key"] for link in links if link["target_pack"] == "corporate-ownership"} == {
        entities["exampla"]}

    # --- a device's regulatory history as of a date, each event cited, jurisdictions apart -----------------
    ask = MedicalDeviceQueries(env.conn)
    history = ask.regulatory_history(h.NS, h.PUMP_DI, "2025-08-15", scopes=h.SCOPES)
    us, eu = history["jurisdictions"]["US"], history["jurisdictions"]["EU"]
    assert [c["k_number"] for c in us["clearances"]] == ["K999001"]
    assert [(r["recall_number"], r["recall_class"], r["status_as_published"]) for r in us["recalls"]] == [
        ("Z-9901-2025", "Class II", "Open, Classified")]
    assert [c["status_as_published"] for c in eu["certificates"]] == ["Valid"]
    assert set(history["gaps"]["eudamed_modules"]) >= {"vigilance-post-market-surveillance"}
    assert history["gaps"]["features_not_selected"] == []
    lead = ask.regulatory_history(h.NS, "P999001", "2025-12-31", scopes=h.SCOPES)
    assert [s["supplement_number"] for s in lead["jurisdictions"]["US"]["supplements"]] == ["S001", "S002"]

    # --- report counts per device: reports, caveats, window and revisions -----------------------------------
    counts = ask.adverse_event_counts(h.NS, h.PUMP_DI, "2025-01-01", "2025-06-30", scopes=h.SCOPES)
    assert counts["published_counts"][0]["counts_as_published"] == [{"event_type": "Malfunction", "reports": 2},
                                                                     {"event_type": "Injury", "reports": 1}]
    assert counts["caveats"] == list(MAUDE_CAVEATS) and counts["sources"]
    assert "never incidence" in counts["count_semantics"]

    # --- monitors: new, revised and unchanged -----------------------------------------------------------------
    monitor = MedicalDeviceMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "pump", watch="device", key=h.PUMP_DI, principal_id="analyst", scopes=h.SCOPES)
    lead_watch = monitor.create(h.NS, "lead", watch="device", key="P999001", principal_id="analyst",
                                scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    initial = monitor.run(watch["subscription_id"], principal_id="analyst", scopes=h.SCOPES)
    assert sorted(n["kind"] for n in initial["notifications"]) == ["certificate_published", "clearance_published",
                                                                  "recall_published"]
    monitor.run(lead_watch["subscription_id"], principal_id="analyst", scopes=h.SCOPES)
    replay = env.run("medical-devices-replay")
    assert replay["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    assert monitor.run(watch["subscription_id"], principal_id="analyst", scopes=h.SCOPES)["notifications"] == []

    # --- the publishers revise: recall terminated, certificate suspended, supplement S003, a removal ---------
    later = env.run("medical-devices-v2", adapters=v2_adapters())
    assert later["status"] == "complete", later
    SubscriptionStore(env.conn).commit_watermark(h.NS, 3)
    kinds = {n["kind"]: n for n in monitor.run(watch["subscription_id"], principal_id="analyst",
                                               scopes=h.SCOPES)["notifications"]}
    assert set(kinds) == {"recall_status_changed", "certificate_status_changed"}
    assert all(n["cites"]["previous_revision_id"] for n in kinds.values())
    assert [n["kind"] for n in monitor.run(lead_watch["subscription_id"], principal_id="analyst",
                                           scopes=h.SCOPES)["notifications"]] == ["supplement_published"]
    revised = ask.regulatory_history(h.NS, h.PUMP_DI, "2025-12-31", scopes=h.SCOPES)
    assert [r["status_as_published"] for r in revised["jurisdictions"]["US"]["recalls"]] == ["Terminated"]
    assert [c["status_as_published"] for c in revised["jurisdictions"]["EU"]["certificates"]] == ["Suspended"]
    before = ask.regulatory_history(h.NS, h.PUMP_DI, "2025-08-15", scopes=h.SCOPES)  # still answers as of then
    assert [r["status_as_published"] for r in before["jurisdictions"]["US"]["recalls"]] == ["Open, Classified"]
    assert [v["change"] for v in store.history(h.NS, h.RECALL, scopes=h.SCOPES)] == ["new", "revised"]
    removed = store.history(h.NS, "medical-devices:fda:510k:K999002", scopes=h.SCOPES)
    assert [v["publication_state"] for v in removed] == ["published", "not-published"]

    # --- a subject with no records; the exclusions hold in every answer ---------------------------------------
    nothing = ask.regulatory_history(h.NS, "ZZQ", "2025-12-31", scopes=h.SCOPES)
    assert nothing["on_record"] is False and "says nothing about the device" in nothing["message"]
    bundle = ask.evidence_bundle(h.NS, h.PUMP_DI, "2025-12-31", scopes=h.SCOPES, received_from="2025-01-01",
                                 received_to="2025-06-30")
    assert bundle["items"] and all(i["record_revision"]["revision_id"] and i["as_of"]["observed_at_ms"]
                                   for i in bundle["items"])
    for answer in (history, lead, counts, revised, nothing, bundle):
        assert forbidden_keys(answer) == []
        text = json.dumps(_without_disclaimers(answer)).lower()
        assert not [w for w in EXCLUDED_WORDS if w in text]  # only the stated exclusions name these
    assert personal_data_anywhere(env.conn) == []
