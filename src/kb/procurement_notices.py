"""Normalise procurement notices into revisioned procedures, lots and award history.

Every acquired notice is kept as a *source assertion*. Procedures are keyed by
provider and the source's cross-stage key (eForms BT-04 procedure identifier,
OCDS ``ocid`` or SAM solicitation number), so a prior-information notice, the
contract notice, its corrigenda and a cancellation become *revisions of one
procedure*, each revision naming the notice that caused it and a classified
change list. Award and contract-modification notices are *award history*
rows linked to their procedure; they never create or reopen an opportunity.

Status is derived conservatively and per lot. A lot is closed only when its
final submission deadline instant has passed (or the notice was cancelled or
the lot awarded); a procedure missing from a later complete listing is
*unconfirmed*, and a failed refresh marks the source stale — neither closes
anything. Staleness is shown from the last successful receipt. Deadlines keep
the original text and offset; a date without an offset is never turned into
an instant. Derived views (assessments, shortlists, workspaces) register the
revisions they used and are invalidated when a procedure is revised or
awarded.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from src.kb.funding_records import canonical, digest
from src.kb.procurement_records import (
    HISTORY_STAGES,
    OPPORTUNITY_STAGES,
    PROCEDURAL,
    READ_SCOPE,
    WRITE_SCOPE,
    cpv_code,
    validate_record,
)

CONTRACT = "noesis-procurement-procedure-v1"
INGEST_SCOPE = "knowledge:ingestion:execute"
PROCEDURE_STATES = ("open", "forthcoming", "closed", "cancelled", "awarded", "unconfirmed", "unknown")
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_notice_assertions(
 assertion_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, procedure_key TEXT NOT NULL, notice_id TEXT NOT NULL,
 stage TEXT NOT NULL, record_hash TEXT NOT NULL, record_json TEXT NOT NULL, observation_id TEXT NOT NULL,
 observed_at_ms BIGINT NOT NULL, evidence_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_procedures(
 procedure_key TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, procedure_id TEXT NOT NULL,
 revision BIGINT NOT NULL, last_seen_ms BIGINT NOT NULL, listing_state TEXT NOT NULL,
 award_generation BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_procedure_revisions(
 procedure_key TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(procedure_key, revision));
CREATE TABLE IF NOT EXISTS procurement_awards(
 award_key TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, procedure_key TEXT NOT NULL,
 procedure_id TEXT NOT NULL, notice_id TEXT NOT NULL, stage TEXT NOT NULL, award_date TEXT,
 buyer_json TEXT NOT NULL, suppliers_json TEXT NOT NULL, cpv_json TEXT NOT NULL, value_json TEXT,
 source_url TEXT NOT NULL, content_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_listing_members(
 namespace TEXT NOT NULL, listing TEXT NOT NULL, procedure_key TEXT NOT NULL,
 PRIMARY KEY(namespace, listing, procedure_key));
CREATE TABLE IF NOT EXISTS procurement_run_members(
 run_id TEXT NOT NULL, listing TEXT NOT NULL, procedure_key TEXT NOT NULL, PRIMARY KEY(run_id, listing, procedure_key));
CREATE TABLE IF NOT EXISTS procurement_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_observation_id TEXT, last_execution TEXT, PRIMARY KEY(namespace, provider));
CREATE TABLE IF NOT EXISTS procurement_view_dependencies(
 view_id TEXT NOT NULL, namespace TEXT NOT NULL, owner TEXT NOT NULL, procedure_key TEXT NOT NULL,
 revision BIGINT NOT NULL, award_generation BIGINT NOT NULL, PRIMARY KEY(view_id, procedure_key));
CREATE TABLE IF NOT EXISTS procurement_view_invalidations(
 view_id TEXT NOT NULL, procedure_key TEXT NOT NULL, from_revision BIGINT NOT NULL, to_revision BIGINT NOT NULL,
 reasons_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(view_id, procedure_key, to_revision));
"""


class NoticeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def procedure_key(namespace, provider, procedure_id):
    return "procurement-procedure:" + digest([namespace, provider, procedure_id])[:32]


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _order_key(item):
    return (item.get("published") or "", item.get("notice_version") or "", item["notice_id"])


def classify_changes(before, after):
    """What changed between two authoritative notice versions (per lot where it applies)."""
    if before is None:
        return [{"kind": "new", "path": "record"}]
    changes = []

    def keyed(items, key):
        return {(i.get("kind"), i.get(key)) if "kind" in i and key == "lot_id" else i.get(key): i for i in items or []}

    old_deadlines, new_deadlines = keyed(before.get("deadlines"), "lot_id"), keyed(after.get("deadlines"), "lot_id")
    for key in sorted(old_deadlines.keys() | new_deadlines.keys(), key=str):
        if canonical(old_deadlines.get(key)) != canonical(new_deadlines.get(key)):
            kind, lot = key if isinstance(key, tuple) else (None, None)
            changes.append({"kind": "deadline_change", "deadline_kind": kind, "lot_id": lot,
                            "before": (old_deadlines.get(key) or {}).get("text"), "after": (new_deadlines.get(key) or {}).get("text")})
    old_reqs, new_reqs = keyed(before.get("requirements"), "requirement_id"), keyed(after.get("requirements"), "requirement_id")
    for key in sorted(old_reqs.keys() | new_reqs.keys()):
        if canonical(old_reqs.get(key)) != canonical(new_reqs.get(key)):
            item = new_reqs.get(key) or old_reqs.get(key)
            changes.append({"kind": "requirement_change", "requirement_id": key, "lot_ids": item.get("lot_ids"),
                            "before": (old_reqs.get(key) or {}).get("text"), "after": (new_reqs.get(key) or {}).get("text")})
    old_lots, new_lots = keyed(before.get("lots"), "lot_id"), keyed(after.get("lots"), "lot_id")
    for key in sorted(old_lots.keys() | new_lots.keys()):
        if canonical(old_lots.get(key)) != canonical(new_lots.get(key)):
            changes.append({"kind": "lot_change", "lot_id": key, "added": key not in old_lots, "removed": key not in new_lots})
    if canonical(before.get("estimated_value")) != canonical(after.get("estimated_value")):
        changes.append({"kind": "value_change", "path": "estimated_value"})
    if (before.get("status") or {}).get("asserted") != (after.get("status") or {}).get("asserted"):
        changes.append({"kind": "status_change", "before": (before.get("status") or {}).get("asserted"),
                        "after": (after.get("status") or {}).get("asserted")})
    if canonical(before.get("documents")) != canonical(after.get("documents")):
        changes.append({"kind": "document_change", "path": "documents"})
    if any(canonical(before.get(k)) != canonical(after.get(k)) for k in ("title", "titles", "classifications", "procedure", "buyer")):
        changes.append({"kind": "descriptive_change", "path": "record"})
    return changes


def lot_ids(record):
    return [lot["lot_id"] for lot in record.get("lots") or []] or [None]


def lot_view(record, lot_id):
    """Requirements, deadlines, classifications and value that apply to one lot (``None``: the whole notice)."""
    lot = next((item for item in record.get("lots") or [] if item["lot_id"] == lot_id), None)
    requirements = [r for r in record.get("requirements") or [] if lot_id is None or not r.get("lot_ids") or lot_id in r["lot_ids"]]
    deadlines = [d for d in record.get("deadlines") or [] if lot_id is None or d.get("lot_id") in (None, lot_id)]
    classifications = (lot or {}).get("classifications") or record.get("classifications") or []
    return {"lot_id": lot_id, "title": (lot or {}).get("title") or record["title"],
            "requirements": requirements, "deadlines": deadlines, "classifications": classifications,
            "cpv": sorted({c["code"] for c in classifications if c["scheme"] == "CPV"} |
                          ({c["code"] for c in record.get("classifications") or [] if c["scheme"] == "CPV"} if not lot else set())),
            "estimated_value": (lot or {}).get("estimated_value") or (record.get("estimated_value") if lot_id is None or len(record.get("lots") or []) <= 1 else None),
            "procedural": [r for r in requirements if r["category"] in PROCEDURAL]}


