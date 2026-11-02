"""Unit tests for the vision describer (B1): fallback, injected model path,
provenance, and the local Ollama provider over a mocked HTTP layer."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.describers.figures import FigureCandidate, figure_documents
from src.ingestion.describers.vision import (
    FigureDescription,
    VisionDescriber,
    VisionDescriberError,
    VisionModelNotAvailable,
    VisionProviderResponseError,
    VisionProviderUnavailable,
)


def test_no_image_bytes_returns_none():
    assert VisionDescriber().describe(None, context="Figure 1") is None
    assert VisionDescriber().describe(b"", context="Figure 1") is None


def test_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    d = VisionDescriber()
    assert d.configured is False
    assert d.describe(b"\x89PNG...", context="a chart") is None


def test_disabled_returns_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NOESIS_VISION", "off")
    assert VisionDescriber().describe(b"bytes", context="x") is None


def test_injected_model_path(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("NOESIS_VISION", "auto")
    captured = {}

    def fake_complete(config, image_b64, mime, prompt):
        captured["mime"] = mime
        captured["has_image"] = bool(image_b64)
        captured["prompt"] = prompt
        return "A line chart of temperature anomaly rising to +1.4C by 2024."

    d = VisionDescriber(complete=fake_complete)
    assert d.configured is True
    desc = d.describe(b"\x89PNG\r\n", context="Figure 3: temperature", mime="image/png")
    assert isinstance(desc, FigureDescription)
    assert "temperature anomaly" in desc.text
    assert desc.model  # provenance stamped
    assert desc.approximate is True
    assert captured["mime"] == "image/png"
    assert captured["has_image"] is True
    assert "temperature" in captured["prompt"]


def test_model_failure_degrades_to_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    def boom(config, image_b64, mime, prompt):
        raise RuntimeError("api down")

    assert VisionDescriber(complete=boom).describe(b"bytes", context="x") is None


def test_empty_model_output_is_none(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert (
        VisionDescriber(complete=lambda *a: "   ").describe(b"bytes", context="x")
        is None
    )


# --------------------------------------------------------------------------- #
# Provenance + local Ollama provider (mocked HTTP layer, no network)
# --------------------------------------------------------------------------- #

_DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def ollama_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NOESIS_VISION_MODEL", raising=False)
    monkeypatch.delenv("NOESIS_OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("NOESIS_VISION_TIMEOUT_S", raising=False)
    monkeypatch.setenv("NOESIS_VISION", "auto")
    monkeypatch.setenv("NOESIS_VISION_PROVIDER", "ollama")


def _client(handler):
    return httpx.Client(
        base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler)
    )


def _ollama_handler(seen, *, chat_status=200, chat_body=None, tags=True):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/chat":
            body = (
                chat_body
                if chat_body is not None
                else {
                    "model": "qwen2.5vl:7b",
                    "message": {
                        "role": "assistant",
                        "content": "A bar chart of CO2 emissions by sector (approx.).",
                    },
                    "done": True,
                }
            )
            return httpx.Response(chat_status, json=body)
        if request.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.9.0"})
        if request.url.path == "/api/tags" and tags:
            return httpx.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "llava:latest",
                            "model": "llava:latest",
                            "digest": "sha256:other",
                        },
                        {
                            "name": "qwen2.5vl:7b",
                            "model": "qwen2.5vl:7b",
                            "digest": _DIGEST,
                        },
                    ]
                },
            )
        return httpx.Response(404, json={"error": "not found"})

    return handler


def test_anthropic_description_records_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("NOESIS_VISION_PROVIDER", raising=False)
    desc = VisionDescriber(complete=lambda *a: "A chart.").describe(b"img", context="x")
    assert desc.provider == "anthropic"
    meta = desc.to_metadata()
    assert meta["provider"] == "anthropic" and meta["model"] == "claude-sonnet-5"
    assert isinstance(meta["latency_ms"], int)


def test_ollama_configured_without_any_key(ollama_env):
    d = VisionDescriber()
    assert d.configured is True
    assert d.provider == "ollama"


def test_auto_provider_never_selects_local(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("NOESIS_VISION_PROVIDER", raising=False)
    assert VisionDescriber().configured is False


def test_unknown_provider_is_off(monkeypatch):
    monkeypatch.setenv("NOESIS_VISION_PROVIDER", "nope")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert VisionDescriber().configured is False


def test_ollama_describe_records_provider_model_digest_version(ollama_env):
    seen = []
    d = VisionDescriber(http_client=_client(_ollama_handler(seen)))
    desc = d.describe(
        b"\x89PNG\r\nfake", context="Figure 2: emissions", mime="image/png"
    )
    assert isinstance(desc, FigureDescription)
    assert desc.text.startswith("A bar chart")
    meta = desc.to_metadata()
    assert meta["provider"] == "ollama"
    assert meta["model"] == "qwen2.5vl:7b"
    assert meta["model_digest"] == _DIGEST
    assert meta["provider_version"] == "0.9.0"
    assert d.last_error is None

    chat = next(r for r in seen if r.url.path == "/api/chat")
    assert chat.method == "POST"
    payload = json.loads(chat.content)
    assert payload["model"] == "qwen2.5vl:7b"
    assert payload["stream"] is False
    message = payload["messages"][0]
    assert base64.b64decode(message["images"][0]) == b"\x89PNG\r\nfake"
    assert "emissions" in message["content"]


def test_ollama_model_override_and_latest_tag_digest(ollama_env, monkeypatch):
    monkeypatch.setenv("NOESIS_VISION_MODEL", "llava")
    seen = []
    handler = _ollama_handler(
        seen, chat_body={"model": "llava", "message": {"content": "A photo."}}
    )
    desc = VisionDescriber(http_client=_client(handler)).describe(b"img")
    assert desc.model == "llava"
    assert desc.model_digest == "sha256:other"  # "llava" matches "llava:latest"
    assert (
        json.loads(next(r for r in seen if r.url.path == "/api/chat").content)["model"]
        == "llava"
    )


def test_ollama_missing_provenance_endpoints_still_describes(ollama_env):
    def handler(request):
        if request.url.path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "A map."}})
        return httpx.Response(500)

    desc = VisionDescriber(http_client=_client(handler)).describe(b"img")
    assert desc.text == "A map." and desc.provider == "ollama"
    assert desc.model_digest is None and "model_digest" not in desc.to_metadata()


def test_ollama_unreachable_is_typed_and_degrades(ollama_env):
    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    d = VisionDescriber(http_client=_client(refuse))
    assert d.describe(b"img", context="x") is None
    assert isinstance(d.last_error, VisionProviderUnavailable)
    meta = d.last_error.to_metadata()
    assert meta == {
        **meta,
        "status": "degraded",
        "error": "provider_unavailable",
        "provider": "ollama",
    }

    with pytest.raises(VisionProviderUnavailable) as info:
        d.describe(b"img", context="x", raise_on_error=True)
    assert isinstance(info.value.__cause__, httpx.ConnectError)


def test_ollama_timeout_is_unavailable(ollama_env):
    def slow(request):
        raise httpx.ReadTimeout("timed out", request=request)

    d = VisionDescriber(http_client=_client(slow))
    assert d.describe(b"img") is None
    assert isinstance(d.last_error, VisionProviderUnavailable)
    assert "timed out" in str(d.last_error)


def test_ollama_model_not_pulled(ollama_env):
    handler = _ollama_handler(
        [],
        chat_status=404,
        chat_body={"error": 'model "qwen2.5vl:7b" not found, try pulling it first'},
    )
    d = VisionDescriber(http_client=_client(handler))
    with pytest.raises(VisionModelNotAvailable, match="ollama pull qwen2.5vl:7b"):
        d.describe(b"img", raise_on_error=True)


@pytest.mark.parametrize(
    "status,body",
    [
        (500, {"error": "out of memory"}),
        (200, {"done": True}),
        (200, ["not", "a", "dict"]),
    ],
)
def test_ollama_bad_responses_are_typed(ollama_env, status, body):
    d = VisionDescriber(
        http_client=_client(_ollama_handler([], chat_status=status, chat_body=body))
    )
    assert d.describe(b"img") is None
    assert isinstance(d.last_error, VisionProviderResponseError)


def test_ollama_empty_content_is_none_without_error(ollama_env):
    handler = _ollama_handler([], chat_body={"message": {"content": "  "}})
    d = VisionDescriber(http_client=_client(handler))
    assert d.describe(b"img") is None
    assert d.last_error is None


def test_ollama_rejects_non_http_base_url(ollama_env, monkeypatch):
    monkeypatch.setenv("NOESIS_OLLAMA_BASE_URL", "file:///etc/passwd")
    assert VisionDescriber().configured is False


def test_ollama_default_base_url_is_loopback(ollama_env):
    from src.ingestion.describers import vision

    config = vision._resolve_config()
    assert config["base_url"] == "http://127.0.0.1:11434"
    assert vision._is_loopback(config["base_url"])
    assert float(config["timeout_s"]) == 180.0


def test_generic_failure_wrapped_in_typed_error(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("NOESIS_VISION_PROVIDER", raising=False)

    def boom(*a):
        raise RuntimeError("api down")

    d = VisionDescriber(complete=boom)
    assert d.describe(b"img") is None
    assert type(d.last_error) is VisionDescriberError
    assert d.last_error.provider == "anthropic"
    with pytest.raises(VisionDescriberError) as info:
        d.describe(b"img", raise_on_error=True)
    assert isinstance(info.value.__cause__, RuntimeError)


def test_figure_document_records_local_provider_and_degradation(ollama_env):
    parent = Document(
        document_id="paper:9",
        source_type="paper",
        language="en",
        ingested_at=1,
        title="P",
        content="body",
    )
    cand = FigureCandidate(
        label="Figure 1", caption="Emissions by sector", image_bytes=b"img"
    )

    ok = VisionDescriber(http_client=_client(_ollama_handler([])))
    fig = figure_documents(parent, candidates=[cand], describer=ok)[0]
    assert fig.metadata["describer"]["provider"] == "ollama"
    assert fig.metadata["describer"]["model_digest"] == _DIGEST
    assert "describer_degraded" not in fig.metadata

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    down = VisionDescriber(http_client=_client(refuse))
    fig = figure_documents(parent, candidates=[cand], describer=down)[0]
    assert fig.metadata["describer"] is None
    assert fig.content == "Figure 1: Emissions by sector"  # caption-only fallback
    assert fig.metadata["describer_degraded"]["error"] == "provider_unavailable"
    assert fig.metadata["describer_degraded"]["provider"] == "ollama"
