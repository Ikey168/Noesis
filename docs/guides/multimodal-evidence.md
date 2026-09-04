# Multimodal evidence

Noesis gives images, charts, maps, audio, video, pages, regions, frames, and
time segments stable, source-native identities. Asset revisions retain byte and
perceptual hashes, media metadata, acquisition provenance, generation,
valid/observed time, producer, and policy context. Binary requests are capped at
5 MB, while missing bytes remain an explicit state. Region and time locators are
validated against known dimensions and duration.

Local-first OCR, speech, frame, caption, and chart adapters persist only bounded
observations and deterministic receipts. Extraction is capped at 500
observations and one hour of media, supports cancellation before commit, and
rejects unsupported codecs. Poor scans and uncertain speakers keep their
confidence and unknown-speaker state.

Every extracted observation is labelled `unverified-extraction`. Links to
claims, entities, events, and sources preserve that status; they do not convert
OCR, a caption, or a plotted-value estimate into verified fact. Supporting and
contradicting modalities remain visible as a conflict group, including reused
media and montages.

Transformation edges record crops, edits, mirrors, and recompression. Provenance
inspection returns exact-byte and perceptual matches, acquisition data, and the
transformation chain. Reviewed authenticity revisions can preserve C2PA data,
metadata stripping, synthetic indicators, evidence, calibrated confidence, and
an `inconclusive` finding where recompression or missing metadata prevents a
strong conclusion.

The MCP scopes are `knowledge:multimodal:read`,
`knowledge:multimodal:write`, `knowledge:multimodal:extract`, and
`knowledge:multimodal:review`. Search and provenance output is bounded and
namespace-isolated; no MCP read returns stored bytes. Offline fixtures cover
scientific charts and OSINT media without network dependencies.
