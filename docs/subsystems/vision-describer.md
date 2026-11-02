# Vision describer

`src/ingestion/describers/vision.py` turns a figure or image into a short,
citable text description at ingest (chart type, axes and units, the headline
relationship, legible values marked approximate). The figure emitter in
`src/ingestion/describers/figures.py` stores that text as the content of a
figure `Document`; the caption is always kept in metadata.

Two providers sit behind the same `VisionDescriber` interface:

| Provider | Selected by | Needs | Cost |
|---|---|---|---|
| `anthropic` | `NOESIS_VISION_PROVIDER=anthropic`, or `auto` with `ANTHROPIC_API_KEY` set | `ANTHROPIC_API_KEY`, the `anthropic` SDK | Paid API |
| `ollama` | `NOESIS_VISION_PROVIDER=ollama` (explicit opt-in) | A running [Ollama](https://ollama.com) server with a vision model pulled | No API cost; local compute |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NOESIS_VISION` | `auto` | `off` disables describing; figures stay caption-only. |
| `NOESIS_VISION_PROVIDER` | `auto` | `auto` uses `anthropic` when `ANTHROPIC_API_KEY` is set and is otherwise off. `auto` never probes a local server. `anthropic` or `ollama` selects explicitly; any other value turns describing off with a warning. |
| `NOESIS_VISION_MODEL` | `claude-sonnet-5` (anthropic), `qwen2.5vl:7b` (ollama) | Model id for the selected provider. For Ollama, any vision-capable tag such as `qwen2.5vl:3b`, `llava:7b` or `gemma3:4b`. |
| `NOESIS_OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Ollama server. Must be `http` or `https`. The default is loopback; a non-loopback URL sends image bytes over the network to that host, which is logged. |
| `NOESIS_VISION_TIMEOUT_S` | `180` | Per-image request timeout for the Ollama provider (connect timeout is 5 s). |
| `ANTHROPIC_API_KEY` | — | Only for the `anthropic` provider. |

Local setup:

```bash
ollama serve                      # or the system service
ollama pull qwen2.5vl:7b          # or a smaller tag on CPU-only hosts
export NOESIS_VISION_PROVIDER=ollama
# optional: export NOESIS_VISION_MODEL=qwen2.5vl:3b
```

The Ollama provider calls `POST /api/chat` with the base64 image, `stream:
false`, `temperature: 0` and a 400-token output cap, using `httpx`. No model is
downloaded by Noesis; pulling a model is an operator action.

## Provenance on each description

Each successful description is stored on the figure document as
`metadata["describer"]`:

| Field | Always | Notes |
|---|---|---|
| `provider` | yes | `anthropic` or `ollama` |
| `model` | yes | Model id the provider reports (falls back to the configured id) |
| `prompt_version` | yes | Version of the describing prompt |
| `approximate` | yes | Values read off charts are approximate |
| `latency_ms` | yes | Wall-clock time of the provider call, measured in Noesis |
| `model_digest` | when available | Ollama: digest of the local model from `GET /api/tags` |
| `provider_version` | when available | Ollama: server version from `GET /api/version` |

Digest and version lookups are best effort: if they fail the description is
still kept and the fields are omitted.

## Failure and degradation

Ingestion never blocks on the describer. When describing is off, no provider
is configured, or a figure has no image bytes, the figure is caption-only and
`metadata["describer"]` is `None`.

When a configured provider fails, the failure is typed rather than silent:

| Error | Raised when |
|---|---|
| `VisionProviderUnavailable` | The Ollama server cannot be reached (connection refused, DNS, network) or the request times out. |
| `VisionModelNotAvailable` | Ollama is reachable but the model is not pulled (HTTP 404 / "model not found"); the message names the `ollama pull` command. |
| `VisionProviderResponseError` | Any other error status or a response without message content. |
| `VisionDescriberError` | Base class; also wraps any other provider exception (for example a missing `anthropic` SDK or an API error). |

`VisionDescriber.describe()` still returns `None` so the figure falls back to
its caption, but the error is kept on `VisionDescriber.last_error`, and the
figure document records it as `metadata["describer_degraded"]` (`status:
degraded`, `error`, `provider`, `model`, `detail`). Callers that must not fall
back pass `raise_on_error=True` to receive the typed exception.

## Hardware expectations

These are planning guidance from model sizes, not Noesis measurements.

- **Memory.** A vision model needs its weights plus the vision encoder in RAM
  (CPU) or VRAM (GPU). As a rough guide for the default 4-bit Ollama
  quantisations: 3B-class tags need about 4–6 GB of free memory, 7B-class tags
  (the default `qwen2.5vl:7b`) about 8–12 GB. Larger images and longer outputs
  add to that.
- **CPU-only and ARM hosts.** Ollama runs on ARM64 Linux without a GPU, but
  per-image latency is then dominated by image encoding and token generation on
  the CPU and can reach tens of seconds or more per figure. Prefer a 3B-class
  tag, keep `max_figures` / `max_images` caps in the figure emitters, and raise
  `NOESIS_VISION_TIMEOUT_S` if large figures time out.
- **First call.** The first request after start-up loads the model and is
  slower than later ones; Ollama unloads idle models after a few minutes by
  default.

### Not yet measured

**Latency on the Noesis ARM server has not been measured.** The provider was
built and tested against a mocked HTTP layer only; no real Ollama server or
model was run. To measure it, enable the provider on the ARM server, ingest a
fixed set of figures, and read the recorded `metadata["describer"]["latency_ms"]`
values (report median and p95 per model tag, separating the first, cold call).
Record the results here with the model tag, digest, Ollama version, and host
specification before relying on a latency figure.

## Tests

`tests/unit/ingestion/describers/test_vision.py` covers provider selection,
provenance fields, the Ollama request shape, digest and version lookups, and
each typed failure, using `httpx.MockTransport`; it needs no network, key or
server.
