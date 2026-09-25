"""Immutable, source-pinned dependency impact reports and comparisons."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from src.domains.technical.impact import assess_inventory
from src.domains.technical.inventory import InventoryStore
from src.kb.research_projects import ResearchProjectStore

CONTRACT = "noesis-technical-impact-report-v1"
COMPARISON_CONTRACT = "noesis-technical-impact-comparison-v1"
EXPORT_CONTRACT = "noesis-technical-impact-export-v1"
READ_SCOPE = "knowledge:technical:read"
WRITE_SCOPE = "knowledge:technical:write"

_DDL = """
CREATE TABLE IF NOT EXISTS technical_impact_reports(
 report_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,report_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class ImpactReportError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, field, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ImpactReportError("invalid_input", f"{field} must be bounded nonempty text")
    return value


class ImpactReportStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(namespace, owner, principal_id, scopes, *, write=False):
        needed = WRITE_SCOPE if write else READ_SCOPE
        ns = f"namespace:{namespace}:{'write' if write else 'read'}"
        if not principal_id or ("operator" not in scopes and
            (needed not in scopes or ns not in scopes or principal_id != owner)):
            raise ImpactReportError("unauthorized", "current technical report and namespace access required")

    def _snapshot(self, document_id, scopes):
        _text(document_id, "source document", 1000)
        if "operator" not in scopes and f"document:{document_id}:read" not in scopes:
            raise ImpactReportError("unauthorized", "current source document read access required")
        try:
            row = self.conn.execute(
                "SELECT c.revision_id,r.content_hash,r.payload_hash FROM document_current_revisions c "
                "JOIN document_revision_records r ON r.document_id=c.document_id AND r.revision_id=c.revision_id "
                "WHERE c.document_id=? AND r.committed_watermark IS NOT NULL",
                [document_id],
            ).fetchone()
        except Exception:
            row = None
        if row is None:
            return None
        return {"document_id": document_id, "revision_id": row[0],
                "content_hash": row[1], "payload_hash": row[2]}

    def _reauthorize_snapshots(self, snapshots, scopes):
        for item in snapshots:
            if "operator" not in scopes and f"document:{item['document_id']}:read" not in scopes:
                raise ImpactReportError("unauthorized", "current source document read access required")
            row = self.conn.execute(
                "SELECT content_hash,payload_hash FROM document_revision_records "
                "WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                [item["document_id"], item["revision_id"]],
            ).fetchone()
            if row is None or row[0] != item["content_hash"] or row[1] != item["payload_hash"]:
                raise ImpactReportError("source_unavailable", "pinned technical evidence is unavailable")

    def _report(self, namespace, report_id, principal_id, scopes):
        self._authorize(namespace, principal_id, principal_id, scopes)
        row = self.conn.execute(
            "SELECT owner,report_json FROM technical_impact_reports WHERE namespace=? AND report_id=?",
            [namespace, report_id],
        ).fetchone()
        if not row:
            raise ImpactReportError("report_unavailable", "technical impact report is unavailable")
        self._authorize(namespace, row[0], principal_id, scopes)
        report = json.loads(row[1])
        InventoryStore(self.conn, initialize=False).inspect(report["inventory_id"], owner_id=report["owner"], limit=1)
        if report.get("project_id"):
            ResearchProjectStore(self.conn, initialize=False).inspect(
                namespace, report["project_id"], principal_id=principal_id, scopes=scopes)
        self._reauthorize_snapshots(report["source_snapshots"], scopes)
        return report

    def create(self, namespace, request_key, inventory_id, *, principal_id, scopes,
               project_id=None, limit=100, offset=0):
        _text(namespace, "namespace", 128)
        _text(request_key, "request key")
        _text(inventory_id, "inventory ID")
        self._authorize(namespace, principal_id, principal_id, scopes, write=True)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 5000:
            raise ImpactReportError("invalid_page", "impact page must be bounded to 100 entries")
        if project_id is not None:
            project = ResearchProjectStore(self.conn, initialize=False).inspect(
                namespace, project_id, principal_id=principal_id, scopes=scopes)
            if project["status"] == "archived":
                raise ImpactReportError("project_archived", "archived project cannot receive new assessment")
        assessment = assess_inventory(self.conn, inventory_id, owner_id=principal_id, limit=limit, offset=offset)
        inventory = InventoryStore(self.conn, initialize=False).inspect(inventory_id, owner_id=principal_id, limit=1)
        report_id = "technical-impact-report:" + _hash([namespace, principal_id, request_key])[:24]
        requested = {"inventory_id": inventory_id, "inventory_hash": assessment["inventory_hash"],
                     "project_id": project_id, "limit": limit, "offset": offset}
        request_hash = _hash(requested)
        prior = self.conn.execute("SELECT request_hash FROM technical_impact_reports WHERE report_id=?", [report_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise ImpactReportError("report_conflict", "request key is bound to another inventory or page")
            return {**self._report(namespace, report_id, principal_id, scopes), "idempotent": True}
        document_ids = set()
        for finding in assessment["findings"]:
            acquired = finding["entry"].get("acquired_package") or {}
            if acquired.get("source_document_id"):
                document_ids.add(acquired["source_document_id"])
            for advisory in finding["advisories"]:
                if advisory.get("source_document_id"):
                    document_ids.add(advisory["source_document_id"])
            for candidate in finding["upstream_review_candidates"]:
                if candidate.get("source_document_id"):
                    document_ids.add(candidate["source_document_id"])
        if len(document_ids) > 500:
            raise ImpactReportError("source_limit", "assessment has too many evidence sources")
        snapshots = {doc: self._snapshot(doc, scopes) for doc in sorted(document_ids)}
        for finding in assessment["findings"]:
            for advisory in finding["advisories"]:
                evidence = snapshots.get(advisory.get("source_document_id"))
                advisory["source_snapshot"] = evidence
                if evidence is None and advisory["finding"] != "unknown":
                    advisory["finding"] = "unknown"
                    advisory["reason"] = "source_revision_unavailable"
            for candidate in finding["upstream_review_candidates"]:
                candidate["source_snapshot"] = snapshots.get(candidate.get("source_document_id"))
            acquired = finding["entry"].get("acquired_package") or {}
            finding["package_snapshot"] = snapshots.get(acquired.get("source_document_id"))
            admissible = [a for a in finding["advisories"] if a["finding"] != "unknown"]
            if any(a["finding"] == "affected" for a in admissible):
                finding["classification"] = "affected"
            elif admissible and len(admissible) == len(finding["advisories"]):
                finding["classification"] = "unaffected_under_assessed_ranges"
            else:
                finding["classification"] = "unknown"
            finding["upgrade_suggestion_status"] = "review_only"
        snapshot_list = [value for value in snapshots.values() if value is not None]
        report = {"contract": CONTRACT, "report_id": report_id, "namespace": namespace,
                  "owner": principal_id, "revision": 1, "inventory_id": inventory_id,
                  "inventory_hash": inventory["inventory_hash"], "project_id": project_id,
                  "offset": offset, "limit": limit, "total": assessment["total"],
                  "findings": assessment["findings"], "source_snapshots": snapshot_list,
                  "missing_source_documents": [doc for doc, value in snapshots.items() if value is None],
                  "source_snapshot_hash": _hash({"snapshots": snapshot_list,
                                                 "missing": [doc for doc, value in snapshots.items() if value is None]}),
                  "coverage_note": assessment["coverage_note"], "created_at_ms": self.now(),
                  "executed_changes": False}
        report["report_hash"] = _hash(report)
        self.conn.execute("INSERT INTO technical_impact_reports VALUES (?,?,?,?,?,?)",
                          [report_id, namespace, principal_id, request_hash, _json(report), self.now()])
        return {**report, "idempotent": False}

    def inspect(self, namespace, report_id, *, principal_id, scopes):
        return self._report(namespace, report_id, principal_id, scopes)

    def compare(self, namespace, left_report_id, right_report_id, *, principal_id, scopes):
        left = self._report(namespace, left_report_id, principal_id, scopes)
        right = self._report(namespace, right_report_id, principal_id, scopes)
        def keyed(report):
            return {(_json(f["entry"].get("coordinate")), f["entry"].get("version"), f["entry"].get("origin")): f
                    for f in report["findings"]}
        lmap, rmap = keyed(left), keyed(right)
        keys = sorted(set(lmap) | set(rmap))
        changes = [{"entry_key": list(key),
                    "before": lmap[key]["classification"] if key in lmap else None,
                    "after": rmap[key]["classification"] if key in rmap else None,
                    "changed": key not in lmap or key not in rmap or
                               lmap[key]["classification"] != rmap[key]["classification"]}
                   for key in keys]
        return {"contract": COMPARISON_CONTRACT, "namespace": namespace,
                "left_report_id": left_report_id, "right_report_id": right_report_id,
                "left_inventory_hash": left["inventory_hash"], "right_inventory_hash": right["inventory_hash"],
                "left_source_snapshot_hash": left["source_snapshot_hash"],
                "right_source_snapshot_hash": right["source_snapshot_hash"],
                "changes": changes, "changed_count": sum(v["changed"] for v in changes),
                "same_inventory": left["inventory_hash"] == right["inventory_hash"],
                "same_sources": left["source_snapshot_hash"] == right["source_snapshot_hash"]}

    def export(self, namespace, report_id, *, principal_id, scopes):
        report = self._report(namespace, report_id, principal_id, scopes)
        bibliography = []
        assertions = []
        for index, finding in enumerate(report["findings"]):
            citations = []
            dependencies = []
            for advisory in finding["advisories"]:
                source = advisory.get("source_snapshot")
                if not source:
                    continue
                citation_id = "citation:" + _hash([report_id, index, advisory["advisory_id"], source["revision_id"]])[:24]
                if not any(value["id"] == citation_id for value in bibliography):
                    bibliography.append({"id": citation_id,
                                         "text": advisory["advisory_id"] + "; " + (advisory.get("source_url") or source["document_id"])})
                if citation_id not in citations:
                    citations.append(citation_id)
                dependency = {"kind": "source", "id": source["document_id"],
                              "revision": source["revision_id"], "namespace": namespace,
                              "locator": {"document_id": source["document_id"],
                                          "revision_id": source["revision_id"]}}
                if dependency not in dependencies:
                    dependencies.append(dependency)
            entry = finding["entry"]
            text = f"{entry['name']} {entry['version']}: {finding['classification']}"
            assertions.append({"id": f"finding-{index}", "text": text,
                               "kind": "sourced" if dependencies else "commentary",
                               "dependencies": dependencies, "citations": citations})
        authored = {"title": "Dependency impact assessment", "sections": [
            {"id": "impact", "title": "Assessed dependencies", "assertions": assertions}],
            "snapshot": {"id": report["report_id"], "generations": {namespace: 0}},
            "bibliography": bibliography,
            "limitations": [report["coverage_note"],
                            "Upgrade candidates require review; no dependency change was executed."]}
        from src.kb.authored_reports import validate_content
        validate_content(authored)
        return {"contract": EXPORT_CONTRACT, "report_id": report_id,
                "inventory_hash": report["inventory_hash"],
                "source_snapshot_hash": report["source_snapshot_hash"],
                "affected": [f for f in report["findings"] if f["classification"] == "affected"],
                "unaffected_under_assessed_ranges": [f for f in report["findings"] if f["classification"] == "unaffected_under_assessed_ranges"],
                "unknown": [f for f in report["findings"] if f["classification"] == "unknown"],
                "bibliography": bibliography, "authored_report_content": authored,
                "project_link": {"kind": "finding", "id": report_id, "namespace": namespace, "revision": 1},
                "executed_changes": False,
                "replay_hash": _hash([report["report_hash"], authored])}
