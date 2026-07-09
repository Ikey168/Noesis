"""Unit tests for the vision describer (B1): fallback + injected model path."""

from __future__ import annotations

from src.ingestion.describers.vision import FigureDescription, VisionDescriber


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
    assert VisionDescriber(complete=lambda *a: "   ").describe(b"bytes", context="x") is None
