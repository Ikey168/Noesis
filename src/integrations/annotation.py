"""Label Studio JSON exchange; imports produce proposals, never human votes."""

from .common import IntegrationError, digest


def export_tasks(records, *, labels):
    if not 1 <= len(records) <= 1000 or not labels or len(set(labels)) != len(labels):
        raise IntegrationError(
            "input_limit", "Bounded records and unique labels are required"
        )
    tasks = []
    seen = set()
    for record in records:
        if set(record) != {"task_id", "revision_id", "text"} or not all(
            isinstance(v, str) and v for v in record.values()
        ):
            raise IntegrationError(
                "invalid_task", "Each task needs task_id, revision_id and text"
            )
        if record["task_id"] in seen or len(record["text"]) > 1_000_000:
            raise IntegrationError("invalid_task", "Duplicate task or excessive text")
        seen.add(record["task_id"])
        data = {**record, "labels": list(labels), "text_sha256": digest(record["text"])}
        tasks.append(
            {
                "data": data,
                "meta": {
                    "noesis_contract": "label-studio-exchange-v1",
                    "input_sha256": digest(data),
                },
                "annotations": [],
                "predictions": [],
            }
        )
    return tasks


def import_annotations(exported, returned, *, reviewer_mapping, current_revisions):
    if any(not isinstance(v, str) or not v.strip() for v in reviewer_mapping.values()):
        raise IntegrationError(
            "unknown_reviewer", "Reviewer identities must be nonempty strings"
        )
    originals = {t["data"]["task_id"]: t for t in exported}
    if len(originals) != len(exported):
        raise IntegrationError("invalid_task", "Duplicate exported task")
    if len(returned) > 1000:
        raise IntegrationError("input_limit", "Too many returned tasks")
    proposals = []
    seen = set()
    for task in returned:
        data = task.get("data", {})
        identity = data.get("task_id")
        original = originals.get(identity)
        if (
            original is None
            or data != original["data"]
            or task.get("meta") != original["meta"]
        ):
            raise IntegrationError(
                "changed_task", "Task text, metadata or schema changed"
            )
        if current_revisions.get(identity) != data["revision_id"]:
            raise IntegrationError("stale_revision", "Annotation source has changed")
        if len(task.get("annotations", [])) > 100:
            raise IntegrationError("input_limit", "Too many annotations per task")
        for annotation in task.get("annotations", []):
            if len(annotation.get("result", [])) > 1000:
                raise IntegrationError("input_limit", "Too many annotation results")
            if annotation.get("was_cancelled"):
                continue
            external = str(annotation.get("completed_by"))
            if external not in reviewer_mapping:
                raise IntegrationError(
                    "unknown_reviewer", "Explicit reviewer identity mapping is required"
                )
            reviewer = reviewer_mapping[external]
            key = (identity, reviewer, str(annotation.get("id")))
            if key in seen:
                continue
            seen.add(key)
            results = []
            for result in annotation.get("result", []):
                value = result.get("value", {})
                kind = result.get("type")
                if kind == "labels":
                    start, end = value.get("start"), value.get("end")
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= len(data["text"])
                    ):
                        raise IntegrationError(
                            "invalid_span", "Invalid Unicode source offsets"
                        )
                    if value.get("text") != data["text"][start:end]:
                        raise IntegrationError(
                            "invalid_span",
                            "Annotated text does not match exact source span",
                        )
                    sources = original["meta"].get("source_spans")
                    if sources is not None and not any(
                        s["start"] <= start < end <= s["end"] for s in sources
                    ):
                        raise IntegrationError(
                            "invalid_span",
                            "Annotation crosses source-document boundaries",
                        )
                    selected = value.get("labels", [])
                elif kind == "choices":
                    selected = value.get("choices", [])
                else:
                    raise IntegrationError(
                        "invalid_annotation", "Only labels and choices are supported"
                    )
                if not selected or any(x not in data["labels"] for x in selected):
                    raise IntegrationError(
                        "invalid_label", "Label is outside the exported schema"
                    )
                results.append({"type": kind, "value": value})
            proposals.append(
                {
                    "task_id": identity,
                    "revision_id": data["revision_id"],
                    "reviewer_id": reviewer,
                    "results": results,
                    "status": "pending-review",
                    "human_verified": False,
                    "external_annotation_id": annotation.get("id"),
                    "sha256": digest(annotation),
                }
            )
    return proposals
