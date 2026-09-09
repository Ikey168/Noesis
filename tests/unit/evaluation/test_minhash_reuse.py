import json

import pytest

pytest.importorskip("datasketch")

from src.evaluation.minhash_reuse import (
    MinHashReuseIndex,
    ReuseRecord,
    exhaustive_adjudication,
)


def records():
    base = "Berlin evidence explains the policy change and its measured effects for residents"
    return [
        ReuseRecord("a", base, "r1"),
        ReuseRecord("b", base, "r1"),
        ReuseRecord("c", "Intro context " + base + " additional analysis", "r1"),
        ReuseRecord(
            "d",
            "Berlin evidence explains a football match and its measured attendance for residents",
            "r1",
        ),
        ReuseRecord(
            "e",
            "A revised administrative notice now withdraws the earlier conclusion",
            "r2",
            ("a",),
        ),
    ]


def test_candidate_generation_exact_adjudication_and_mandatory_provenance():
    values = records()
    index = MinHashReuseIndex(threshold=0.35, seed=7)
    for record in values:
        index.add(record)
    rows = {row["record_id"]: row for row in index.adjudicate(values[-1])}
    assert rows["a"]["mandatory_provenance_candidate"] is True
    assert rows["a"]["reuse_decision"] is True
    assert exhaustive_adjudication(values)


def test_revision_update_delete_and_replay_are_deterministic():
    values = records()
    index = MinHashReuseIndex(threshold=0.35, seed=9)
    for record in values:
        index.add(record)
    before = index.query(values[1])
    index.add(ReuseRecord("a", "Completely changed gardening document", "r2"))
    assert index.records["a"].revision == "r2"
    assert index.remove("d") is True and index.remove("d") is False
    state = index.export_state()
    replay = MinHashReuseIndex.from_state(json.loads(json.dumps(state)))
    assert replay.export_state() == state
    assert replay.query(values[1]) == index.query(values[1])
    assert before != index.query(values[1])


def test_configuration_is_versioned_and_replay_rejects_mismatch():
    index = MinHashReuseIndex(shingle_size=4, num_perm=64, threshold=0.4, seed=3)
    state = index.export_state()
    state["version"] = "wrong"
    with pytest.raises(ValueError, match="version"):
        MinHashReuseIndex.from_state(state)


def test_incoming_provenance_links_survive_lexical_miss_and_delete():
    import pytest

    pytest.importorskip("datasketch")
    from src.evaluation.minhash_reuse import MinHashReuseIndex, ReuseRecord

    index = MinHashReuseIndex(threshold=0.9)
    original = ReuseRecord("original", "Distinct original evidence about studies", "v1")
    incoming = ReuseRecord(
        "incoming",
        "Completely different text with no lexical reuse",
        "v1",
        ("original",),
    )
    index.add(original)
    index.add(incoming)
    result = index.query(original)
    assert (
        result[0]["record_id"] == "incoming"
        and result[0]["mandatory_provenance_candidate"]
    )
    index.remove("incoming")
    assert index.query(original) == []
