"""Project/report citation monitoring through existing durable subscriptions."""

import json

from src.kb.authored_reports import AuthoredReportStore
from src.kb.evidence_changes import EvidenceResolver
from src.kb.project_comparison import assess
from src.kb.research_projects import (
    ResearchProjectError,
    ResearchProjectStore,
    _hash,
    _json,
)
from src.kb.subscriptions import SubscriptionError, SubscriptionStore

CATEGORIES = {"revised", "withdrawn", "notice", "unavailable", "recovered"}
_DDL = """CREATE TABLE IF NOT EXISTS citation_alert_acknowledgments(
 subscription_id TEXT NOT NULL, event_id TEXT NOT NULL, principal_id TEXT NOT NULL,
 PRIMARY KEY(subscription_id,event_id,principal_id));"""


class CitationAlertStore:
    def __init__(self, conn, *, initialize=True):
        self.conn = conn
        self.subscriptions = SubscriptionStore(conn, initialize=initialize)
        self.projects = ResearchProjectStore(conn, initialize=initialize)
        self.reports = AuthoredReportStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _target(self, target, principal_id, scopes):
        if (
            not isinstance(target, dict)
            or set(target) != {"kind", "namespace", "id", "revision"}
            or target["kind"] not in {"project", "report"}
            or type(target["revision"]) is not int
            or target["revision"] < 1
        ):
            raise ResearchProjectError(
                "invalid_target",
                "pin a project or report namespace, identity and revision",
            )
        store = self.projects if target["kind"] == "project" else self.reports
        return store.inspect(
            target["namespace"],
            target["id"],
            revision=target["revision"],
            principal_id=principal_id,
            scopes=scopes,
        )

    def _dependencies(self, target, principal_id, scopes):
        state = self._target(target, principal_id, scopes)
        dependencies = {}

        def add(dep, affected):
            document_id = (
                dep["id"]
                if dep["kind"] == "source"
                else dep.get("locator", {}).get("document_id")
            )
            if (
                document_id
                and "operator" not in scopes
                and f"document:{document_id}:read" not in scopes
            ):
                raise SubscriptionError(
                    "unauthorized", "current cited-document access required"
                )
            key = _hash(dep)
            dependencies.setdefault(key, {"dependency": dep, "affected": []})[
                "affected"
            ].append(affected)

        if target["kind"] == "report":
            for section in state["content"]["sections"]:
                for assertion in section["assertions"]:
                    for dep in assertion["dependencies"]:
                        add(
                            dep,
                            {
                                "section_id": section["id"],
                                "assertion_id": assertion["id"],
                            },
                        )
        else:
            result = assess(self.conn, state, scopes)
            if any(x["reason"] == "inaccessible_source" for x in result["omissions"]):
                raise ResearchProjectError(
                    "unauthorized", "current access to all cited sources is required"
                )
            for link in state["links"]:
                if link["kind"] in {"evidence", "finding"}:
                    loc = link.get("locator", {})
                    if not loc.get("document_id") or not loc.get("revision_id"):
                        raise ResearchProjectError(
                            "unpinned_source",
                            "project citations require document and revision locators",
                        )
                    add(
                        {
                            "kind": "source",
                            "namespace": link.get("namespace", target["namespace"]),
                            "id": loc["document_id"],
                            "revision": loc["revision_id"],
                            "locator": loc,
                        },
                        {"kind": link["kind"], "id": link["id"]},
                    )
            for finding in result["findings"].values():
                for source in finding["supports"]:
                    add(
                        {
                            "kind": "source",
                            "namespace": finding["reference"].get(
                                "namespace", target["namespace"]
                            ),
                            "id": source["document_id"],
                            "revision": source["revision_id"],
                            "locator": source["locator"],
                        },
                        {"kind": "finding", "id": finding["reference"]["id"]},
                    )
        if (
            len(dependencies) > 1000
            or sum(len(v["affected"]) for v in dependencies.values()) > 1000
        ):
            raise ResearchProjectError(
                "citation_limit",
                "at most 1000 distinct citation dependencies per subscription",
            )
        return dependencies

    def create(
        self, target, request_key, categories, batch_size, *, principal_id, scopes
    ):
        self._dependencies(target, principal_id, scopes)
        if (
            not isinstance(categories, list)
            or not categories
            or set(categories) - CATEGORIES
            or len(set(categories)) != len(categories)
        ):
            raise SubscriptionError(
                "invalid_categories",
                "choose unique supported citation change categories",
            )
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise SubscriptionError("invalid_limit", "batch size must be 1..100")
        return self.subscriptions.create(
            {
                "namespace": target["namespace"],
                "query": {"operation": "evidence", "citation_target": target},
                "filters": {"categories": sorted(categories), "batch_size": batch_size},
                "cadence": {"trigger": "manual"},
                "delivery": {"kind": "poll"},
            },
            request_key,
            principal_id=principal_id,
            scopes=scopes,
        )

    def inspect(self, subscription_id, *, principal_id, scopes):
        sub = self.subscriptions.inspect(
            subscription_id, principal_id=principal_id, scopes=scopes
        )
        target = sub["query"].get("citation_target")
        if not target:
            raise SubscriptionError(
                "invalid_subscription", "a citation subscription is required"
            )
        self._dependencies(target, principal_id, scopes)
        return sub

    def set_status(self, subscription_id, status, *, principal_id, scopes):
        self.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if status == "deleted":
            return self.subscriptions.delete(
                subscription_id, principal_id=principal_id, scopes=scopes
            )
        return self.subscriptions.set_status(
            subscription_id, status, principal_id=principal_id, scopes=scopes
        )

    @staticmethod
    def _summary(row):
        if row is None:
            return None
        # Do not copy source body payloads or unrelated metadata into alerts.
        fields = (
            "document_id",
            "revision_id",
            "revision",
            "content_hash",
            "lifecycle",
            "generation",
            "state_id",
            "logical_id",
            "artifact_id",
        )
        return {k: row[k] for k in fields if k in row}

    def evaluate(self, subscription_id, *, principal_id, scopes):
        sub = self.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        if sub["status"] != "active":
            return {
                "subscription_id": subscription_id,
                "status": "skipped",
                "reason": sub["status"],
            }
        deps = self._dependencies(sub["query"]["citation_target"], principal_id, scopes)
        resolver = EvidenceResolver(self.conn, scopes)
        self.conn.execute("BEGIN")
        try:
            items = []
            for key, value in sorted(deps.items()):
                current = resolver.compare(value["dependency"])
                if current["reason"].endswith("access_unavailable"):
                    raise SubscriptionError(
                        "unauthorized", "current access to cited evidence is required"
                    )
                category = (
                    "unavailable"
                    if current["status"] == "uncertain"
                    else "withdrawn"
                    if current["reason"] == "confirmed_withdrawal"
                    or (current.get("after") or {}).get("lifecycle")
                    in {"retracted", "deleted", "withdrawn"}
                    else "notice"
                    if current["reason"] == "provider_notice_requires_review"
                    else "revised"
                    if current["status"] == "affected"
                    else "current"
                )
                if category == "withdrawn" and value["dependency"]["kind"] == "source":
                    metadata = (
                        json.loads(current["after"].get("payload_json") or "{}").get(
                            "metadata"
                        )
                        or {}
                    )
                    explicit = metadata.get("tombstone") or str(
                        metadata.get("lifecycle") or metadata.get("status") or ""
                    ).lower() in {
                        "deleted",
                        "tombstone",
                        "removed",
                        "retracted",
                        "withdrawn",
                    }
                    if not explicit:
                        category = "notice"
                        current["reason"] = "inferred_withdrawal_requires_review"
                items.append(
                    {
                        "id": key,
                        "category": category,
                        "reason": current["reason"],
                        "dependency": value["dependency"],
                        "affected": sorted(value["affected"], key=_hash),
                        "before_revision": self._summary(current["before"]),
                        "after_revision": self._summary(current["after"]),
                        "notices": [
                            {
                                k: v
                                for k, v in notice.items()
                                if k
                                in {
                                    "notice_id",
                                    "notice_document_id",
                                    "notice_type",
                                    "provider",
                                    "notice_date",
                                    "input_id",
                                }
                            }
                            for notice in current.get("details", [])
                        ],
                        "target": sub["query"]["citation_target"],
                        "support_verified": False,
                    }
                )
            result = {
                "items": items,
                "coverage": {
                    "complete": all(x["category"] != "unavailable" for x in items)
                },
            }
            if len(_json(result).encode()) > 8 * 1024**2:
                raise SubscriptionError("citation_limit", "observation exceeds 8 MiB")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        # Native source-pack watermarks are per pack (direct observations use
        # zero), so they cannot be used as a global subscription cursor. Commit
        # an explicit observation of the resolved evidence instead. The origin
        # metadata distinguishes this cursor from an ingestion generation.
        detail = {
            "origin": "citation-observation-v1",
            "subscription_id": subscription_id,
            "observation_sha256": _hash(result),
            "basis": "committed evidence plus explicit pending/unavailable diagnostics",
        }
        self.conn.execute("BEGIN")
        try:
            prior = self.conn.execute(
                "SELECT watermark,detail_json FROM knowledge_subscription_watermarks WHERE namespace=? ORDER BY watermark DESC LIMIT 1",
                [sub["namespace"]],
            ).fetchone()
            duplicate = self.conn.execute(
                "SELECT watermark FROM knowledge_subscription_watermarks WHERE namespace=? AND detail_json=? ORDER BY watermark DESC LIMIT 1",
                [sub["namespace"], _json(detail)],
            ).fetchone()
            # Reuse only the last evaluated observation: a later recovery back
            # to earlier content must still produce a new transition.
            if duplicate and duplicate[0] == sub["last_watermark"]:
                watermark = duplicate[0]
            else:
                watermark = (prior[0] if prior else 0) + 1
                self.subscriptions.commit_watermark(
                    sub["namespace"], watermark, kind="consolidation", detail=detail
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.subscriptions.evaluate(
            subscription_id, watermark, result, principal_id=principal_id, scopes=scopes
        )

    def poll(self, subscription_id, *, principal_id, scopes, cursor=""):
        sub = self.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        batch = self.subscriptions.poll(
            subscription_id,
            principal_id=principal_id,
            scopes=scopes,
            cursor=cursor,
            limit=sub["filters"]["batch_size"],
        )
        events = []
        for event in batch["events"]:
            before, after = event["before"], event["after"]
            if not after or "category" not in after:
                continue
            category = after["category"]
            if category == "current":
                if not before or before.get("category") == "current":
                    continue
                category = "recovered"
            elif (
                category == "revised"
                and before
                and before.get("category") == "unavailable"
            ):
                category = "recovered"
            if category not in sub["filters"]["categories"]:
                continue
            # Revalidate historical dependencies as well as the current target.
            for item in (before, after):
                if item and EvidenceResolver(self.conn, scopes).compare(
                    item["dependency"]
                )["reason"].endswith("access_unavailable"):
                    raise SubscriptionError(
                        "unauthorized", "current evidence access required for replay"
                    )
            ack = self.conn.execute(
                "SELECT 1 FROM citation_alert_acknowledgments WHERE subscription_id=? AND event_id=? AND principal_id=?",
                [subscription_id, event["event_id"], principal_id],
            ).fetchone()
            target = after["target"]
            actions = []
            if after["dependency"]["kind"] == "source":
                for label in ("before_revision", "after_revision"):
                    revision = after.get(label)
                    if revision:
                        actions.append(
                            {
                                "tool": "document_revision",
                                "document_id": revision["document_id"],
                                "revision": revision["revision"],
                                "include_retracted": True,
                                "side": label,
                            }
                        )
            if target["kind"] == "report":
                actions.append(
                    {
                        "tool": "assess_authored_report_changes",
                        "namespace": target["namespace"],
                        "report_id": target["id"],
                    }
                )
            else:
                actions.append(
                    {
                        "tool": "inspect_research_project",
                        "namespace": target["namespace"],
                        "project_id": target["id"],
                    }
                )
                actions.append(
                    {
                        "tool": "list_review_inbox_tasks",
                        "namespace": target["namespace"],
                        "project_id": target["id"],
                    }
                )
            events.append(
                {
                    **event,
                    "contract": "noesis-citation-alert-v1",
                    "category": category,
                    "acknowledged": bool(ack),
                    "actions": actions,
                }
            )
        return {
            **batch,
            "events": events,
            "aggregation": "bounded ordered batch; all source revision events retained",
        }

    def acknowledge(self, subscription_id, event_id, *, principal_id, scopes):
        self.inspect(subscription_id, principal_id=principal_id, scopes=scopes)
        self.subscriptions._get(subscription_id, principal_id, scopes, write=True)
        row = self.conn.execute(
            "SELECT after_json FROM knowledge_subscription_events WHERE subscription_id=? AND event_id=?",
            [subscription_id, event_id],
        ).fetchone()
        if not row:
            raise SubscriptionError(
                "event_unavailable", "event does not belong to this subscription"
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_alert_acknowledgments VALUES (?,?,?)",
            [subscription_id, event_id, principal_id],
        )
        return {
            "event_id": event_id,
            "acknowledged": True,
            "evidence_approved": False,
            "report_modified": False,
        }
