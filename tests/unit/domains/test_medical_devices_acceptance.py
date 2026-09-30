"""Offline device-to-regulatory-history acceptance for the Clinical Evidence medical-devices features (MD13, #2718).

The pinned ``clinical-evidence`` 0.1.4 fixtures for the seven openFDA, one
AccessGUDID and three EUDAMED sources replay through the real source-pack runtime
(fixture adapters compiled from the installed pack) with sockets blocked and the
``medical-devices-fda``, ``medical-devices-gudid`` and ``medical-devices-eudamed``
features selected. A device and a manufacturer reach cited clearances, an
approval with its supplements, recalls, EU registrations and certificates as of a
date, reviewable identity across FDA, GUDID and EUDAMED, cross-pack links and
report counts with MAUDE's caveats; a product code with no records is
``none_on_record``. Every company and device is fictional; contact persons,
addresses and patient blocks exist only in the native responses, which the
parsers discard. Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.medical_devices_sources import (
    FIXTURE_SECRET,
    MAUDE_CAVEATS,
    MedicalDevicesAdapter,
)
from src.ingestion.medical_devices_sources import fixture_transport as device_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.medical_devices_identity import MedicalDevicesIdentity
from src.kb.medical_devices_links import MedicalDevicesLinks
from src.kb.medical_devices_monitoring import MedicalDevicesMonitor
from src.kb.medical_devices_queries import MedicalDevicesQueries
from src.kb.medical_devices_records import (
    EXCLUSIONS,
    FEATURES,
    MedicalDevicesStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import medical_devices_fixture_builder as fb
from tests.unit import medical_devices_harness as h
from tests.unit.composition.test_migration import _migrated

PUBLIC_DNS = lambda _host: ["8.8.8.8"]
EXCLUDED_WORDS = ("safety signal", "incidence", "causal", "risk score", "we recommend", "clinical advice:")
V2_SOURCES = ("devices-fda-pma", "devices-fda-recalls", "devices-fda-enforcement", "devices-gudid-identifiers",
              "devices-eudamed-certificates")


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
        coordinator.select("clinical-evidence", bundles["clinical-evidence"]["version"], features=list(FEATURES))
        coordinator.activate("medical-devices-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for item in manifest["sources"]:
            runtime.accept_license(h.PACK_ID, item["source_id"], principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(h.PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": h.PACK_ID, "run_key": key, "operation": "records", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def v2_adapter(source_id: str) -> MedicalDevicesAdapter:
    return MedicalDevicesAdapter(h.source(source_id), transport=device_transport(h.native_pages(source_id, "v2")),
                                 secret=FIXTURE_SECRET)


def personal_data_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds a placeholder contact person, address or patient field."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in fb.PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_device_and_manufacturer_to_cited_regulatory_history_with_revisions_identity_links_and_counts():
    env = Env()
    assert all(feature_enabled(env.conn, f) for f in FEATURES)
    first = env.run(h.SOURCES, "medical-devices")
    assert first["status"] == "complete", first
    store = MedicalDevicesStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 19
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # MD01: nothing personal reached documents, records or receipts
    h.load_ownership(env.conn)
    h.load_product_safety(env.conn)
    h.load_clinical(env.conn)
    ask = MedicalDevicesQueries(env.conn)

    # --- reviewable identity across FDA, GUDID and EUDAMED (proposed, then reviewed; nothing automatic) ---------
    identity = MedicalDevicesIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert all(c["state"] == "proposed" for c in proposed["candidates"])
    (udi,) = [c for c in proposed["candidates"] if c["method"] == "udi-di"]
    before_review = ask.regulatory_history(h.NS, h.FIXTURE_DI, scopes=h.SCOPES, as_of="2099-12-31")
    assert before_review["jurisdictions"]["EU"]["devices"] == []  # no accepted match yet: EU stays apart
    identity.review(h.NS, udi["candidate_id"], "accept", "same GS1 UDI-DI", principal_id="reviewer",
                    scopes=h.REVIEW_SCOPES)
    for candidate in identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES,
                                      ownership_namespace=h.OWN_NS)["candidates"]:
        if candidate["state"] == "proposed" and candidate["method"] in {"accepted-device-match", "premarket-number"}:
            identity.review(h.NS, candidate["candidate_id"], "accept", "identifiers agree", principal_id="reviewer",
                            scopes=h.REVIEW_SCOPES)
    unmatched = {u["record_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)["unmatched"]}
    assert f"medical-devices:gudid:di:{h.EXAMPLE_DI}" in unmatched  # no EU registration: stays unmatched

    # --- device -> regulatory history as of a date, jurisdictions apart, each event cited ---------------------
    device = ask.regulatory_history(h.NS, h.FIXTURE_DI, scopes=h.SCOPES, as_of="2099-12-31")
    us, eu = device["jurisdictions"]["US"], device["jurisdictions"]["EU"]
    assert [e["k_number"] for e in us["clearances"]] == ["K999902"]
    assert [e["certificate_number"] for e in eu["certificates"]] == [fb.CERTIFICATE]
    assert [e["srn"] for e in eu["actors"]] == [fb.SRN]
    assert all(e["citation"]["revision_id"] for b in (*us.values(), *eu.values()) for e in b)
    manufacturer = ask.regulatory_history(h.NS, fb.SRN, scopes=h.SCOPES, as_of="2099-12-31")
    assert [e["basic_udi_di"] for e in manufacturer["jurisdictions"]["EU"]["devices"]] == [fb.BASIC_UDI_DI]
    fda_maker = ask.regulatory_history(h.NS, "medical-devices:fda:manufacturer:example-medical-devices:us",
                                       scopes=h.SCOPES, as_of="2099-12-31")
    assert {e["k_number"] for e in fda_maker["jurisdictions"]["US"]["clearances"]} == {"K999901"}
    assert [e["pma_number"] for e in fda_maker["jurisdictions"]["US"]["approvals"]] == ["P999901"]
    valve = ask.regulatory_history(h.NS, "ZZB", scopes=h.SCOPES, as_of="2099-06-30")
    (approval,) = valve["jurisdictions"]["US"]["approvals"]
    assert [s["supplement_number"] for s in approval["supplements"]] == ["S001", "S002"]
    pump = ask.regulatory_history(h.NS, h.EXAMPLE_DI, scopes=h.SCOPES, as_of="2099-05-01")
    assert {(e["source_id"], e["status_as_published"]) for e in pump["jurisdictions"]["US"]["recalls"]} == {
        ("devices-fda-recalls", "Open, Classified"), ("devices-fda-enforcement", "Ongoing")}

    # --- cross-pack links by citation and accepted matches ------------------------------------------------------
    linked = MedicalDevicesLinks(env.conn, now=env.now).link(
        h.NS, principal_id="alice", scopes=h.SCOPES, products_namespace=h.PRODUCTS_NS, clinical_namespace=h.NS,
        ownership_namespace=h.OWN_NS)
    status = {r["kind"]: r["status"] for r in linked["results"]}
    assert status["product-safety"] == status["medicines"] == status["trial"] == "linked"
    assert status["ownership"] == "none_linked"  # no name+country match was accepted
    assert all(link["record_revision_id"] and link["target_revision"] for r in linked["results"] for link in r["links"])

    # --- adverse-event report counts with caveats, never rates -------------------------------------------------
    counts = ask.adverse_event_counts(h.NS, h.EXAMPLE_DI, scopes=h.SCOPES, window_from="2099-01-01",
                                      window_to="2099-06-30")
    assert counts["unit"] == "reports" and counts["caveats"] == list(MAUDE_CAVEATS)
    assert [c["reports_in_period_as_published"] for c in counts["published_counts"]] == [17, 12]
    assert counts["reports_on_record"]["total_reports"] == 3 and counts["source_revisions"]

    # --- later publications are revisions a monitor reports; unchanged adds nothing -----------------------------
    monitor = MedicalDevicesMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "pump", watch="device", key=h.EXAMPLE_DI, principal_id="alice", scopes=h.SCOPES)
    valve_watch = monitor.create(h.NS, "valve", watch="product-code", key="ZZB", principal_id="alice",
                                 scopes=h.SCOPES)
    cert_watch = monitor.create(h.NS, "maker", watch="manufacturer", key=fb.SRN, principal_id="alice",
                                scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    for sub in (watch, valve_watch, cert_watch):
        assert monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    revised = env.run(V2_SOURCES, "medical-devices-v2", adapters={s: v2_adapter(s) for s in V2_SOURCES})
    assert revised["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    kinds = {n["kind"] for sub in (watch, valve_watch, cert_watch)
             for n in monitor.run(sub["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]}
    assert {"recall_status_changed", "device_version_published", "supplement_published", "supplement_listed",
            "certificate_status_changed"} <= kinds
    SubscriptionStore(env.conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    chain = store.history(h.NS, "medical-devices:eudamed:certificate:9999-MDR-0001", scopes=h.SCOPES)
    assert [(e["change"], e["record"]["fields"]["status"]) for e in chain] == [("new", "Valid"),
                                                                             ("revised", "Suspended")]
    suspended = ask.regulatory_history(h.NS, fb.SRN, scopes=h.SCOPES, as_of="2099-09-01")
    assert suspended["jurisdictions"]["EU"]["certificates"][0]["status_as_published"] == "Suspended"
    earlier = ask.regulatory_history(h.NS, fb.SRN, scopes=h.SCOPES, as_of="2099-07-01")
    assert earlier["jurisdictions"]["EU"]["certificates"][0]["status_as_published"] == "Valid"

    # --- a subject with no records, exclusions and minimisation, idempotent replay ------------------------------
    none = ask.regulatory_history(h.NS, "ZZC", scopes=h.SCOPES, as_of="2099-12-31")
    assert none["status"] == "none_on_record"
    for answer in (device, manufacturer, valve, pump, counts, none):
        assert forbidden_keys(answer) == [] and answer["exclusions"] == list(EXCLUSIONS)
        text = json.dumps({k: v for k, v in answer.items() if k not in {"exclusions", "caveats", "notice"}}).lower()
        assert not any(word in text for word in EXCLUDED_WORDS)
        assert not [p for p in fb.PERSONAL if p in json.dumps(answer)]  # (the EUDAMED paths say placeholder)
    revisions = env.conn.execute("SELECT count(*) FROM medical_device_revisions").fetchone()[0]
    replay = env.run(h.SOURCES, "medical-devices-replay")
    assert replay["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM medical_device_revisions").fetchone()[0] == revisions
    assert personal_data_anywhere(env.conn) == []


def test_offline_and_live_evidence_are_reported_separately():
    env = Env()
    env.run(["devices-eudamed-certificates"], "eudamed-only")
    state = readiness(env.conn)
    assert state["enabled"] == dict.fromkeys(FEATURES, True)
    assert all(p["live"] != "verified-live" for p in state["providers"].values())
    assert state["providers"]["eudamed"]["records"] == 1
    assert state["providers"]["eudamed"]["evidence_origins"] == ["fixture"]
    evidence = (h.ROOT / "docs/development/medical-devices-evidence/README.md").read_text()
    assert "Live evidence: none yet" in evidence


def test_with_the_features_disabled_the_clinical_bundle_is_unchanged():
    conn = h.connection()
    _, coordinator, _, _ = _migrated(conn)
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    bound = {b["provider"] for b in coordinator.active()["plan"]["bindings"]}
    assert "clinical.devices" not in bound
