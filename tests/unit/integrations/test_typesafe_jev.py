from __future__ import annotations

import json

import pytest

from src.integrations.typesafe_jev import (
    JevClient,
    JevConfig,
    JevError,
    choice_question,
    noul_question,
)


def test_config_uses_official_key_and_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-test-key")
    monkeypatch.delenv("NOESIS_JEV_API_KEY", raising=False)
    monkeypatch.delenv("NOESIS_JEV_ENABLED", raising=False)
    config = JevConfig.from_env()
    assert config.api_key == "secret-test-key"
    assert JevClient.configured() is True

    monkeypatch.setenv("NOESIS_JEV_ENABLED", "false")
    assert JevConfig.from_env().api_key is None
    assert JevClient.configured() is False


def test_system_one_uses_bearer_auth_and_validates_answers():
    seen = {}

    def transport(endpoint, payload, headers, timeout):
        seen.update(
            endpoint=endpoint,
            payload=json.loads(payload),
            headers=headers,
            timeout=timeout,
        )
        return json.dumps(
            {
                "model": "jev-1.13.0",
                "answers": {"claim": {"type": "noul", "noul": 0.97}},
                "usage": {"input_tokens": 10, "output_tokens": 2},
            }
        ).encode()

    client = JevClient(
        JevConfig(api_key="private-key", timeout_seconds=3.0),
        transport=transport,
    )
    result = client.system_one(
        state={"sentence": "The launch happened."},
        questions={"claim": noul_question("Is this a factual claim?")},
    )

    assert result["answers"]["claim"]["noul"] == 0.97
    assert client.last_model == "jev-1.13.0"
    assert seen["headers"]["Authorization"] == "Bearer private-key"
    assert seen["payload"]["model"] == "jev-latest"
    assert seen["timeout"] == 3.0


def test_system_one_rejects_missing_answers_without_exposing_key():
    client = JevClient(
        JevConfig(api_key="never-print-this"),
        transport=lambda *_args: b'{"model":"jev","answers":{}}',
    )
    with pytest.raises(JevError) as excinfo:
        client.system_one(
            state="x",
            questions={"route": choice_question("Route?", {"a": "A", "b": "B"})},
        )
    assert excinfo.value.code == "invalid_response"
    assert "never-print-this" not in str(excinfo.value)


def test_question_helpers_match_system_one_shapes():
    assert noul_question("true?") == {
        "type": "noul",
        "instructions": "true?",
    }
    assert choice_question("where?", {"a": "A"}) == {
        "type": "choice",
        "instructions": "where?",
        "criteria": {"a": "A"},
    }
