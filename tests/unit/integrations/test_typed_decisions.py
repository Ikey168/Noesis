import json

import pytest
from jsonschema import Draft7Validator

from src.integrations.decisions import (
    DecisionError,
    DecisionReceipt,
    DecisionRequest,
    parse_answers,
)
from src.integrations.typesafe import TypeSafeClient
from src.kb.schema_registry import _builtin_definitions


def request():
    return DecisionRequest(
        task_id="stance",
        rubric_id="stance-v1",
        state={"text": "Support the bill."},
        questions={
            "stance": {
                "type": "choice",
                "instructions": "What stance does `text` express?",
                "criteria": {"support": "Favors it", "oppose": "Opposes it"},
            },
            "strength": {
                "type": "score",
                "instructions": "How strong is the stance?",
                "criteria": ["None", "Some", "Strong"],
            },
            "explicit": {"type": "noul", "instructions": "Is the stance explicit?"},
        },
        source_binding={
            "kind": "document_revision",
            "document_id": "doc-1",
            "revision_id": "rev-2",
        },
        model="jev-1.13.0",
        policy_id="review-v1",
        calibration_id="cal-v1",
    )


def response():
    return {
        "model": "jev-1.13.0",
        "answers": {
            "stance": {
                "type": "choice",
                "choice": "support",
                "probabilities": {"support": 0.8, "oppose": 0.2},
                "confidence": 0.6,
            },
            "strength": {
                "type": "score",
                "score": 1.5,
                "legend": {"0": "None", "1": "Some", "2": "Strong"},
                "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
                "confidence": 0.4,
            },
            "explicit": {"type": "noul", "noul": 0.9},
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_complete_receipt_round_trip_and_registered_schema():
    seen = []

    def transport(**kwargs):
        seen.append(kwargs)
        return (
            200,
            {"x-typesafe-request-id": "vendor-1"},
            json.dumps(response()).encode(),
        )

    receipt = TypeSafeClient(transport=transport).decide(
        request(), api_key="secret-key", request_id="run-1"
    )
    assert seen[0]["body"]["questions"]["stance"]["type"] == "choice"
    data = receipt.as_dict()
    assert "secret-key" not in json.dumps(data)
    assert data["model_requested"] == data["model_returned"] == "jev-1.13.0"
    assert data["answers"]["stance"]["selected_probability"] == 0.8
    assert data["answers"]["stance"]["vendor_confidence"] == 0.6
    assert data["answers"]["explicit"]["vendor_confidence"] is None
    assert DecisionReceipt.from_dict(data).as_dict() == data
    definition = next(
        d for d in _builtin_definitions() if d["name"] == "typed-decision"
    )
    assert not list(Draft7Validator(definition["content"]).iter_errors(data))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["answers"].pop("stance"),
        lambda r: r["answers"]["stance"].update({"choice": "other"}),
        lambda r: r["answers"]["stance"].update(
            {"probabilities": {"support": 0.3, "oppose": 0.2}}
        ),
        lambda r: r["answers"]["stance"].update(
            {"probabilities": {"support": float("nan"), "oppose": 0.2}}
        ),
        lambda r: r["answers"]["strength"].update(
            {"legend": {"0": "None", "1": "Some", "2": "Wrong"}}
        ),
        lambda r: r["answers"]["strength"].update({"score": 2.0}),
        lambda r: r["answers"]["explicit"].pop("noul"),
        lambda r: r["usage"].update({"input_tokens": -1}),
    ],
)
def test_malformed_answers_rejected(mutate):
    payload = response()
    mutate(payload)
    with pytest.raises(DecisionError):
        parse_answers(payload, request())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "authentication_failed"),
        (422, "invalid_provider_request"),
        (429, "rate_limited"),
        (529, "provider_overloaded"),
    ],
)
def test_provider_failures_are_unavailable_not_negative(status, code):
    receipt = TypeSafeClient(transport=lambda **_: (status, {}, b"{}"))
    result = receipt.decide(request(), api_key="secret").as_dict()
    assert result["status"] == "unavailable"
    assert result["execution"]["error_code"] == code
    assert result["answers"]["explicit"]["value"] is None


def test_retry_after_requires_and_consumes_each_reservation():
    calls, reservations, sleeps = [], [], []

    def transport(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return 429, {"Retry-After": "2"}, b"{}"
        return 200, {}, json.dumps(response()).encode()

    client = TypeSafeClient(
        transport=transport, sleep=sleeps.append, monotonic=lambda: 0
    )
    with pytest.raises(DecisionError):
        client.decide(request(), api_key="secret", max_attempts=2)
    result = client.decide(
        request(),
        api_key="secret",
        max_attempts=2,
        max_cost_micros_per_attempt=10,
        reserve_attempt=lambda n, cost: reservations.append((n, cost)),
    ).as_dict()
    assert reservations == [(1, 10), (2, 10)]
    assert sleeps == [2]
    assert result["attempts"] == 2
    assert result["execution"]["reserved_cost_micros"] == 20


def test_timeout_and_malformed_json_are_unavailable():
    def timeout(**kwargs):
        raise TimeoutError("connection timed out")

    for transport, code in (
        (timeout, "timeout"),
        (lambda **_: (200, {}, b"{"), "schema_drift"),
    ):
        result = (
            TypeSafeClient(transport=transport)
            .decide(request(), api_key="secret")
            .as_dict()
        )
        assert result["status"] == "unavailable"
        assert result["execution"]["error_code"] == code


def test_round_trip_rejects_inconsistent_probabilities():
    result = (
        TypeSafeClient(transport=lambda **_: (200, {}, json.dumps(response()).encode()))
        .decide(request(), api_key="secret")
        .as_dict()
    )
    result["answers"]["stance"]["selected_probability"] = 0.6
    with pytest.raises(DecisionError):
        DecisionReceipt.from_dict(result)
