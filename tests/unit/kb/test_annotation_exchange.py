import copy

import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.annotation_exchange import (
    EXPORT_SCOPE,
    IMPORT_SCOPE,
    LabelStudioExchange,
    normalize_offset,
)
from src.kb.review_datasets import DATASET_SCOPE, ReviewDatasetStore
from tests.unit.kb.test_review_inbox import setup


def preparation():
    inbox, _, (task,), scopes = setup()
    scopes |= {EXPORT_SCOPE, IMPORT_SCOPE, DATASET_SCOPE}
    auth = {"principal_id": "coordinator", "scopes": scopes}
    inbox.assign("r", task["task_id"], ["alice", "bob"], **auth)
    exchange = LabelStudioExchange(inbox.conn)
    return inbox, task, exchange, auth


def export(exchange, task, auth, reviewer):
    return exchange.export(
        "r",
        [task["task_id"]],
        reviewer_id=reviewer,
        label_mapping={"Accept": {"decision": "accepted"}},
        entity_labels=["NAME"],
        support_labels=["entailed", "insufficient"],
        deployment_id="local-test",
        transfer_approved=True,
        **auth,
    )


def native(exported, reviewer, *, support="entailed", assisted=False):
    task = copy.deepcopy(exported["tasks"][0])
    task["id"] = 42
    task["annotations"] = [
        {
            "id": 100 + reviewer,
            "completed_by": reviewer,
            "lead_time": 1.25,
            "parent_prediction": 123 if assisted else None,
            "result": [
                {
                    "from_name": "review",
                    "to_name": "text",
                    "type": "choices",
                    "value": {"choices": ["Accept"]},
                },
                {
                    "from_name": "support",
                    "to_name": "text",
                    "type": "choices",
                    "value": {"choices": [support]},
                },
                {
                    "from_name": "entities",
                    "to_name": "text",
                    "type": "labels",
                    "value": {
                        "start": 0,
                        "end": 8,
                        "text": "Original",
                        "labels": ["NAME"],
                    },
                },
            ],
        }
    ]
    return [task]


def test_native_label_studio_votes_feed_existing_release_with_details():
    inbox, task, exchange, auth = preparation()
    for index, reviewer in enumerate(("alice", "bob"), 1):
        outgoing = export(exchange, task, auth, reviewer)
        completed = native(outgoing, index)
        result = exchange.import_completed(
            outgoing["exchange_id"],
            completed,
            reviewer_map={str(index): reviewer},
            **auth,
        )
        assert len(result["accepted"]) == 1 and result["rejected"] == []
        replay = exchange.import_completed(
            outgoing["exchange_id"],
            completed,
            reviewer_map={str(index): reviewer},
            **auth,
        )
        assert len(replay["accepted"]) == 1
    assert inbox.inspect("r", task["task_id"], **auth)["status"] == "consensus_ready"
    inbox.resolve("r", task["task_id"], "Independent agreement", **auth)
    dataset = ReviewDatasetStore(inbox.conn)
    draft = dataset.build_dataset("r", [task["task_id"]], **auth)
    assert draft["rows"][0]["annotation"]["entities"][0]["text"] == "Original"
    assert len(draft["rows"][0]["annotation_provenance"]) == 2
    assert draft["status"] == "draft" and draft["effort"]["self_reported_ms"] == 2500


def test_incompatible_labels_stale_source_and_assistance_are_explicit():
    inbox, task, exchange, auth = preparation()
    outgoing = export(exchange, task, auth, "alice")
    completed = native(outgoing, 1)
    completed[0]["annotations"][0]["result"][2]["value"]["labels"] = ["UNKNOWN"]
    assert (
        exchange.import_completed(
            outgoing["exchange_id"], completed, reviewer_map={"1": "alice"}, **auth
        )["rejected"][0]["reason"]
        == "incompatible_label_schema"
    )
    assisted = exchange.import_completed(
        outgoing["exchange_id"],
        native(outgoing, 1, assisted=True),
        reviewer_map={"1": "alice"},
        **auth,
    )
    assert assisted["accepted"][0]["assisted"]
    assert (
        inbox.inspect("r", task["task_id"], **auth)["votes"][0]["annotation_origin"]
        == "machine"
    )
    outgoing = export(exchange, task, auth, "bob")
    DocumentRevisionStore(inbox.conn).observe(
        {"document_id": "doc:0", "content": "Changed source"}
    )
    assert (
        exchange.import_completed(
            outgoing["exchange_id"],
            native(outgoing, 2),
            reviewer_map={"2": "bob"},
            **auth,
        )["rejected"][0]["reason"]
        == "stale_source_or_target_revision"
    )


def test_conflicting_detail_labels_are_disputed_not_training_consensus():
    inbox, task, exchange, auth = preparation()
    for index, reviewer in enumerate(("alice", "bob"), 1):
        outgoing = export(exchange, task, auth, reviewer)
        result = exchange.import_completed(
            outgoing["exchange_id"],
            native(
                outgoing, index, support="entailed" if index == 1 else "insufficient"
            ),
            reviewer_map={str(index): reviewer},
            **auth,
        )
        assert result["accepted"]
    assert inbox.inspect("r", task["task_id"], **auth)["status"] == "disputed"
    assert (
        ReviewDatasetStore(inbox.conn).build_dataset("r", [task["task_id"]], **auth)[
            "rows"
        ]
        == []
    )


def test_unicode_offsets_are_explicit_and_never_split_surrogates():
    assert normalize_offset("🧠Müller", 2, "utf16-code-units") == 1
    assert normalize_offset("🧠Müller", 1, "unicode-codepoints") == 1
    with pytest.raises(ValueError, match="surrogate"):
        normalize_offset("🧠Müller", 1, "utf16-code-units")
