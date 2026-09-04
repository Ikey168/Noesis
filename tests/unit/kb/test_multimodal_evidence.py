from __future__ import annotations

import base64
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.multimodal_evidence import (
    EXTRACT_SCOPE,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    MultimodalError,
    MultimodalStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _asset(store, native="figure-1", version="1", data=b"figure", **changes):
    values = {
        "namespace": "scientific",
        "source_id": "paper:1",
        "native_id": native,
        "version": version,
        "asset_type": "chart",
        "media_type": "image/png",
        "bytes_base64": base64.b64encode(data).decode() if data is not None else None,
        "perceptual_hash": "phash:one",
        "metadata": {"width": 100, "height": 80},
        "segments": [
            {
                "kind": "plot",
                "locator": {"region": {"x": 0, "y": 0, "width": 100, "height": 80}},
            }
        ],
        "generation": 1,
        "observed_at_ms": 10,
        "producer": {"id": "ingest"},
        "policy": {"id": "media-v1"},
        "provenance": {"url": "https://example.test/paper"},
        "principal_id": "curator",
        "scopes": {WRITE_SCOPE},
    }
    values.update(changes)
    return store.register_asset(**values)


def test_assets_duplicates_clipping_missing_bytes_transformations_and_identity():
    conn = duckdb.connect(":memory:")
    store = MultimodalStore(conn, now=lambda: 100)
    first = _asset(store)
    assert _asset(store)["idempotent"]
    duplicate = _asset(store, native="copy", data=b"figure")
    assert first["asset_id"] in duplicate["duplicate_asset_ids"]
    missing = _asset(store, native="missing", data=None, perceptual_hash=None)
    assert not missing["bytes_available"]
    changed = _asset(
        store,
        version="2",
        data=b"edited",
        predecessor_revision_id=first["asset_revision_id"],
    )
    assert changed["asset_id"] == first["asset_id"]
    transform = store.transform(
        "scientific",
        first["asset_id"],
        duplicate["asset_id"],
        "crop",
        {"region": [0, 0, 50, 50]},
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    assert transform["operation"] == "crop"
    with pytest.raises(MultimodalError, match="outside asset bounds"):
        _asset(
            store,
            native="bad-clip",
            segments=[
                {"locator": {"region": {"x": 90, "y": 0, "width": 20, "height": 10}}}
            ],
        )
    _validate("noesis-multimodal-asset-v1.json", changed)
    conn.close()


def test_bounded_ocr_speech_frames_chart_scan_speakers_codec_cancel_and_replay():
    conn = duckdb.connect(":memory:")
    store = MultimodalStore(conn, now=lambda: 100)
    asset = _asset(store)
    observations = [
        {
            "kind": "axis",
            "value": "GDP",
            "locator": {"region": {"x": 1, "y": 1, "width": 5, "height": 5}},
            "confidence": 0.35,
        },
        {
            "kind": "speech",
            "value": "unclear audio",
            "locator": {"time": {"start_ms": 0, "end_ms": 1000}},
            "confidence": 0.4,
            "speaker": None,
        },
    ]
    receipt = store.extract(
        "scientific",
        asset["asset_id"],
        "chart",
        observations,
        principal_id="worker",
        scopes={EXTRACT_SCOPE},
        limit=1,
    )
    assert receipt["truncated"] and receipt["items"][0]["confidence"] == 0.35
    assert store.replay("scientific", receipt["extraction_id"], scopes={READ_SCOPE})[
        "deterministic"
    ]
    cancelled = store.extract(
        "scientific",
        asset["asset_id"],
        "ocr",
        observations,
        principal_id="worker",
        scopes={EXTRACT_SCOPE},
        cancel_requested=True,
    )
    assert cancelled["status"] == "cancelled"
    with pytest.raises(MultimodalError, match="unsupported codec"):
        store.extract(
            "scientific",
            asset["asset_id"],
            "frames",
            observations,
            codec="avi",
            principal_id="worker",
            scopes={EXTRACT_SCOPE},
        )
    long = _asset(
        store,
        native="video",
        asset_type="video",
        media_type="video/mp4",
        metadata={"duration_ms": 4_000_000},
    )
    with pytest.raises(MultimodalError, match="exceeds"):
        store.extract(
            "scientific",
            long["asset_id"],
            "speech",
            observations,
            principal_id="worker",
            scopes={EXTRACT_SCOPE},
        )
    _validate("noesis-multimodal-extraction-v1.json", receipt)
    conn.close()


def test_cross_modal_caption_reuse_montage_uncertain_speakers_and_conflicts():
    conn = duckdb.connect(":memory:")
    store = MultimodalStore(conn, now=lambda: 100)
    asset = _asset(store)
    extracted = store.extract(
        "scientific",
        asset["asset_id"],
        "captions",
        [
            {
                "kind": "caption",
                "value": "increase",
                "locator": {"page": 3},
                "confidence": 0.6,
            }
        ],
        principal_id="worker",
        scopes={EXTRACT_SCOPE},
    )
    observation = extracted["items"][0]
    supporting = store.link_observation(
        "scientific",
        observation["observation_id"],
        "claim",
        "claim:gdp",
        "caption-describes",
        "supports",
        0.6,
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    other = _asset(store, native="montage")
    contradictory = store.extract(
        "scientific",
        other["asset_id"],
        "ocr",
        [
            {
                "kind": "legend",
                "value": "decrease",
                "locator": {"region": {"x": 1}},
                "confidence": 0.5,
                "speaker": None,
            }
        ],
        principal_id="worker",
        scopes={EXTRACT_SCOPE},
    )
    conflict = store.link_observation(
        "scientific",
        contradictory["items"][0]["observation_id"],
        "claim",
        "claim:gdp",
        "legend-describes",
        "contradicts",
        0.5,
        principal_id="curator",
        scopes={WRITE_SCOPE},
    )
    assert supporting["verification_status"] == "unverified-extraction"
    assert conflict["conflict_group"] and not contradictory["items"][0]["speaker_known"]
    _validate("noesis-cross-modal-evidence-v1.json", conflict)
    conn.close()


def test_provenance_metadata_recompression_mirror_synthetic_uncertainty_and_c2pa():
    conn = duckdb.connect(":memory:")
    store = MultimodalStore(conn, now=lambda: 100)
    original = _asset(store)
    mirrored = _asset(
        store,
        native="mirror",
        data=b"recompressed",
        perceptual_hash="phash:one",
        metadata={},
    )
    store.transform(
        "scientific",
        original["asset_id"],
        mirrored["asset_id"],
        "mirror-and-recompress",
        {"metadata_stripped": True},
        principal_id="analyst",
        scopes={WRITE_SCOPE},
    )
    finding = store.assess_authenticity(
        "scientific",
        mirrored["asset_id"],
        "inconclusive",
        0.55,
        c2pa=None,
        metadata_findings=["metadata-stripped"],
        synthetic_indicators=["generator-pattern"],
        uncertainty="recompression weakens signal",
        evidence=["detector:local"],
        principal_id="reviewer",
        scopes={REVIEW_SCOPE},
    )
    assert store.assess_authenticity(
        "scientific",
        mirrored["asset_id"],
        "inconclusive",
        0.55,
        c2pa=None,
        metadata_findings=["metadata-stripped"],
        synthetic_indicators=["generator-pattern"],
        uncertainty="recompression weakens signal",
        evidence=["detector:local"],
        principal_id="reviewer",
        scopes={REVIEW_SCOPE},
    )["idempotent"]
    provenance = store.provenance(
        "scientific", mirrored["asset_id"], scopes={READ_SCOPE}
    )
    assert (
        original["asset_id"] in provenance["matches"] and provenance["transformations"]
    )
    _validate("noesis-media-authenticity-v1.json", finding)
    _validate("noesis-media-authenticity-v1.json", provenance)
    conn.close()


def test_search_segments_namespace_authorization_osint_and_scientific():
    conn = duckdb.connect(":memory:")
    store = MultimodalStore(conn, now=lambda: 100)
    scientific = _asset(store)
    osint = _asset(
        store,
        native="map",
        namespace="osint",
        source_id="source:osint",
        asset_type="map",
    )
    found = store.search("scientific", "figure", scopes={READ_SCOPE}, limit=1)
    assert found["items"][0]["asset_id"] == scientific["asset_id"]
    segment = store.segment(
        "scientific",
        scientific["asset_id"],
        scientific["segments"][0]["segment_id"],
        scopes={READ_SCOPE},
    )
    assert segment["evidence_locator"]["asset_id"] == scientific["asset_id"]
    with pytest.raises(MultimodalError, match="not found"):
        store.asset("scientific", osint["asset_id"], scopes={READ_SCOPE})
    with pytest.raises(MultimodalError, match="missing required scope"):
        store.search("scientific", "x", scopes={"knowledge:read"})
    _validate("noesis-multimodal-search-v1.json", found)
    _validate("noesis-multimodal-search-v1.json", segment)
    conn.close()
