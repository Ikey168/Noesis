"""Offline medicines-regulation harness: the real runtime, adapters, record store, identity, links and monitors.

Provider responses come from the ``clinical-evidence`` source pack's authored
medicines fixtures (``tests/fixtures/source_packs/medicines-*.json``, built by
:mod:`tests.unit.medicines_fixture_builder`) plus the Clinical harness fixtures
(trials, FAERS counts) through an injected transport; RxNav answers come from
authored pages. Earlier and later revisions (SmPC revision 3, SPL version 7, a
DSC update, a discontinued US product) are served by swapping pages. Receipts
say ``execution: injected``; this is never live coverage.
"""

from __future__ import annotations

import json

from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical import harness
from tests.unit.clinical.harness import NS, PACK_FIXTURES

MEDICINES_SCHEMA = "noesis-clinical-medicines-record-v1"
SCOPES = harness.BASE_SCOPES | {"knowledge:clinical:review", "operator"}
REVIEWER = harness.BASE_SCOPES | {"knowledge:clinical:review"}
READ_ONLY = {"knowledge:clinical:read", f"namespace:{NS}:read"}


class Env(harness.Env):
    def __init__(self, path=None, now_iso="2026-09-27T09:00:00+00:00"):
        super().__init__(path, now_iso)
        for path_ in sorted(PACK_FIXTURES.glob("medicines-*.json")):
            for page in json.loads(path_.read_text())["native_pages"]:
                self.web.pages[page["request"]] = {"status": page.get("status", 200), "body": page["body"]}

    def acquire_medicines(self, run_key, source_ids=None):
        manifest, _ = self.runtime._manifest("clinical-evidence")
        selected = [s for s in manifest["sources"] if s["mapping"]["target_schema"] == MEDICINES_SCHEMA
                    and (not source_ids or s["source_id"] in source_ids)]
        adapters = {s["source_id"]: self.runtime.factory.compile(s, transport=self.web.transport) for s in selected}
        return self.runtime.run({"pack_id": "clinical-evidence", "run_key": run_key, "operation": "records",
                                 "source_ids": [s["source_id"] for s in selected], "max_pages": 50,
                                 "max_results": 500, "mode": "backfill", "backfill": {"from_ms": 0}},
                                principal_id="operator", adapters=adapters)

    # ------------------------------------------------------------ revisions

    def serve_earlier(self):
        """SmPC revision 3 and SPL version 7 (the revisions in force before the pinned ones)."""
        self.web.set(fb.EMA_EXPORT, fb.ema_export(3))
        self.web.set(fb.EMA_PI, fb.smpc_text(3))
        self.web.set(f"/dailymed/services/v2/spls/{fb.SET_ID}/history.json", fb.spl_history(7))
        self.web.set(f"/dailymed/services/v2/spls/{fb.SET_ID}.xml", fb.spl_xml(7))

    def serve_pinned(self):
        self.web.set(fb.EMA_EXPORT, fb.ema_export(4))
        self.web.set(fb.EMA_PI, fb.smpc_text(4))
        self.web.set(f"/dailymed/services/v2/spls/{fb.SET_ID}/history.json", fb.spl_history(8))
        self.web.set(f"/dailymed/services/v2/spls/{fb.SET_ID}.xml", fb.spl_xml(8))

    def serve_dsc_update(self):
        self.web.set(fb.DSC_PATH, fb.dsc_html(updated=True))

    def serve_discontinued(self):
        self.web.set(fb.DRUGSFDA_REQUEST, fb.drugsfda(discontinued=True))

    # ------------------------------------------------------------ identity, links

    def rxnav(self):
        from src.ingestion.clinical_providers import fixture_transport
        from src.ingestion.medicines_sources import RxNavClient

        return RxNavClient(transport=fixture_transport(fb.rxnav_pages()))

    def identity(self):
        from src.kb.clinical_terms import MedicineIdentity

        return MedicineIdentity(self.conn, now=self.now)

    def propose(self):
        return self.identity().propose(NS, principal_id="alice", scopes=SCOPES, client=self.rxnav())

    def match(self, subject_key, state=None):
        return next(m for m in self.identity().matches(NS, scopes=SCOPES, state=state)
                    if m["subject_key"] == subject_key)

    def accept_eu(self, principal_id="bob"):
        match = self.match("ema:EMEA/H/C/009001")
        return self.identity().review(NS, match["match_id"], "accept", "active substance noetiglutide is the RxNorm "
                                      "ingredient; EU product reviewed", principal_id=principal_id, scopes=REVIEWER)

    def link_medicines(self, observation="medicines-link-1"):
        from src.kb.clinical_publications import PublicationLinker

        return PublicationLinker(self.conn, now=self.now).link_medicines(NS, principal_id="alice", scopes=SCOPES,
                                                                         observation_id=observation)

    def service(self):
        from src.kb.clinical_medicines import MedicinesService

        return MedicinesService(self.conn, now=self.now)

    def journey(self):
        """Trials and FAERS counts, earlier then pinned label revisions, RxNorm identity, review, citation links."""
        self.acquire("r1")
        self.seed_publications()
        self.serve_earlier()
        first = self.acquire_medicines("m1")
        self.serve_pinned()
        second = self.acquire_medicines("m2")
        proposed = self.propose()
        self.accept_eu()
        linked = self.link_medicines()
        return {"first": first, "second": second, "proposed": proposed, "linked": linked}
