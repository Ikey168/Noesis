"""
Vision describer (Track B / B1).

Runs a vision-language model over a figure/image at ingest and returns a
description aimed at what mining needs — chart type, axes, units, the headline
relationship, and any legible numbers — so a figure becomes searchable, minable,
and citable while the pipeline stays text-native.

Two providers sit behind one interface:

* ``anthropic`` — paid API, needs ``ANTHROPIC_API_KEY``.
* ``ollama``    — a local open-weight vision model served by Ollama over its
  HTTP API (``POST /api/chat`` with a base64 image). No key, no paid API.

Graceful degradation: with describing off, no provider configured, no image
bytes, or a provider failure, :meth:`VisionDescriber.describe` returns ``None``
and the caller falls back to caption-only, so ingestion never blocks on the
model. A provider *failure* is not silent: it is raised internally as a typed
:class:`VisionDescriberError` (e.g. :class:`VisionProviderUnavailable` when the
local Ollama server is unreachable), kept on ``VisionDescriber.last_error``, and
recorded by the figure emitter as ``describer_degraded`` metadata. Callers that
want the exception pass ``raise_on_error=True``.

Every description records the provider, model, and — for Ollama when the
server reports them — the model digest and server version.

Configuration (all optional):

* ``NOESIS_VISION``               — ``auto`` (default) or ``off``.
* ``NOESIS_VISION_PROVIDER``      — ``auto`` (default: ``anthropic`` when
  ``ANTHROPIC_API_KEY`` is set, otherwise off), ``anthropic``, or ``ollama``.
  The local provider is opt-in; ``auto`` never probes a local server.
* ``NOESIS_VISION_MODEL``         — model id override for the chosen provider
  (defaults: ``claude-sonnet-5`` / ``qwen2.5vl:7b``).
* ``NOESIS_OLLAMA_BASE_URL``      — Ollama server URL, default
  ``http://127.0.0.1:11434`` (loopback).
* ``NOESIS_VISION_TIMEOUT_S``     — per-image request timeout for the local
  provider, default 180 seconds (CPU inference is slow).
* ``ANTHROPIC_API_KEY``.

See ``docs/subsystems/vision-describer.md``.
"""

from __future__ import annotations

import base64
import ipaddress
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

PROMPT_VERSION = "v1"
_DEFAULT_MODEL = "claude-sonnet-5"  # anthropic default (kept for compatibility)
_DEFAULT_MODELS = {
    "anthropic": _DEFAULT_MODEL,
    "ollama": "qwen2.5vl:7b",
}
_PROVIDERS = frozenset(_DEFAULT_MODELS)
_DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
_DEFAULT_LOCAL_TIMEOUT_S = 180.0
_MAX_OUTPUT_TOKENS = 400

_PROMPT = (
    "You are describing a figure from a document so it can be searched and "
    "fact-checked. In 2-4 sentences state: the figure/chart type, what the axes "
    "or categories are and their units, the single headline relationship it "
    "shows, and any legible numeric values. Do NOT invent values you cannot "
    "read. If a value is read off a chart, say it is approximate. Caption/context "
    "for grounding: {context}"
)


# --------------------------------------------------------------------------- #
# Typed errors
# --------------------------------------------------------------------------- #


class VisionDescriberError(RuntimeError):
    """A configured vision provider failed to produce a description."""

    kind = "provider_error"

    def __init__(self, message: str, *, provider: str = "", model: str = ""):
        super().__init__(message)
        self.provider = provider
        self.model = model

    def to_metadata(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "error": self.kind,
            "provider": self.provider,
            "model": self.model,
            "detail": str(self),
        }


class VisionProviderUnavailable(VisionDescriberError):
    """The provider could not be reached (connection refused, DNS, timeout)."""

    kind = "provider_unavailable"


class VisionModelNotAvailable(VisionDescriberError):
    """The provider is reachable but the configured model is not installed."""

    kind = "model_not_available"


class VisionProviderResponseError(VisionDescriberError):
    """The provider answered with an error status or an unusable body."""

    kind = "provider_response_error"


# --------------------------------------------------------------------------- #
# Description record
# --------------------------------------------------------------------------- #


@dataclass
class VisionCompletion:
    """What a provider call returns: the text plus optional provenance."""

    text: str | None
    model: str | None = None
    model_digest: str | None = None
    provider_version: str | None = None


@dataclass
class FigureDescription:
    """A model-produced description of a figure, provenance-stamped."""

    text: str
    model: str
    provider: str = "anthropic"
    prompt_version: str = PROMPT_VERSION
    approximate: bool = True
    model_digest: str | None = None
    provider_version: str | None = None
    latency_ms: int | None = None

    def to_metadata(self) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "approximate": self.approximate,
        }
        if self.model_digest:
            meta["model_digest"] = self.model_digest
        if self.provider_version:
            meta["provider_version"] = self.provider_version
        if self.latency_ms is not None:
            meta["latency_ms"] = self.latency_ms
        return meta


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def vision_enabled() -> bool:
    return os.getenv("NOESIS_VISION", "auto").strip().lower() not in (
        "off",
        "0",
        "false",
    )