def effective_status(content, awards, *, as_of_ms, listing_state="listed", source_stale=False):
    """Derive procedure and per-lot state at ``as_of_ms`` with reasons."""
    record = content["record"]
    now = datetime.fromtimestamp(as_of_ms / 1000, tz=UTC)
    cancelled = content.get("cancellation")
    cancelled_lots = set((cancelled or {}).get("lot_ids") or []) if cancelled else set()
    awarded = {}
    for award in awards:
        if award["stage"] != "award" or award["content"].get("status") not in {"active", None}:
            continue
        for lot in award["content"].get("lot_ids") or [None]:
            awarded.setdefault(lot, []).append(award["award_key"])
    lots = {}
    for lot_id in lot_ids(record):
        reasons, state = [], None
        submissions = [d for d in record.get("deadlines") or [] if d["kind"] == "submission" and d.get("lot_id") in (None, lot_id)]
        upcoming = []
        for item in submissions:
            if item.get("instant"):
                if _instant(item["instant"]) > now:
                    upcoming.append(item)
            elif item.get("date") and item["date"] >= now.date().isoformat():
                upcoming.append(item)
                reasons.append(f"deadline {item['date']} states no offset; exact cutoff unknown")
        if cancelled and (not cancelled_lots or lot_id in cancelled_lots or lot_id is None):
            state = "cancelled"
            reasons.append(f"cancellation notice {cancelled['notice_id']}")
        elif lot_id in awarded or None in awarded:
            state = "awarded"
            reasons.append("an award notice for this procedure covers this lot")
        elif record["stage"] == "prior-information" or (record.get("status") or {}).get("asserted") == "planned":
            state = "forthcoming"
            reasons.append("prior information notice: planned procurement, not a call for competition")
        elif submissions and not upcoming:
            state = "closed"
            reasons.append("the submission deadline has passed")
        elif upcoming:
            state = "open"
        elif (record.get("status") or {}).get("asserted") == "active":
            state = "open"
            reasons.append("provider states the notice is active; no submission deadline is stated")
        else:
            state = "unknown"
            reasons.append("status and deadline unknown")
        if listing_state == "absent_from_listing" and state in {"open", "forthcoming", "unknown"}:
            reasons.append("no longer present in the latest complete listing; not treated as closed")
            state = "unconfirmed"
        if source_stale and state not in {"closed", "cancelled", "awarded"}:
            reasons.append("latest provider refresh failed; state may be outdated (see source freshness)")
        upcoming.sort(key=lambda d: _instant(d["instant"]) if d.get("instant") else _instant(d["date"] + "T23:59:59+14:00"))
        lots[lot_id or "_"] = {"lot_id": lot_id, "state": state, "reasons": reasons,
                               "next_deadline": upcoming[0] if upcoming else None}
    states = {v["state"] for v in lots.values()}
    for candidate in ("open", "unconfirmed", "forthcoming", "unknown"):
        if candidate in states:
            state = candidate
            break
    else:
        state = "cancelled" if states == {"cancelled"} else "awarded" if states == {"awarded"} else "closed"
    deadlines = [v["next_deadline"] for v in lots.values() if v["next_deadline"]]
    deadlines.sort(key=lambda d: _instant(d["instant"]) if d.get("instant") else _instant(d["date"] + "T23:59:59+14:00"))
    return {"state": state, "lots": lots, "next_deadline": deadlines[0] if deadlines else None,
            "reasons": sorted({r for v in lots.values() for r in v["reasons"]}), "source_stale": source_stale}


class ProcurementNoticeStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _read(namespace, scopes):
        if "operator" in scopes:
            return
        if READ_SCOPE not in scopes or (f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes):
            raise NoticeError("unauthorized", "procurement read and namespace access are required")

    @staticmethod
    def _write(namespace, scopes):
        if "operator" in scopes:
            return
        if WRITE_SCOPE not in scopes or INGEST_SCOPE not in scopes or f"namespace:{namespace}:write" not in scopes:
            raise NoticeError("unauthorized", "procurement write, ingestion and namespace write scopes are required")

    # ------------------------------------------------------------ ingestion

    def ingest(self, namespace, provider, records, *, observation_id, observed_at_ms, scopes, evidence=None,
               coverage=None, run_id=None):
        """Apply one provider observation (a page or a whole listing).

        ``coverage`` is ``{"complete": bool, "listing": str}``; only a complete
        listing marks previously seen procedures absent, and absence closes
        nothing.
        """
        self._write(namespace, scopes)
        if not isinstance(records, list) or len(records) > 5000:
            raise NoticeError("invalid_observation", "bounded record list required")
        records = sorted((validate_record(r) for r in records), key=_order_key)
        if any(r["provider"] != provider for r in records):
            raise NoticeError("invalid_observation", "records must belong to the observed provider")
        coverage, evidence = coverage or {"complete": False}, evidence or {}
        listing = coverage.get("listing") or provider
        summary = {"observation_id": observation_id, "provider": provider, "created": [], "revised": [], "unchanged": [],
                   "superseded": [], "awards": [], "absent": [], "invalidated_views": [], "changes": {}}
        self.conn.execute("BEGIN")
        try:
            seen = set()
            for item in records:
                key = procedure_key(namespace, provider, item["procedure_id"])
                record_hash = digest(item)
                self.conn.execute(
                    "INSERT INTO procurement_notice_assertions VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    ["procurement-assertion:" + digest([key, item["notice_id"], record_hash, observation_id])[:32], namespace, key,
                     item["notice_id"], item["stage"], record_hash, canonical(item), observation_id, observed_at_ms, canonical(evidence)])
                if item["stage"] in HISTORY_STAGES:
                    self._apply_award(namespace, key, item, observed_at_ms, summary)
                    continue
                seen.add(key)
                self._apply_notice(namespace, key, item, observed_at_ms, summary, listing)
            for key in sorted(seen):
                self.conn.execute("INSERT INTO procurement_listing_members VALUES (?,?,?) ON CONFLICT DO NOTHING", [namespace, listing, key])
                if run_id:
                    self.conn.execute("INSERT INTO procurement_run_members VALUES (?,?,?) ON CONFLICT DO NOTHING", [run_id, listing, key])
            if coverage.get("complete"):
                summary["absent"] = self._mark_absent(namespace, listing, seen)
            self.conn.execute(
                """INSERT INTO procurement_provider_state VALUES (?,?,?,NULL,NULL,?,?)
                   ON CONFLICT (namespace, provider) DO UPDATE SET last_success_ms=excluded.last_success_ms,
                   last_observation_id=excluded.last_observation_id, last_execution=excluded.last_execution,
                   last_failure_ms=NULL, last_failure_code=NULL""",
                [namespace, provider, observed_at_ms, observation_id, evidence.get("execution") or "unrecorded"])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        summary["coverage"] = coverage
        return summary

    def _mark_absent(self, namespace, listing, seen):
        absent = []
        for (key,) in self.conn.execute(
                "SELECT procedure_key FROM procurement_listing_members WHERE namespace=? AND listing=?", [namespace, listing]).fetchall():
            if key not in seen:
                self.conn.execute("UPDATE procurement_procedures SET listing_state='absent_from_listing' WHERE procedure_key=?", [key])
                absent.append(key)
        return absent

    def close_listing(self, namespace, listing, run_id, *, scopes):
        """After a complete run: procedures of ``listing`` not seen in ``run_id`` become unconfirmed (never closed)."""
        self._write(namespace, scopes)
        seen = {k for (k,) in self.conn.execute(
            "SELECT procedure_key FROM procurement_run_members WHERE run_id=? AND listing=?", [run_id, listing]).fetchall()}
        return self._mark_absent(namespace, listing, seen)

    def _content(self, key, revision=None):
        row = self.conn.execute(
            """SELECT r.content_json FROM procurement_procedures p JOIN procurement_procedure_revisions r
               ON r.procedure_key=p.procedure_key AND r.revision=coalesce(?, p.revision) WHERE p.procedure_key=?""",
            [revision, key]).fetchone()
        return json.loads(row[0]) if row else None

    def _apply_notice(self, namespace, key, item, observed_at_ms, summary, listing):
        row = self.conn.execute("SELECT revision FROM procurement_procedures WHERE procedure_key=?", [key]).fetchone()
        previous = self._content(key) if row else None
        if previous:
            applied = previous["applied_notices"]
            known = next((a for a in applied if a["notice_id"] == item["notice_id"]), None)
            if known and known["record_hash"] == digest(item):
                self.conn.execute("UPDATE procurement_procedures SET last_seen_ms=greatest(last_seen_ms, ?), listing_state='listed' WHERE procedure_key=?",
                                  [observed_at_ms, key])
                summary["unchanged"].append(key)
                return
            latest = previous["cause"]
            if not known and _order_key(item) < _order_key(latest) and item["stage"] != "cancellation":
                # An older notice arriving late never rolls a procedure back.
                self.conn.execute("UPDATE procurement_procedures SET last_seen_ms=greatest(last_seen_ms, ?), listing_state='listed' WHERE procedure_key=?",
                                  [observed_at_ms, key])
                summary["superseded"].append({"procedure_key": key, "notice_id": item["notice_id"]})
                return
        base = previous["record"] if previous else None
        if item["stage"] == "cancellation":
            current = base or item
            cancellation = {"notice_id": item["notice_id"], "published": item.get("published"), "source_url": item["source_url"],
                            "lot_ids": [lot["lot_id"] for lot in item.get("lots") or []] or None}
            changes = [{"kind": "cancellation", "notice_id": item["notice_id"]}]
        else:
            current, cancellation = item, (previous or {}).get("cancellation")
            changes = classify_changes(base, item)
            if item["stage"] == "corrigendum":
                changes.insert(0, {"kind": "corrigendum", "notice_id": item["notice_id"], "changes": item.get("changes")})
        revision = (row[0] if row else 0) + 1
        applied = [a for a in (previous or {}).get("applied_notices", []) if a["notice_id"] != item["notice_id"]]
        applied.append({"notice_id": item["notice_id"], "stage": item["stage"], "published": item.get("published"),
                        "notice_version": item.get("notice_version"), "record_hash": digest(item), "source_url": item["source_url"]})
        content = {"contract": CONTRACT, "procedure_key": key, "namespace": namespace, "provider": item["provider"],
                   "procedure_id": item["procedure_id"], "record": current, "cancellation": cancellation,
                   "cause": {"notice_id": item["notice_id"], "stage": item["stage"], "published": item.get("published"),
                             "notice_version": item.get("notice_version"), "source_url": item["source_url"]},
                   "applied_notices": applied, "changes": changes, "revision": revision, "listing": listing,
                   "previous_revision": row[0] if row else None, "observed_at_ms": observed_at_ms}
        if row:
            self.conn.execute("UPDATE procurement_procedures SET revision=?, last_seen_ms=greatest(last_seen_ms, ?), listing_state='listed' WHERE procedure_key=?",
                              [revision, observed_at_ms, key])
            summary["revised"].append(key)
        else:
            self.conn.execute("INSERT INTO procurement_procedures VALUES (?,?,?,?,1,?,'listed',0)",
                              [key, namespace, item["provider"], item["procedure_id"], observed_at_ms])
            summary["created"].append(key)
        self.conn.execute("INSERT INTO procurement_procedure_revisions VALUES (?,?,?,?)", [key, revision, canonical(content), self.now()])
        summary["changes"][key] = changes
        if row:
            self._invalidate(key, revision, sorted({c["kind"] for c in changes}), summary)

    def _invalidate(self, key, revision, reasons, summary):
        for view_id, old in self.conn.execute(
                "SELECT view_id, revision FROM procurement_view_dependencies WHERE procedure_key=? AND revision<?", [key, revision]).fetchall():
            self.conn.execute("INSERT INTO procurement_view_invalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                              [view_id, key, old, revision, canonical(reasons), self.now()])
            summary["invalidated_views"].append(view_id)

    def _apply_award(self, namespace, key, item, observed_at_ms, summary):
        cpv = sorted({c["code"] for c in item.get("classifications") or [] if c["scheme"] == "CPV"})
        other = sorted({f"{c['scheme']}:{c['code']}" for c in item.get("classifications") or [] if c["scheme"] != "CPV"})
        entries = item.get("awards") or []
        contracts = {c.get("award_id"): c for c in item.get("contracts") or []}
        for award in entries:
            award_key = "procurement-award:" + digest([namespace, item["provider"], item["notice_id"], award["award_id"]])[:32]
            contract = contracts.get(award["award_id"])
            content = {**award, "contract": contract, "notice_id": item["notice_id"], "stage": item["stage"],
                       "procedure_id": item["procedure_id"], "published": item.get("published"), "title": item["title"],
                       "classifications": item.get("classifications") or [], "other_classifications": other,
                       "semantics": "award history: context only, never evidence that a procedure is open"}
            existing = self.conn.execute("SELECT content_json FROM procurement_awards WHERE award_key=?", [award_key]).fetchone()
            if existing and existing[0] == canonical(content):
                continue
            self.conn.execute(
                """INSERT INTO procurement_awards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT (award_key) DO UPDATE SET content_json=excluded.content_json, value_json=excluded.value_json,
                   suppliers_json=excluded.suppliers_json, observed_at_ms=excluded.observed_at_ms""",
                [award_key, namespace, item["provider"], key, item["procedure_id"], item["notice_id"], item["stage"],
                 award.get("date") or (contract or {}).get("date_signed") or item.get("published"), canonical(item["buyer"]),
                 canonical(award.get("suppliers") or []), canonical(cpv),
                 canonical(award.get("awarded_value") or (contract or {}).get("value")), item["source_url"], canonical(content), observed_at_ms])
            summary["awards"].append(award_key)
            # An award changes the procedure's derived state: views pinned to an
            # earlier award generation become stale (see view_status).
            self.conn.execute("UPDATE procurement_procedures SET award_generation=award_generation+1 WHERE procedure_key=?", [key])
            summary["invalidated_views"].extend(v for (v,) in self.conn.execute(
                "SELECT view_id FROM procurement_view_dependencies WHERE procedure_key=?", [key]).fetchall())

    def record_failure(self, namespace, provider, *, observation_id, failure_code, observed_at_ms, scopes):
        """A failed or partial refresh marks the source stale; it closes nothing."""
        self._write(namespace, scopes)
        self.conn.execute(
            """INSERT INTO procurement_provider_state VALUES (?,?,NULL,?,?,?,NULL)
               ON CONFLICT (namespace, provider) DO UPDATE SET last_failure_ms=excluded.last_failure_ms,
               last_failure_code=excluded.last_failure_code""",
            [namespace, provider, observed_at_ms, failure_code, observation_id])
        return {"provider": provider, "state": "stale", "failure_code": failure_code}

    def provider_state(self, namespace, provider):
        row = self.conn.execute(
            "SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution FROM procurement_provider_state WHERE namespace=? AND provider=?",
            [namespace, provider]).fetchone()
        if not row:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        return {"provider": provider, "last_success_ms": row[0], "last_failure_ms": row[1], "last_failure_code": row[2],
                "last_execution": row[3], "stale": row[1] is not None,
                "staleness": "fresh" if row[1] is None else "stale since the last successful receipt"}

    # ------------------------------------------------------------ reads

    def awards_for(self, namespace, key):
        return [{"award_key": k, "stage": s, "content": json.loads(c)} for k, s, c in self.conn.execute(
            "SELECT award_key, stage, content_json FROM procurement_awards WHERE namespace=? AND procedure_key=? ORDER BY award_key",
            [namespace, key]).fetchall()]

    def get(self, namespace, key, *, scopes, revision=None, as_of_ms=None):
        self._read(namespace, scopes)
        row = self.conn.execute(
            "SELECT listing_state, revision FROM procurement_procedures WHERE procedure_key=? AND namespace=?", [key, namespace]).fetchone()
        if not row:
            raise NoticeError("notice_not_found", "procedure is unavailable")
        content = self._content(key, revision)
        if content is None:
            raise NoticeError("notice_not_found", "procedure revision is unavailable")
        provider = self.provider_state(namespace, content["provider"])
        awards = self.awards_for(namespace, key)
        return {**content, "current_revision": row[1], "listing_state": row[0], "source_freshness": provider,
                "awards": awards,
                "status": effective_status(content, awards, as_of_ms=as_of_ms or self.now(), listing_state=row[0],
                                           source_stale=provider["stale"])}

    def list(self, namespace, *, scopes, providers=None, as_of_ms=None, limit=500):
        self._read(namespace, scopes)
        rows = self.conn.execute(
            "SELECT procedure_key, provider FROM procurement_procedures WHERE namespace=? ORDER BY procedure_key LIMIT ?",
            [namespace, min(max(int(limit), 1), 5000)]).fetchall()
        return [self.get(namespace, key, scopes=scopes, as_of_ms=as_of_ms) for key, provider in rows
                if not providers or provider in providers]

    def history(self, namespace, key, *, scopes):
        self._read(namespace, scopes)
        rows = self.conn.execute(
            "SELECT content_json FROM procurement_procedure_revisions WHERE procedure_key=? ORDER BY revision", [key]).fetchall()
        return [{"revision": c["revision"], "cause": c["cause"], "changes": c["changes"], "observed_at_ms": c["observed_at_ms"]}
                for c in (json.loads(r[0]) for r in rows) if c["namespace"] == namespace]

    def award_history(self, namespace, *, scopes, buyer=None, supplier=None, cpv=None, procedure=None, limit=200):
        """Award and modification history with sources and dates.

        ``buyer``/``supplier`` match source names case-insensitively or any
        stated identifier; ``cpv`` matches the code's hierarchy branch. This is
        context: an award never shows that any procedure is open.
        """
        self._read(namespace, scopes)
        rows = self.conn.execute(
            """SELECT award_key, provider, procedure_key, procedure_id, notice_id, stage, award_date, buyer_json, suppliers_json,
                      cpv_json, value_json, source_url, content_json FROM procurement_awards WHERE namespace=?
               ORDER BY award_date DESC NULLS LAST, award_key""", [namespace]).fetchall()

        def party_matches(item, wanted):
            wanted = wanted.casefold().strip()
            return item["name"].casefold().strip() == wanted or any(i["id"].casefold() == wanted for i in item.get("identifiers") or [])

        result = []
        for row in rows:
            buyer_item, suppliers, codes = json.loads(row[7]), json.loads(row[8]), json.loads(row[9])
            if buyer and not party_matches(buyer_item, buyer):
                continue
            if supplier and not any(party_matches(s, supplier) for s in suppliers):
                continue
            if cpv:
                prefix = (cpv_code(cpv) or str(cpv)).rstrip("0")
                if not any(code.startswith(prefix[:max(len(prefix), 2)]) for code in codes):
                    continue
            if procedure and row[2] != procedure:
                continue
            content = json.loads(row[12])
            result.append({"award_key": row[0], "provider": row[1], "procedure_key": row[2], "procedure_id": row[3],
                           "notice_id": row[4], "stage": row[5], "date": row[6], "buyer": buyer_item, "suppliers": suppliers,
                           "cpv": codes, "value": json.loads(row[10]) if row[10] else None, "source_url": row[11],
                           "lot_ids": content.get("lot_ids"), "status": content.get("status"),
                           "contract": content.get("contract"), "semantics": content["semantics"]})
            if len(result) >= limit:
                break
        return result

    def register_view(self, namespace, view_id, owner, pins):
        """Record that a derived view (assessment, shortlist, workspace) used these revisions."""
        for key, revision in pins.items():
            generation = (self.conn.execute("SELECT award_generation FROM procurement_procedures WHERE procedure_key=?",
                                            [key]).fetchone() or [0])[0]
            self.conn.execute(
                """INSERT INTO procurement_view_dependencies VALUES (?,?,?,?,?,?)
                   ON CONFLICT (view_id, procedure_key) DO UPDATE SET revision=excluded.revision,
                   award_generation=excluded.award_generation""",
                [view_id, namespace, owner, key, revision, generation])

    def view_status(self, view_id):
        rows = self.conn.execute(
            "SELECT procedure_key, from_revision, to_revision, reasons_json FROM procurement_view_invalidations v WHERE view_id=? "
            "AND from_revision >= (SELECT revision FROM procurement_view_dependencies d WHERE d.view_id=v.view_id AND d.procedure_key=v.procedure_key) "
            "ORDER BY procedure_key, to_revision", [view_id]).fetchall()
        stale = [{"procedure_key": k, "from_revision": f, "to_revision": t, "reasons": json.loads(r)} for k, f, t, r in rows]
        for key, pinned, current in self.conn.execute(
                """SELECT d.procedure_key, d.award_generation, p.award_generation FROM procurement_view_dependencies d
                   JOIN procurement_procedures p USING(procedure_key) WHERE d.view_id=? AND p.award_generation > d.award_generation
                   ORDER BY d.procedure_key""", [view_id]).fetchall():
            stale.append({"procedure_key": key, "reasons": ["award"], "award_generation": {"pinned": pinned, "current": current}})
        return {"view_id": view_id, "current": not stale, "invalidations": stale}


class ProcurementProjector:
    """Source-pack projector: committed pages of ``noesis-procurement-record-v1`` become procedures and award history."""

    SCOPES = frozenset({"operator"})

    def __init__(self, conn):
        self.conn = conn
        self.store = ProcurementNoticeStore(conn)

    def _observed_at(self, run_id, source_id):
        row = self.conn.execute("SELECT updated_at_ms FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                                [run_id, source_id]).fetchone()
        return int(row[0]) if row else self.store.now()

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del principal_id
        selection = dict(source.get("procurement") or {})
        namespace = selection.get("namespace") or "procurement"
        provider = selection["provider"]
        items = [dict(r["procurement_record"]) for r in records if r.get("procurement_record")]
        evidence = {"run_id": run_id, "pack_id": manifest["pack_id"], "source_id": source["source_id"],
                    "execution": page_receipt.get("execution") or "unrecorded", "response_sha256": page_receipt.get("response_sha256"),
                    "documents": sorted(d["document_id"] for d in documents)}
        return self.store.ingest(namespace, provider, items, observation_id=f"{run_id}:{source['source_id']}",
                                 observed_at_ms=self._observed_at(run_id, source["source_id"]), scopes=self.SCOPES,
                                 evidence=evidence, coverage={"complete": False, "listing": source["source_id"]}, run_id=run_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        selection = dict(source.get("procurement") or {})
        namespace = selection.get("namespace") or "procurement"
        if status == "complete":
            absent = self.store.close_listing(namespace, source["source_id"], run_id, scopes=self.SCOPES)
            return {"status": status, "absent": absent}
        row = self.conn.execute("SELECT failure_json FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                                [run_id, source["source_id"]]).fetchone()
        code = (json.loads(row[0]) if row and row[0] else {}).get("code") or "source_failed"
        failure = self.store.record_failure(namespace, selection["provider"], observation_id=f"{run_id}:{source['source_id']}",
                                            failure_code=code, observed_at_ms=self._observed_at(run_id, source["source_id"]),
                                            scopes=self.SCOPES)
        return {"status": status, "failure": failure}
