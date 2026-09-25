"""Normalize funding opportunities, revisions and application deadlines.

Every acquisition is kept as a *provider assertion*. Opportunities are keyed by
provider, record kind, source-native identifier and round, so different
rounds of one programme never merge and repeated acquisition of identical
content never creates a revision. When the authoritative content changes a new
opportunity revision is appended with a classified amendment list, and every
derived view registered against an older revision is invalidated.

Status is derived conservatively. A call is closed only when its provider
says so or its final submission deadline instant has passed; a call missing
from a later listing, or a failed refresh, makes its state *unconfirmed* and
its source stale, never closed. Deadlines retain the original text and
timezone; a date without a known timezone is not converted into an instant.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from src.kb.funding_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
    validate_record,
)

CONTRACT = "noesis-funding-opportunity-v1"
INGEST_SCOPE = "knowledge:ingestion:execute"
_AUTHORITY_RANK = {"funder": 0, "administering-body": 1, "directory": 2}
_CONFLICT_FIELDS = ("deadlines", "status", "instrument", "financial_terms", "requirements")


def _semantic(value):
    """Compare assertions by meaning: locators and native labels differ per page."""
    if isinstance(value, dict):
        return {k: _semantic(v) for k, v in value.items() if k not in {"locator", "native_label", "native", "text"}}
    if isinstance(value, list):
        return [_semantic(v) for v in value]
    return value


def _detail_rank(source_key):
    """Detail pages (topic/fund/programme) outrank search or directory listings."""
    return 0 if any(marker in source_key for marker in (":topic:", ":fund:", ":programme", "exist:")) else 1
_DDL = """
CREATE TABLE IF NOT EXISTS funding_provider_assertions(
 assertion_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, opportunity_id TEXT NOT NULL,
 source_key TEXT NOT NULL, record_hash TEXT NOT NULL, record_json TEXT NOT NULL,
 observation_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funding_opportunities(
 opportunity_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL,
 record_kind TEXT NOT NULL, provider_id TEXT NOT NULL, round_id TEXT,
 revision BIGINT NOT NULL, last_seen_ms BIGINT NOT NULL, listing_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS funding_opportunity_revisions(
 opportunity_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(opportunity_id, revision));
CREATE TABLE IF NOT EXISTS funding_opportunity_links(
 namespace TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL, kind TEXT NOT NULL,
 evidence_json TEXT NOT NULL, PRIMARY KEY(namespace, from_id, to_id, kind));
CREATE TABLE IF NOT EXISTS funding_listing_members(
 namespace TEXT NOT NULL, listing TEXT NOT NULL, opportunity_id TEXT NOT NULL,
 PRIMARY KEY(namespace, listing, opportunity_id));
CREATE TABLE IF NOT EXISTS funding_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_observation_id TEXT, last_execution TEXT, PRIMARY KEY(namespace, provider));
CREATE TABLE IF NOT EXISTS funding_view_dependencies(
 view_id TEXT NOT NULL, namespace TEXT NOT NULL, owner TEXT NOT NULL, opportunity_id TEXT NOT NULL,
 revision BIGINT NOT NULL, PRIMARY KEY(view_id, opportunity_id));
CREATE TABLE IF NOT EXISTS funding_view_invalidations(
 view_id TEXT NOT NULL, opportunity_id TEXT NOT NULL, from_revision BIGINT NOT NULL,
 to_revision BIGINT NOT NULL, reasons_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(view_id, opportunity_id, to_revision));
"""


class OpportunityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def opportunity_id(namespace, record):
    return "funding-opportunity:" + digest([
        namespace, record["provider"], record["record_kind"], record["provider_id"], record.get("round_id"),
    ])[:32]


def _parse_instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_amendments(before, after):
    """Classify what changed between two authoritative record versions."""
    if before is None:
        return [{"kind": "new", "path": "record"}]
    changes = []
    if canonical(before.get("deadlines")) != canonical(after.get("deadlines")):
        changes.append({"kind": "deadline_shift", "path": "deadlines",
                        "before": before.get("deadlines"), "after": after.get("deadlines")})
    if (before.get("status") or {}).get("asserted") != (after.get("status") or {}).get("asserted"):
        changes.append({"kind": "status_change", "path": "status",
                        "before": (before.get("status") or {}).get("asserted"),
                        "after": (after.get("status") or {}).get("asserted")})
    old = {r["requirement_id"]: r for r in before.get("requirements") or []}
    new = {r["requirement_id"]: r for r in after.get("requirements") or []}
    for key in sorted(old.keys() | new.keys()):
        if canonical(old.get(key)) != canonical(new.get(key)):
            changes.append({"kind": "rule_change", "path": f"requirements/{key}",
                            "before": old.get(key, {}).get("text"), "after": new.get(key, {}).get("text")})
    for path in ("financial_terms", "instrument"):
        if canonical(before.get(path)) != canonical(after.get(path)):
            changes.append({"kind": "terms_change", "path": path})
    if canonical(before.get("documents")) != canonical(after.get("documents")):
        changes.append({"kind": "documents_change", "path": "documents"})
    remaining = {"title", "themes", "references", "sections", "programme", "funder"}
    if any(canonical(before.get(k)) != canonical(after.get(k)) for k in remaining):
        changes.append({"kind": "descriptive_change", "path": "record"})
    return changes


def effective_status(record, *, as_of_ms, listing_state="listed", source_stale=False, conflicts=None):
    """Derive the application window state at ``as_of_ms`` with reasons."""
    kind = record["record_kind"]
    if kind == "award":
        return {"state": "not_an_opportunity", "reasons": ["award history is not an application call"]}
    if kind == "directory_entry":
        return {"state": "not_an_opportunity",
                "reasons": ["directory listing; resolve the administering body's current call"]}
    asserted = (record.get("status") or {}).get("asserted", "unknown")
    now = datetime.fromtimestamp(as_of_ms / 1000, tz=UTC)
    deadlines = record.get("deadlines") or []
    closing = [d for d in deadlines if d["kind"] in {"submission", "cut-off"}]
    openings = [d for d in deadlines if d["kind"] == "opening" and d.get("instant")]
    reasons, upcoming = [], []
    for deadline in closing:
        if deadline.get("instant"):
            if _parse_instant(deadline["instant"]) > now:
                upcoming.append(deadline)
        elif deadline.get("date") and deadline["date"] >= now.date().isoformat():
            upcoming.append(deadline)
            reasons.append(f"deadline {deadline['date']} has no source timezone; exact cutoff unknown")
    final_passed = bool(closing) and not upcoming and all(
        d.get("instant") or d.get("date") for d in closing
    )
    if kind == "programme":
        state = "rolling" if asserted == "rolling" else "programme"
        reasons.append("programme-level record; application windows are defined by calls")
    elif asserted == "closed":
        state, reasons = "closed", reasons + ["provider states the call is closed"]
    elif final_passed:
        state, reasons = "closed", reasons + ["final submission deadline has passed"]
    elif openings and all(_parse_instant(d["instant"]) > now for d in openings):
        state = "forthcoming"
    elif asserted in {"open", "rolling", "forthcoming"}:
        state = asserted
    else:
        state, reasons = "unknown", reasons + ["provider status unknown"]
    if listing_state == "absent_from_listing" and state not in {"closed"}:
        reasons.append("no longer present in the latest complete listing; not treated as closed")
        state = "unconfirmed"
    for conflict in conflicts or []:
        if conflict["field"] in {"deadlines", "status"}:
            reasons.append(f"provider pages disagree on {conflict['field']}; the detail page is used, check the source")
    if source_stale and state not in {"closed"}:
        reasons.append("latest provider refresh failed; state may be outdated")
    upcoming.sort(key=lambda d: _parse_instant(d["instant"] if d.get("instant") else d["date"] + "T23:59:59+14:00"))
    return {"state": state, "reasons": reasons, "next_deadline": upcoming[0] if upcoming else None,
            "stages": sorted({d.get("stage") for d in closing if d.get("stage")}),
            "source_stale": source_stale}


class FundingOpportunityStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _read(namespace, scopes):
        if "operator" in scopes:
            return
        if READ_SCOPE not in scopes or (
            f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes
        ):
            raise OpportunityError("unauthorized", "funding read and namespace access are required")

    @staticmethod
    def _write(namespace, scopes):
        if "operator" in scopes:
            return
        if WRITE_SCOPE not in scopes or INGEST_SCOPE not in scopes or f"namespace:{namespace}:write" not in scopes:
            raise OpportunityError("unauthorized", "funding write, ingestion and namespace write scopes are required")

    def ingest(self, namespace, provider, records, *, observation_id, observed_at_ms, scopes,
               evidence=None, coverage=None):
        """Apply one provider observation.

        ``coverage`` is ``{"complete": bool, "listing": str}``; only a complete
        listing marks previously seen records of that listing absent, and
        absence never closes a call.
        """
        self._write(namespace, scopes)
        if not isinstance(records, list) or len(records) > 5000:
            raise OpportunityError("invalid_observation", "bounded record list required")
        coverage = coverage or {"complete": False}
        records = [validate_record(r) for r in records]
        if any(r["provider"] != provider for r in records):
            raise OpportunityError("invalid_observation", "records must belong to the observed provider")
        evidence = evidence or {}
        summary = {"observation_id": observation_id, "provider": provider, "created": [], "revised": [],
                   "unchanged": [], "absent": [], "invalidated_views": [], "amendments": {}}
        self.conn.execute("BEGIN")
        try:
            seen = set()
            for record in records:
                identity = opportunity_id(namespace, record)
                seen.add(identity)
                record_hash = digest(record)
                source_key = coverage.get("listing") or provider
                assertion_id = "funding-assertion:" + digest([identity, source_key, record_hash, observation_id])[:32]
                self.conn.execute(
                    "INSERT INTO funding_provider_assertions VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [assertion_id, namespace, identity, source_key, record_hash, canonical(record),
                     observation_id, observed_at_ms, canonical(evidence)])
                self._apply(namespace, identity, record, observed_at_ms, summary,
                            listing=coverage.get("listing") or provider)
            listing = coverage.get("listing") or provider
            for identity in sorted(seen):
                self.conn.execute("INSERT INTO funding_listing_members VALUES (?,?,?) ON CONFLICT DO NOTHING",
                                  [namespace, listing, identity])
            if coverage.get("complete"):
                # Only a complete observation of the same listing can show that a
                # member disappeared; disappearance is recorded, never closure.
                members = self.conn.execute(
                    "SELECT opportunity_id FROM funding_listing_members WHERE namespace=? AND listing=?",
                    [namespace, listing]).fetchall()
                for (identity,) in members:
                    if identity not in seen:
                        self.conn.execute("UPDATE funding_opportunities SET listing_state='absent_from_listing' WHERE opportunity_id=?", [identity])
                        summary["absent"].append(identity)
            self.conn.execute(
                """INSERT INTO funding_provider_state VALUES (?,?,?,NULL,NULL,?,?)
                   ON CONFLICT (namespace, provider) DO UPDATE SET last_success_ms=excluded.last_success_ms,
                   last_observation_id=excluded.last_observation_id, last_execution=excluded.last_execution,
                   last_failure_ms=NULL, last_failure_code=NULL""",
                [namespace, provider, observed_at_ms, observation_id, evidence.get("execution") or "unrecorded"])
            self._link(namespace, records)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        summary["coverage"] = coverage
        return summary

    def _content(self, identity, revision=None):
        row = self.conn.execute(
            """SELECT r.content_json FROM funding_opportunities o JOIN funding_opportunity_revisions r
               ON r.opportunity_id=o.opportunity_id AND r.revision=coalesce(?, o.revision) WHERE o.opportunity_id=?""",
            [revision, identity]).fetchone()
        return json.loads(row[0]) if row else None

    def _current_assertions(self, namespace, identity):
        rows = self.conn.execute(
            """SELECT source_key, record_json FROM funding_provider_assertions a WHERE namespace=? AND opportunity_id=?
               AND observed_at_ms = (SELECT max(observed_at_ms) FROM funding_provider_assertions b
                                     WHERE b.opportunity_id=a.opportunity_id AND b.source_key=a.source_key)
               ORDER BY source_key, observed_at_ms""", [namespace, identity]).fetchall()
        latest = {}
        for key, encoded in rows:
            latest[key] = json.loads(encoded)
        return latest

    def _apply(self, namespace, identity, record, observed_at_ms, summary, *, listing):
        assertions = self._current_assertions(namespace, identity)
        # Authority precedence chooses the primary record; disagreements among
        # current assertions from different pages are retained as conflicts.
        primary = assertions[sorted(assertions, key=lambda k: (_AUTHORITY_RANK[assertions[k]["authority"]["kind"]], _detail_rank(k), k))[0]]
        conflicts = []
        for field in _CONFLICT_FIELDS:
            values = {key: canonical(_semantic(r.get(field))) for key, r in assertions.items() if r.get(field) not in (None, [], {})}
            if len(set(values.values())) > 1:
                conflicts.append({"field": field, "values": [
                    {"source_key": key, "source_url": assertions[key]["source_url"], "value": json.loads(value)}
                    for key, value in sorted(values.items())]})
        row = self.conn.execute(
            "SELECT revision, listing_state FROM funding_opportunities WHERE opportunity_id=?", [identity]).fetchone()
        previous = self._content(identity) if row else None
        content = {"contract": CONTRACT, "opportunity_id": identity, "namespace": namespace,
                   "record": primary, "conflicts": conflicts, "listing": listing,
                   "assertion_sources": sorted(assertions)}
        if previous and digest({k: previous[k] for k in ("record", "conflicts")}) == digest(
            {k: content[k] for k in ("record", "conflicts")}
        ):
            self.conn.execute(
                "UPDATE funding_opportunities SET last_seen_ms=greatest(last_seen_ms, ?), listing_state='listed' WHERE opportunity_id=?",
                [observed_at_ms, identity])
            summary["unchanged"].append(identity)
            return
        revision = (row[0] if row else 0) + 1
        amendments = classify_amendments(previous["record"] if previous else None, primary)
        content.update(revision=revision, amendments=amendments, observed_at_ms=observed_at_ms,
                       previous_revision=row[0] if row else None)
        if row:
            self.conn.execute(
                "UPDATE funding_opportunities SET revision=?, last_seen_ms=greatest(last_seen_ms, ?), listing_state='listed' WHERE opportunity_id=?",
                [revision, observed_at_ms, identity])
            summary["revised"].append(identity)
        else:
            self.conn.execute(
                "INSERT INTO funding_opportunities VALUES (?,?,?,?,?,?,1,?,'listed')",
                [identity, namespace, primary["provider"], primary["record_kind"], primary["provider_id"],
                 primary.get("round_id"), observed_at_ms])
            summary["created"].append(identity)
        self.conn.execute("INSERT INTO funding_opportunity_revisions VALUES (?,?,?,?)",
                          [identity, revision, canonical(content), self.now()])
        summary["amendments"][identity] = amendments
        if row:
            views = self.conn.execute(
                "SELECT view_id, revision FROM funding_view_dependencies WHERE opportunity_id=? AND revision<?",
                [identity, revision]).fetchall()
            for view_id, old in views:
                self.conn.execute(
                    "INSERT INTO funding_view_invalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [view_id, identity, old, revision, canonical(sorted({a["kind"] for a in amendments})), self.now()])
                summary["invalidated_views"].append(view_id)

    def _link(self, namespace, records):
        """Link directory entries/programmes to authoritative calls they reference."""
        for record in records:
            for reference in record.get("references") or []:
                if reference["kind"] not in {"authoritative-call", "programme"} or not reference.get("provider_id"):
                    continue
                targets = self.conn.execute(
                    "SELECT opportunity_id FROM funding_opportunities WHERE namespace=? AND provider=? AND provider_id=?",
                    [namespace, reference.get("provider"), reference["provider_id"]]).fetchall()
                for (target,) in targets:
                    self.conn.execute(
                        "INSERT INTO funding_opportunity_links VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
                        [namespace, opportunity_id(namespace, record), target, reference["kind"],
                         canonical({"source_url": record["source_url"], "reference": reference})])

    def record_failure(self, namespace, provider, *, observation_id, failure_code, observed_at_ms, scopes):
        """A failed or partial refresh marks the source stale; it closes nothing."""
        self._write(namespace, scopes)
        self.conn.execute(
            """INSERT INTO funding_provider_state VALUES (?,?,NULL,?,?,?,NULL)
               ON CONFLICT (namespace, provider) DO UPDATE SET last_failure_ms=excluded.last_failure_ms,
               last_failure_code=excluded.last_failure_code""",
            [namespace, provider, observed_at_ms, failure_code, observation_id])
        return {"provider": provider, "state": "stale", "failure_code": failure_code}

    def provider_state(self, namespace, provider):
        row = self.conn.execute(
            "SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution FROM funding_provider_state WHERE namespace=? AND provider=?",
            [namespace, provider]).fetchone()
        if not row:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        # A success clears the failure fields, so any retained failure is newer.
        stale = row[1] is not None
        return {"provider": provider, "last_success_ms": row[0], "last_failure_ms": row[1],
                "last_failure_code": row[2], "last_execution": row[3], "stale": stale}

    def get(self, namespace, identity, *, scopes, revision=None, as_of_ms=None):
        self._read(namespace, scopes)
        row = self.conn.execute(
            "SELECT listing_state, revision FROM funding_opportunities WHERE opportunity_id=? AND namespace=?",
            [identity, namespace]).fetchone()
        if not row:
            raise OpportunityError("opportunity_not_found", "opportunity is unavailable")
        content = self._content(identity, revision)
        if content is None:
            raise OpportunityError("opportunity_not_found", "opportunity revision is unavailable")
        provider = self.provider_state(namespace, content["record"]["provider"])
        content.update(
            current_revision=row[1], listing_state=row[0], source_freshness=provider,
            status=effective_status(content["record"], as_of_ms=as_of_ms or self.now(),
                                    listing_state=row[0], source_stale=provider["stale"],
                                    conflicts=content.get("conflicts")),
            links=[{"to": t, "kind": k, "evidence": json.loads(e)} for t, k, e in self.conn.execute(
                "SELECT to_id, kind, evidence_json FROM funding_opportunity_links WHERE namespace=? AND from_id=?",
                [namespace, identity]).fetchall()],
            linked_from=[f for (f,) in self.conn.execute(
                "SELECT from_id FROM funding_opportunity_links WHERE namespace=? AND to_id=?",
                [namespace, identity]).fetchall()],
        )
        return content

    def list(self, namespace, *, scopes, providers=None, kinds=None, as_of_ms=None, limit=500):
        self._read(namespace, scopes)
        rows = self.conn.execute(
            "SELECT opportunity_id, provider, record_kind FROM funding_opportunities WHERE namespace=? ORDER BY opportunity_id LIMIT ?",
            [namespace, min(max(int(limit), 1), 5000)]).fetchall()
        return [self.get(namespace, identity, scopes=scopes, as_of_ms=as_of_ms) for identity, provider, kind in rows
                if (not providers or provider in providers) and (not kinds or kind in kinds)]

    def history(self, namespace, identity, *, scopes):
        self._read(namespace, scopes)
        rows = self.conn.execute(
            "SELECT content_json FROM funding_opportunity_revisions WHERE opportunity_id=? ORDER BY revision",
            [identity]).fetchall()
        return [{"revision": c["revision"], "amendments": c["amendments"], "observed_at_ms": c["observed_at_ms"]}
                for c in (json.loads(r[0]) for r in rows) if c["namespace"] == namespace]

    def register_view(self, namespace, view_id, owner, pins):
        """Record that a derived view (assessment, shortlist, workspace) used these revisions."""
        for identity, revision in pins.items():
            self.conn.execute(
                """INSERT INTO funding_view_dependencies VALUES (?,?,?,?,?)
                   ON CONFLICT (view_id, opportunity_id) DO UPDATE SET revision=excluded.revision""",
                [view_id, namespace, owner, identity, revision])

    def view_status(self, view_id):
        rows = self.conn.execute(
            "SELECT opportunity_id, from_revision, to_revision, reasons_json FROM funding_view_invalidations v WHERE view_id=? "
            "AND from_revision >= (SELECT revision FROM funding_view_dependencies d WHERE d.view_id=v.view_id AND d.opportunity_id=v.opportunity_id) "
            "ORDER BY opportunity_id, to_revision", [view_id]).fetchall()
        stale = [{"opportunity_id": o, "from_revision": f, "to_revision": t, "reasons": json.loads(r)} for o, f, t, r in rows]
        return {"view_id": view_id, "current": not stale, "invalidations": stale}