def _timeout_s() -> float:
    raw = os.getenv("NOESIS_VISION_TIMEOUT_S", "").strip()
    if not raw:
        return _DEFAULT_LOCAL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        logger.warning(
            "NOESIS_VISION_TIMEOUT_S=%r is not a number; using %s",
            raw,
            _DEFAULT_LOCAL_TIMEOUT_S,
        )
        return _DEFAULT_LOCAL_TIMEOUT_S
    return value if value > 0 else _DEFAULT_LOCAL_TIMEOUT_S


def _is_loopback(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").strip("[]")
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _resolve_config() -> dict[str, str] | None:
    if not vision_enabled():
        return None
    provider = os.getenv("NOESIS_VISION_PROVIDER", "auto").strip().lower() or "auto"
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if provider == "auto":
        if not api_key:
            return None
        provider = "anthropic"
    if provider not in _PROVIDERS:
        logger.warning(
            "NOESIS_VISION_PROVIDER=%r is not supported (choose from %s); vision describing is off",
            provider,
            ", ".join(sorted(_PROVIDERS)),
        )
        return None
    model = os.getenv("NOESIS_VISION_MODEL", "").strip() or _DEFAULT_MODELS[provider]
    if provider == "anthropic":
        if not api_key:
            return None
        return {"provider": provider, "model": model, "api_key": api_key}
    base_url = (
        os.getenv("NOESIS_OLLAMA_BASE_URL", "").strip() or _DEFAULT_OLLAMA_BASE_URL
    ).rstrip("/")
    if urlparse(base_url).scheme not in ("http", "https"):
        logger.warning(
            "NOESIS_OLLAMA_BASE_URL=%r must be an http(s) URL; vision describing is off",
            base_url,
        )
        return None
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "timeout_s": str(_timeout_s()),
    }


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


def _describe_anthropic(
    config: dict[str, str], image_b64: str, mime: str, prompt: str
) -> str | None:
    import anthropic  # lazy: optional dependency

    client = anthropic.Anthropic(api_key=config["api_key"])
    response = client.messages.create(
        model=config["model"],
        max_tokens=_MAX_OUTPUT_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )
    parts = [b.text for b in response.content if getattr(b, "type", "") == "text"]
    return "".join(parts).strip() or None


def _model_names_match(configured: str, listed: str) -> bool:
    def norm(name: str) -> str:
        return name if ":" in name else f"{name}:latest"

    return norm(configured) == norm(listed)


def _ollama_provenance(client: Any, model: str) -> dict[str, str | None]:
    """Best-effort model digest (``/api/tags``) and server version (``/api/version``).

    Provenance lookups never fail a description; missing values stay ``None``.
    """
    import httpx

    out: dict[str, str | None] = {"model_digest": None, "provider_version": None}
    try:
        resp = client.get("/api/version")
        if resp.status_code == 200:
            out["provider_version"] = (resp.json() or {}).get("version") or None
    except (httpx.HTTPError, ValueError):
        logger.debug("Ollama version lookup failed", exc_info=True)
    try:
        resp = client.get("/api/tags")
        if resp.status_code == 200:
            for entry in (resp.json() or {}).get("models", []) or []:
                name = entry.get("model") or entry.get("name") or ""
                if name and _model_names_match(model, name):
                    out["model_digest"] = entry.get("digest") or None
                    break
    except (httpx.HTTPError, ValueError):
        logger.debug("Ollama model digest lookup failed", exc_info=True)
    return out


