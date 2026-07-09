"""
Vision describer (Track B / B1).

Runs a vision-language model over a figure/image at ingest and returns a
description aimed at what mining needs — chart type, axes, units, the headline
relationship, and any legible numbers — so a figure becomes searchable, minable,
and citable while the pipeline stays text-native.

Key-gated with graceful degradation: with no key, no SDK, an API failure, or no
image bytes, :meth:`describe` returns ``None`` and the caller falls back to
caption-only. Ingestion therefore never blocks on the model.

Configuration (all optional):

* ``NOESIS_VISION``           — ``auto`` (default) or ``off``.
* ``NOESIS_VISION_PROVIDER``  — ``anthropic`` (default); auto from key presence.
* ``NOESIS_VISION_MODEL``     — model id override.
* ``ANTHROPIC_API_KEY``.

See ``docs/architecture/VISUAL_EVIDENCE_PLAN.md``.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
_DEFAULT_MODEL = "claude-sonnet-5"
_MAX_OUTPUT_TOKENS = 400

_PROMPT = (
    "You are describing a figure from a document so it can be searched and "
    "fact-checked. In 2-4 sentences state: the figure/chart type, what the axes "
    "or categories are and their units, the single headline relationship it "
    "shows, and any legible numeric values. Do NOT invent values you cannot "
    "read. If a value is read off a chart, say it is approximate. Caption/context "
    "for grounding: {context}"
)


@dataclass
class FigureDescription:
    """A model-produced description of a figure, provenance-stamped."""

    text: str
    model: str
    prompt_version: str = PROMPT_VERSION
    approximate: bool = True

    def to_metadata(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "prompt_version": self.prompt_version,
            "approximate": self.approximate,
        }


def vision_enabled() -> bool:
    return os.getenv("NOESIS_VISION", "auto").strip().lower() not in ("off", "0", "false")


def _resolve_config() -> Optional[Dict[str, str]]:
    if not vision_enabled():
        return None
    provider = os.getenv("NOESIS_VISION_PROVIDER", "anthropic").strip().lower() or "anthropic"
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("NOESIS_VISION_MODEL", "").strip() or _DEFAULT_MODEL
    return {"provider": provider, "model": model, "api_key": api_key}


def _describe_anthropic(config: Dict[str, str], image_b64: str, mime: str, prompt: str) -> Optional[str]:
    import anthropic  # lazy: optional dependency

    client = anthropic.Anthropic(api_key=config["api_key"])
    response = client.messages.create(
        model=config["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime, "data": image_b64}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts).strip() or None


class VisionDescriber:
    """Key-gated VLM describer with a caption-only fallback.

    ``complete`` is injectable so the model-call path is exercised in tests
    without a key or the SDK: it receives ``(config, image_b64, mime, prompt)``
    and returns the description text or None.
    """

    def __init__(self, complete: Optional[Callable[[Dict[str, str], str, str, str], Optional[str]]] = None):
        self._complete = complete or _describe_anthropic

    @property
    def configured(self) -> bool:
        return _resolve_config() is not None

    def describe(
        self,
        image_bytes: Optional[bytes],
        context: str = "",
        mime: str = "image/png",
    ) -> Optional[FigureDescription]:
        """Describe an image, or return None to signal caption-only fallback.

        None whenever: describing is off / no key, no image bytes, the SDK is
        missing, or the call fails or returns empty. The caller uses the caption.
        """
        if not image_bytes:
            return None
        config = _resolve_config()
        if config is None:
            return None
        try:
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
            prompt = _PROMPT.format(context=context or "(none)")
            text = self._complete(config, image_b64, mime, prompt)
        except Exception:  # noqa: BLE001 - any failure degrades to caption-only
            logger.warning("VisionDescriber: describe failed — caption-only fallback", exc_info=True)
            return None
        if not text or not text.strip():
            return None
        return FigureDescription(text=text.strip(), model=config["model"])
