import copy

import pytest

from src.evaluation.entity_backends import (
    RapidFuzzCandidates,
    SplinkCandidates,
    train_splink_policy,
)


def entity(identity, name, **kwargs):
    return {
        "id": identity,
        "name": name,
        "type": "Organization",
        "revision": "revision-1",
        **kwargs,
    }


def training():
    return [
        {
            "id": "yes",
            "group_id": "training-yes",
            "split": "train",
            "label_origin": "fixture",
            "match": True,
            "left": entity("a", "Berliner Muster GmbH", address="Musterstraße 1"),
            "right": entity("b", "Berliner Muster", address="Musterstraße 1"),
        },
        {
            "id": "no",
            "group_id": "training-no",
            "split": "train",
            "label_origin": "fixture",
            "match": False,
            "left": entity("c", "Other Corp", address="Elsewhere 2"),
            "right": entity("d", "Example Ltd", address="Nowhere 3"),
        },
    ]


def test_rapidfuzz_native_batch_preserves_aliases_and_identity_guards():
    pytest.importorskip("rapidfuzz")
    source = entity("source", "Muster GmbH", identifiers={"registry": "one"})
    candidates = [
        entity("wrong", "Muster", identifiers={"registry": "two"}),
        entity("alias", "Different display", aliases=["Muster"]),
        entity("exact", "Completely different", identifiers={"registry": "one"}),
    ]
    before = copy.deepcopy(candidates)
    result = RapidFuzzCandidates(threshold=0.95).score(source, candidates)
    assert (
        candidates == before and not result["automatic_merge"] and result["ambiguous"]
    )
    assert result["candidates"][0]["id"] == "exact"
    assert not next(row for row in result["candidates"] if row["id"] == "wrong")[
        "eligible_for_review"
    ]


def test_splink_native_fitted_inference_and_frozen_policy():
    pytest.importorskip("splink")
    policy = train_splink_policy(training())
    scorer = SplinkCandidates(policy)
    values = [
        entity("one", "Muster", identifiers={"registry": "one"}),
        entity("two", "Muster", identifiers={"registry": "two"}),
        entity("three", "Different"),
    ]
    result = scorer.predict(values, evaluation_group_ids=["held-out"])
    assert len(result["candidates"]) == 3 and not result["automatic_merge"]
    conflict = next(
        row
        for row in result["candidates"]
        if {row["left_id"], row["right_id"]} == {"one", "two"}
    )
    assert (
        not conflict["eligible_for_review"]
        and conflict["field_evidence"]["name"]["comparison_level"] == 2
    )
    assert result == scorer.predict(values, evaluation_group_ids=["held-out"])
    with pytest.raises(ValueError, match="leakage"):
        scorer.predict(values, evaluation_group_ids=["training-yes"])
    policy["prior_probability"] = 0.2
    with pytest.raises(ValueError, match="changed"):
        SplinkCandidates(policy)


def test_training_does_not_accept_test_split_or_missing_provenance():
    pairs = training()
    pairs[0]["split"] = "test"
    with pytest.raises(ValueError, match="train"):
        train_splink_policy(pairs)