def _describe_ollama(
    config: dict[str, str],
    image_b64: str,
    mime: str,
    prompt: str,
    http_client: Any = None,
) -> VisionCompletion:
    """Describe one image with a local Ollama vision model via ``POST /api/chat``.

    Raises a typed :class:`VisionDescriberError` subclass on failure. ``mime``
    is not sent: Ollama sniffs the image format from the bytes.
    """
    import httpx  # lazy: keep module import cheap

    provider, model, base_url = "ollama", config["model"], config["base_url"]
    timeout = float(config.get("timeout_s") or _DEFAULT_LOCAL_TIMEOUT_S)
    owns_client = http_client is None
    client = http_client or httpx.Client(
        base_url=base_url, timeout=httpx.Timeout(timeout, connect=5.0)
    )
    payload = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
        "options": {"temperature": 0, "num_predict": _MAX_OUTPUT_TOKENS},
    }
    try:
        try:
            resp = client.post("/api/chat", json=payload)
        except httpx.TimeoutException as exc:
            raise VisionProviderUnavailable(
                f"Ollama at {base_url} timed out after {timeout:g}s describing with {model}",
                provider=provider,
                model=model,
            ) from exc
        except httpx.TransportError as exc:
            raise VisionProviderUnavailable(
                f"Ollama at {base_url} is unreachable ({type(exc).__name__}); "
                "start it with `ollama serve` or set NOESIS_OLLAMA_BASE_URL",
                provider=provider,
                model=model,
            ) from exc

        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        error_text = str(body.get("error") or "").strip()
        if resp.status_code == 404 or (
            "not found" in error_text.lower() and "model" in error_text.lower()
        ):
            raise VisionModelNotAvailable(
                f"Ollama model {model!r} is not installed at {base_url}; run `ollama pull {model}`"
                + (f" ({error_text})" if error_text else ""),
                provider=provider,
                model=model,
            )
        if resp.status_code != 200:
            raise VisionProviderResponseError(
                f"Ollama returned HTTP {resp.status_code}"
                + (f": {error_text}" if error_text else ""),
                provider=provider,
                model=model,
            )
        if error_text:
            raise VisionProviderResponseError(
                f"Ollama error: {error_text}", provider=provider, model=model
            )
        message = body.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise VisionProviderResponseError(
                "Ollama /api/chat response has no message content",
                provider=provider,
                model=model,
            )
        text = str(message.get("content") or "").strip() or None
        provenance = _ollama_provenance(client, model) if text else {}
        return VisionCompletion(
            text=text,
            model=str(body.get("model") or model),
            model_digest=provenance.get("model_digest"),
            provider_version=provenance.get("provider_version"),
        )
    finally:
        if owns_client:
            client.close()


CompleteFn = Callable[[dict[str, str], str, str, str], str | None | VisionCompletion]


class VisionDescriber:
    """VLM describer with a caption-only fallback.

    ``complete`` is injectable so the model-call path is exercised in tests
    without a key, SDK, or server: it receives ``(config, image_b64, mime,
    prompt)`` and returns the description text (or a :class:`VisionCompletion`)
    or None. ``http_client`` (an ``httpx.Client`` with ``base_url`` set) is
    passed to the Ollama provider, e.g. one backed by ``httpx.MockTransport``.
    """

    def __init__(self, complete: CompleteFn | None = None, http_client: Any = None):
        self._complete = complete
        self._http_client = http_client
        self.last_error: VisionDescriberError | None = None

    @property
    def configured(self) -> bool:
        return _resolve_config() is not None

    @property
    def provider(self) -> str | None:
        config = _resolve_config()
        return config["provider"] if config else None

    def _call(self, config: dict[str, str], image_b64: str, mime: str, prompt: str):
        if self._complete is not None:
            return self._complete(config, image_b64, mime, prompt)
        if config["provider"] == "ollama":
            return _describe_ollama(
                config, image_b64, mime, prompt, http_client=self._http_client
            )
        return _describe_anthropic(config, image_b64, mime, prompt)

    def describe(
        self,
        image_bytes: bytes | None,
        context: str = "",
        mime: str = "image/png",
        raise_on_error: bool = False,
    ) -> FigureDescription | None:
        """Describe an image, or return None to signal caption-only fallback.

        None whenever: describing is off / no provider configured, no image
        bytes, or the provider call fails or returns empty. A failure is kept on
        ``last_error`` as a typed :class:`VisionDescriberError` (and raised when
        ``raise_on_error`` is true) so the degradation stays visible.
        """
        self.last_error = None
        if not image_bytes:
            return None
        config = _resolve_config()
        if config is None:
            return None
        provider = config["provider"]
        if provider == "ollama" and not _is_loopback(config["base_url"]):
            logger.info(
                "VisionDescriber: sending image to non-loopback Ollama at %s",
                config["base_url"],
            )
        started = time.monotonic()
        try:
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
            prompt = _PROMPT.format(context=context or "(none)")
            result = self._call(config, image_b64, mime, prompt)
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, VisionDescriberError)
                else VisionDescriberError(
                    f"{type(exc).__name__}: {exc}",
                    provider=provider,
                    model=config["model"],
                )
            )
            self.last_error = error
            logger.warning(
                "VisionDescriber: %s describe failed (%s) — caption-only fallback",
                provider,
                error.kind,
                exc_info=True,
            )
            if raise_on_error:
                if error is exc:
                    raise
                raise error from exc
            return None
        latency_ms = round((time.monotonic() - started) * 1000)
        completion = (
            result
            if isinstance(result, VisionCompletion)
            else VisionCompletion(text=result)
        )
        text = (completion.text or "").strip()
        if not text:
            return None
        return FigureDescription(
            text=text,
            model=completion.model or config["model"],
            provider=provider,
            model_digest=completion.model_digest,
            provider_version=completion.provider_version,
            latency_ms=latency_ms,
        )
