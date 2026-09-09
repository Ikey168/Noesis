"""Native Label Studio JSON exchange under Noesis review and release authority."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import math

from src.kb.review_inbox import WRITE_SCOPE, ReviewInboxStore
from src.kb.review_targets import ReviewTargetError, ReviewTargets

EXPORT_SCOPE = "knowledge:inbox:export"
IMPORT_SCOPE = "knowledge:inbox:import"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _labels(values):
    if (
        not isinstance(values, list)
        or len(values) > 100
        or any(not isinstance(v, str) or not v or len(v) > 100 for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("bounded distinct annotation labels required")
    return values


def normalize_offset(text, offset, unit):
    if type(offset) is not int or offset < 0:
        raise ValueError("invalid span offset")
    if unit == "unicode-codepoints":
        if offset > len(text):
            raise ValueError("span offset outside source")
        return offset
    if unit != "utf16-code-units":
        raise ValueError("explicit supported offset unit required")
    used = 0
    for index, character in enumerate(text):
        if used == offset:
            return index
        used += 2 if ord(character) > 0xFFFF else 1
        if used > offset:
            raise ValueError("offset splits a Unicode surrogate pair")
    if used == offset:
        return len(text)
    raise ValueError("span offset outside source")


class LabelStudioExchange:
    def __init__(self, conn):
        self.conn, self.inbox = conn, ReviewInboxStore(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS review_annotation_exchanges(exchange_id TEXT PRIMARY KEY,namespace TEXT,owner TEXT,reviewer TEXT,payload_json TEXT)"
        )

    @staticmethod
    def _scope(principal_id, scopes, required):
        if not principal_id or "operator" not in scopes and required not in scopes:
            raise ReviewTargetError(
                "unauthorized", "explicit annotation transfer scope required"
            )

    def export(
        self,
        namespace,
        task_ids,
        *,
        reviewer_id,
        label_mapping,
        entity_labels=(),
        support_labels=(),
        offset_unit="unicode-codepoints",
        deployment_id,
        transfer_approved=False,
        principal_id,
        scopes,
    ):
        self._scope(principal_id, scopes, EXPORT_SCOPE)
        if (
            transfer_approved is not True
            or not isinstance(deployment_id, str)
            or not 1 <= len(deployment_id) <= 200
        ):
            raise ValueError(
                "explicit authorized transfer and deployment identity required"
            )
        if (
            not isinstance(task_ids, list)
            or not 1 <= len(task_ids) <= 1000
            or len(set(task_ids)) != len(task_ids)
        ):
            raise ValueError("bounded distinct review tasks required")
        if (
            offset_unit not in {"unicode-codepoints", "utf16-code-units"}
            or not isinstance(label_mapping, dict)
            or not label_mapping
        ):
            raise ValueError(
                "explicit offset convention and native review label mapping required"
            )
        choices = _labels(list(label_mapping))
        entities = _labels(list(entity_labels))
        support = _labels(list(support_labels))
        schema = {
            "review": label_mapping,
            "entities": entities,
            "support": support,
            "offset_unit": offset_unit,
        }
        tasks = []
        for task_id in sorted(task_ids):
            task = self.inbox.inspect(
                namespace, task_id, principal_id=principal_id, scopes=scopes
            )
            self.inbox._authorize(
                task, principal_id, scopes, WRITE_SCOPE, coordinator=True
            )
            if task["stale"] or task["resolution"]:
                raise ReviewTargetError(
                    "target_stale", "export a current unresolved review target"
                )
            if not self.conn.execute(
                "SELECT 1 FROM review_inbox_assignments WHERE task_id=? AND reviewer_id=?",
                [task_id, reviewer_id],
            ).fetchone():
                raise ReviewTargetError(
                    "unauthorized",
                    "export is limited to an assigned independent reviewer",
                )
            for label in label_mapping.values():
                ReviewTargets.validate_label(task["target"]["kind"], label)
            # One source per annotation task; other evidence references remain
            # intact on the native review target, not concatenated into false offsets.
            if len(task["sources"]) != 1:
                raise ValueError(
                    "select a single source revision for a span annotation task"
                )
            source = task["sources"][0]
            row = self.conn.execute(
                "SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                [source["document_id"], source["revision_id"]],
            ).fetchone()
            if not row:
                raise ReviewTargetError(
                    "source_unavailable", "frozen annotation source is unavailable"
                )
            text = json.loads(row[0]).get("content")
            if not isinstance(text, str) or not text or len(text) > 262144:
                raise ValueError("bounded source text required for annotation")
            data = {
                "text": text,
                "noesis_task_id": task_id,
                "source_id": source["document_id"],
                "source_revision": source["revision_id"],
                "target_revision_hash": task["target_revision_hash"],
                "schema_sha256": _digest(schema),
            }
            tasks.append(
                {
                    "data": data,
                    "meta": {"noesis": True, "related_groups": task["related_groups"]},
                }
            )
        payload = {
            "contract": "noesis-label-studio-native-v1",
            "namespace": namespace,
            "reviewer_id": reviewer_id,
            "deployment_id": deployment_id,
            "schema": schema,
            "tasks": tasks,
            "preannotations": False,
        }
        if len(_json(payload).encode()) > 16 * 1024**2:
            raise ValueError("annotation exchange exceeds 16 MiB")
        identity = "annotation-exchange:" + _digest(payload)
        self.conn.execute(
            "INSERT INTO review_annotation_exchanges VALUES (?,?,?,?,?) ON CONFLICT DO NOTHING",
            [identity, namespace, principal_id, reviewer_id, _json(payload)],
        )
        config = '<View><Text name="text" value="$text"/><Choices name="review" toName="text" choice="single" required="true">'
        config += (
            "".join(
                '<Choice value="' + html.escape(v, quote=True) + '"/>' for v in choices
            )
            + "</Choices>"
        )
        if entities:
            config += (
                '<Labels name="entities" toName="text">'
                + "".join(
                    '<Label value="' + html.escape(v, quote=True) + '"/>'
                    for v in entities
                )
                + "</Labels>"
            )
        if support:
            config += (
                '<Choices name="support" toName="text" choice="single">'
                + "".join(
                    '<Choice value="' + html.escape(v, quote=True) + '"/>'
                    for v in support
                )
                + "</Choices>"
            )
        config += "</View>"
        return {
            **payload,
            "exchange_id": identity,
            "label_config": config,
            "sha256": _digest(payload),
        }

    def import_completed(
        self, exchange_id, completed_tasks, *, reviewer_map, principal_id, scopes
    ):
        self._scope(principal_id, scopes, IMPORT_SCOPE)
        row = self.conn.execute(
            "SELECT namespace,owner,reviewer,payload_json FROM review_annotation_exchanges WHERE exchange_id=?",
            [exchange_id],
        ).fetchone()
        if not row or row[1] != principal_id and "operator" not in scopes:
            raise ReviewTargetError(
                "unauthorized", "annotation exchange is unavailable to this principal"
            )
        payload = json.loads(row[3])
        schema = payload["schema"]
        if exchange_id != "annotation-exchange:" + _digest(payload):
            raise ValueError("stored exchange changed")
        if (
            not isinstance(reviewer_map, dict)
            or not isinstance(completed_tasks, list)
            or len(completed_tasks) > 1000
            or len(_json(completed_tasks).encode()) > 16 * 1024**2
        ):
            raise ValueError(
                "bounded native export and explicit reviewer identity map required"
            )
        originals = {item["data"]["noesis_task_id"]: item for item in payload["tasks"]}
        accepted, rejected, seen = [], [], set()
        for item in completed_tasks:
            identity = item.get("data", {}).get("noesis_task_id")
            try:
                if identity not in originals or identity in seen:
                    raise ValueError("unknown_or_duplicate_task")
                seen.add(identity)
                original = originals[identity]["data"]
                if any(
                    item["data"].get(key) != value for key, value in original.items()
                ):
                    raise ValueError("source_or_schema_changed")
                annotations = item.get("annotations", [])
                if not isinstance(annotations, list) or len(annotations) != 1:
                    raise ValueError("one_independent_vote_per_export_task_required")
                annotation = annotations[0]
                external_reviewer = annotation.get("completed_by")
                if isinstance(external_reviewer, dict):
                    external_reviewer = external_reviewer.get("id")
                reviewer = reviewer_map.get(str(external_reviewer))
                if reviewer != row[2]:
                    raise ValueError("reviewer_identity_not_mapped")
                if annotation.get("was_cancelled") or not annotation.get("id"):
                    raise ValueError("cancelled_or_unidentified_annotation")
                lead_time = annotation.get("lead_time")
                if (
                    type(lead_time) not in {int, float}
                    or not math.isfinite(lead_time)
                    or not 0 <= lead_time <= 8 * 3600
                ):
                    raise ValueError("bounded_annotator_effort_required")
                task = self.inbox.inspect(
                    row[0], identity, principal_id=principal_id, scopes=scopes
                )
                self.inbox._authorize(
                    task, principal_id, scopes, WRITE_SCOPE, coordinator=True
                )
                current = self.conn.execute(
                    "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
                    [original["source_id"]],
                ).fetchone()
                if (
                    not current
                    or current[0] != original["source_revision"]
                    or task["stale"]
                ):
                    raise ValueError("stale_source_or_target_revision")
                results = annotation.get("result")
                if not isinstance(results, list) or not 1 <= len(results) <= 1000:
                    raise ValueError("bounded_native_annotation_results_required")
                approval, support_label, spans = None, None, []
                for result in results:
                    if result.get("to_name") != "text":
                        raise ValueError("incompatible_label_schema")
                    control, kind, value = (
                        result.get("from_name"),
                        result.get("type"),
                        result.get("value", {}),
                    )
                    if control in {"review", "support"} and kind == "choices":
                        labels = value.get("choices")
                        allowed = schema[control]
                        if (
                            not isinstance(labels, list)
                            or len(labels) != 1
                            or labels[0] not in allowed
                        ):
                            raise ValueError("incompatible_label_schema")
                        if control == "review":
                            if approval is not None:
                                raise ValueError("duplicate_review_label")
                            approval = copy.deepcopy(schema["review"][labels[0]])
                        else:
                            if support_label is not None:
                                raise ValueError("duplicate_support_label")
                            support_label = labels[0]
                    elif control == "entities" and kind == "labels":
                        labels = value.get("labels")
                        if (
                            not isinstance(labels, list)
                            or len(labels) != 1
                            or labels[0] not in schema["entities"]
                        ):
                            raise ValueError("incompatible_label_schema")
                        start = normalize_offset(
                            original["text"], value.get("start"), schema["offset_unit"]
                        )
                        end = normalize_offset(
                            original["text"], value.get("end"), schema["offset_unit"]
                        )
                        if not start < end or original["text"][start:end] != value.get(
                            "text"
                        ):
                            raise ValueError("unicode_offset_mismatch")
                        spans.append(
                            {
                                "start": start,
                                "end": end,
                                "text": value["text"],
                                "label": labels[0],
                            }
                        )
                    else:
                        raise ValueError("incompatible_label_schema")
                if approval is None:
                    raise ValueError("native_review_label_required")
                assisted = bool(
                    payload["preannotations"]
                    or item.get("predictions")
                    or annotation.get("parent_prediction")
                    or annotation.get("parent_annotation")
                    or annotation.get("last_action")
                    in {"prediction", "propagated_annotation"}
                )
                details = {
                    "annotation": {
                        "source_id": original["source_id"],
                        "source_revision": original["source_revision"],
                        "schema_sha256": original["schema_sha256"],
                        "entities": sorted(
                            spans, key=lambda v: (v["start"], v["end"], v["label"])
                        ),
                        "support": support_label,
                    },
                    "provenance": {
                        "exchange_id": exchange_id,
                        "external_annotation_id": annotation["id"],
                        "external_reviewer_id": str(external_reviewer),
                        "imported_by": principal_id,
                        "assisted": assisted,
                        "independence_certified": False,
                    },
                }
                outcome = self.inbox.submit(
                    row[0],
                    identity,
                    original["target_revision_hash"],
                    approval,
                    "Imported Label Studio vote; external identity is mapped, not independently certified.",
                    round(lead_time * 1000),
                    "machine" if assisted else "human",
                    principal_id=reviewer,
                    scopes=scopes,
                    annotation_details=details,
                )
                accepted.append(
                    {
                        "task_id": identity,
                        "reviewer_id": reviewer,
                        "status": outcome["status"],
                        "assisted": assisted,
                    }
                )
            except (ValueError, ReviewTargetError) as exc:
                rejected.append(
                    {"task_id": identity, "reason": getattr(exc, "code", str(exc))}
                )
        return {
            "accepted": accepted,
            "rejected": rejected,
            "automatic_adjudication": False,
            "automatic_dataset_release": False,
        }
