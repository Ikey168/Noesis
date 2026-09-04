"""Versioned multimodal assets, bounded observations, links, and provenance."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

ASSET_CONTRACT = "noesis-multimodal-asset-v1"
EXTRACTION_CONTRACT = "noesis-multimodal-extraction-v1"
LINK_CONTRACT = "noesis-cross-modal-evidence-v1"
AUTHENTICITY_CONTRACT = "noesis-media-authenticity-v1"
SEARCH_CONTRACT = "noesis-multimodal-search-v1"
READ_SCOPE = "knowledge:multimodal:read"
WRITE_SCOPE = "knowledge:multimodal:write"
EXTRACT_SCOPE = "knowledge:multimodal:extract"
REVIEW_SCOPE = "knowledge:multimodal:review"
ASSET_TYPES = {"image", "chart", "map", "audio", "video", "page"}
EXTRACTORS = {"ocr", "speech", "frames", "captions", "chart"}

_DDL = """
CREATE TABLE IF NOT EXISTS multimodal_asset_revisions (
  asset_revision_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL,
  version TEXT NOT NULL, asset_type TEXT NOT NULL, media_type TEXT NOT NULL,
  byte_hash TEXT, perceptual_hash TEXT, byte_size BIGINT, bytes BLOB,
  payload_json TEXT NOT NULL, content_hash TEXT NOT NULL, predecessor_revision_id TEXT,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(asset_id,version)
);
CREATE TABLE IF NOT EXISTS multimodal_asset_current (
  asset_id TEXT PRIMARY KEY, asset_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS multimodal_extractions (
  extraction_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, asset_id TEXT NOT NULL,
  extractor TEXT NOT NULL, input_hash TEXT NOT NULL, output_hash TEXT NOT NULL,
  receipt_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,asset_id,extractor,input_hash)
);
CREATE TABLE IF NOT EXISTS multimodal_observations (
  observation_id TEXT PRIMARY KEY, extraction_id TEXT NOT NULL, namespace TEXT NOT NULL,
  asset_id TEXT NOT NULL, kind TEXT NOT NULL, value_json TEXT NOT NULL,
  locator_json TEXT NOT NULL, confidence DOUBLE NOT NULL, speaker TEXT,
  verification_status TEXT NOT NULL, provenance_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS multimodal_evidence_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, observation_id TEXT NOT NULL,
  target_type TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
  stance TEXT NOT NULL, confidence DOUBLE NOT NULL, conflict_group TEXT,
  payload_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,observation_id,target_type,target_id,relation)
);
CREATE TABLE IF NOT EXISTS multimodal_transformations (
  transformation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, parent_asset_id TEXT NOT NULL,
  child_asset_id TEXT NOT NULL, operation TEXT NOT NULL, parameters_json TEXT NOT NULL,
  payload_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,parent_asset_id,child_asset_id,operation)
);
CREATE TABLE IF NOT EXISTS multimodal_authenticity_revisions (
  authenticity_revision_id TEXT PRIMARY KEY, authenticity_id TEXT NOT NULL,
  namespace TEXT NOT NULL, asset_id TEXT NOT NULL, version BIGINT NOT NULL,
  finding TEXT NOT NULL, confidence DOUBLE NOT NULL, payload_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, predecessor_revision_id TEXT,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(authenticity_id,version)
);
CREATE TABLE IF NOT EXISTS multimodal_authenticity_current (
  authenticity_id TEXT PRIMARY KEY, authenticity_revision_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS multimodal_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class MultimodalError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, bytes) else _canonical(value).encode()
    return hashlib.sha256(raw).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise MultimodalError("unauthorized", f"missing required scope {required}")


def _bounded(value: int, maximum: int = 500) -> int:
    return min(max(int(value), 1), maximum)


class MultimodalStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        audit_id = (
            "media-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO multimodal_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def register_asset(
        self,
        namespace: str,
        source_id: str,
        native_id: str,
        version: str,
        asset_type: str,
        media_type: str,
        *,
        principal_id: str,
        scopes: set[str],
        bytes_base64: str | None = None,
        perceptual_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        segments: Sequence[Mapping[str, Any]] = (),
        predecessor_revision_id: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if asset_type not in ASSET_TYPES or not all(
            str(v).strip()
            for v in (namespace, source_id, native_id, version, media_type)
        ):
            raise MultimodalError(
                "invalid_asset",
                "stable identity, version, supported type, and media type are required",
            )
        raw = None
        if bytes_base64 is not None:
            try:
                raw = base64.b64decode(bytes_base64, validate=True)
            except Exception as exc:
                raise MultimodalError(
                    "invalid_binary", "asset bytes are not valid base64"
                ) from exc
            if len(raw) > 5_000_000:
                raise MultimodalError(
                    "asset_too_large", "asset bytes exceed the 5 MB MCP boundary"
                )
        asset_id = "media-asset:" + _digest([namespace, source_id, native_id])[:24]
        current = self.conn.execute(
            "SELECT r.asset_revision_id FROM multimodal_asset_current c JOIN multimodal_asset_revisions r ON r.asset_revision_id=c.asset_revision_id WHERE r.namespace=? AND r.asset_id=?",
            [namespace, asset_id],
        ).fetchone()
        if predecessor_revision_id and (
            not current or current[0] != predecessor_revision_id
        ):
            raise MultimodalError(
                "revision_conflict", "predecessor is not the current asset revision"
            )
        meta = dict(metadata or {})
        width, height, duration = (
            meta.get("width"),
            meta.get("height"),
            meta.get("duration_ms"),
        )
        normalized_segments = []
        for index, item in enumerate(segments):
            value = dict(item)
            locator = dict(value.get("locator") or {})
            if "time" in locator:
                start, end = (
                    locator["time"].get("start_ms", 0),
                    locator["time"].get("end_ms", 0),
                )
                if (
                    start < 0
                    or end <= start
                    or (duration is not None and end > duration)
                ):
                    raise MultimodalError(
                        "invalid_clip", f"segment {index} is outside media duration"
                    )
            if "region" in locator:
                region = locator["region"]
                if (
                    min(
                        region.get("x", -1),
                        region.get("y", -1),
                        region.get("width", 0),
                        region.get("height", 0),
                    )
                    < 0
                    or region.get("width", 0) <= 0
                    or region.get("height", 0) <= 0
                    or (width and region["x"] + region["width"] > width)
                    or (height and region["y"] + region["height"] > height)
                ):
                    raise MultimodalError(
                        "invalid_clip", f"segment {index} is outside asset bounds"
                    )
            segment_id = (
                "media-segment:" + _digest([asset_id, value.get("kind"), locator])[:24]
            )
            normalized_segments.append(
                {
                    "segment_id": segment_id,
                    "kind": value.get("kind", "region"),
                    "locator": locator,
                    "label": value.get("label"),
                }
            )
        now = self.now()
        payload = {
            "contract": ASSET_CONTRACT,
            "namespace": namespace,
            "asset_id": asset_id,
            "source_id": source_id,
            "native_id": native_id,
            "version": version,
            "asset_type": asset_type,
            "media_type": media_type,
            "byte_hash": _digest(raw) if raw is not None else None,
            "perceptual_hash": perceptual_hash,
            "byte_size": len(raw) if raw is not None else None,
            "bytes_available": raw is not None,
            "metadata": meta,
            "segments": normalized_segments,
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed_at_ms if observed_at_ms is not None else now,
            "producer": dict(producer or {}),
            "policy": dict(policy or {}),
            "provenance": dict(provenance or {}),
            "predecessor_revision_id": predecessor_revision_id,
        }
        content_hash = _digest(payload)
        revision_id = "media-revision:" + content_hash[:24]
        existing = self.conn.execute(
            "SELECT content_hash,payload_json FROM multimodal_asset_revisions WHERE asset_id=? AND version=?",
            [asset_id, version],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise MultimodalError(
                    "version_conflict", "asset version has different content"
                )
            return {**_load(existing[1], {}), "idempotent": True}
        payload["asset_revision_id"] = revision_id
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO multimodal_asset_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    asset_id,
                    namespace,
                    version,
                    asset_type,
                    media_type,
                    payload["byte_hash"],
                    perceptual_hash,
                    payload["byte_size"],
                    raw,
                    _canonical(payload),
                    content_hash,
                    predecessor_revision_id,
                    generation,
                    valid_from_ms,
                    valid_to_ms,
                    payload["observed_at_ms"],
                    _canonical(producer or {}),
                    _canonical(policy or {}),
                    _canonical(provenance or {}),
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO multimodal_asset_current VALUES (?,?) ON CONFLICT(asset_id) DO UPDATE SET asset_revision_id=excluded.asset_revision_id",
                [asset_id, revision_id],
            )
            self._audit(
                namespace,
                "register-asset",
                revision_id,
                principal_id,
                {"byte_hash": payload["byte_hash"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        duplicates = []
        if payload["byte_hash"] or perceptual_hash:
            rows = self.conn.execute(
                "SELECT DISTINCT asset_id FROM multimodal_asset_revisions WHERE namespace=? AND asset_id<>? AND (byte_hash=? OR (? IS NOT NULL AND perceptual_hash=?)) ORDER BY asset_id LIMIT 50",
                [
                    namespace,
                    asset_id,
                    payload["byte_hash"],
                    perceptual_hash,
                    perceptual_hash,
                ],
            ).fetchall()
            duplicates = [row[0] for row in rows]
        return {**payload, "duplicate_asset_ids": duplicates, "idempotent": False}

    def asset(
        self, namespace: str, asset_id: str, *, scopes: set[str], revision_id=None
    ):
        _require(scopes, READ_SCOPE)
        if revision_id:
            row = self.conn.execute(
                "SELECT payload_json FROM multimodal_asset_revisions WHERE namespace=? AND asset_id=? AND asset_revision_id=?",
                [namespace, asset_id, revision_id],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT r.payload_json FROM multimodal_asset_current c JOIN multimodal_asset_revisions r ON r.asset_revision_id=c.asset_revision_id WHERE r.namespace=? AND r.asset_id=?",
                [namespace, asset_id],
            ).fetchone()
        if not row:
            raise MultimodalError("asset_not_found", "multimodal asset was not found")
        return _load(row[0], {})

    def search(
        self,
        namespace: str,
        query: str,
        *,
        scopes: set[str],
        asset_type=None,
        limit=50,
        offset=0,
    ):
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.payload_json FROM multimodal_asset_current c JOIN multimodal_asset_revisions r ON r.asset_revision_id=c.asset_revision_id WHERE r.namespace=? ORDER BY r.asset_id",
            [namespace],
        ).fetchall()
        needle = query.casefold().strip()
        items = [
            value
            for (raw,) in rows
            if needle in _canonical(value := _load(raw, {})).casefold()
            and (not asset_type or value["asset_type"] == asset_type)
        ]
        start, bounded = max(int(offset), 0), _bounded(limit)
        page = items[start : start + bounded]
        return {
            "contract": SEARCH_CONTRACT,
            "namespace": namespace,
            "items": page,
            "total": len(items),
            "next_offset": start + len(page)
            if start + len(page) < len(items)
            else None,
        }

    def segment(
        self, namespace: str, asset_id: str, segment_id: str, *, scopes: set[str]
    ):
        asset = self.asset(namespace, asset_id, scopes=scopes)
        for value in asset["segments"]:
            if value["segment_id"] == segment_id:
                return {
                    "contract": SEARCH_CONTRACT,
                    "namespace": namespace,
                    "asset_id": asset_id,
                    "segment": value,
                    "evidence_locator": {"asset_id": asset_id, **value["locator"]},
                }
        raise MultimodalError("segment_not_found", "asset segment was not found")

    def extract(
        self,
        namespace: str,
        asset_id: str,
        extractor: str,
        observations: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
        codec=None,
        limit=500,
        duration_limit_ms=3_600_000,
        cancel_requested=False,
        adapter="local-fixture-v1",
    ):
        _require(scopes, EXTRACT_SCOPE)
        asset = self.asset(namespace, asset_id, scopes={READ_SCOPE})
        if extractor not in EXTRACTORS:
            raise MultimodalError(
                "unsupported_extractor", "unsupported multimodal extractor"
            )
        if codec and codec not in {
            "png",
            "jpeg",
            "webp",
            "pdf",
            "wav",
            "mp3",
            "mp4",
            "webm",
        }:
            raise MultimodalError("unsupported_codec", f"unsupported codec {codec}")
        duration = int(asset["metadata"].get("duration_ms") or 0)
        if duration > min(max(int(duration_limit_ms), 1), 3_600_000):
            raise MultimodalError(
                "media_too_long", "media exceeds the bounded extraction duration"
            )
        input_hash = _digest(
            [asset["asset_revision_id"], extractor, observations, codec, adapter]
        )
        if cancel_requested:
            return {
                "contract": EXTRACTION_CONTRACT,
                "namespace": namespace,
                "asset_id": asset_id,
                "extractor": extractor,
                "status": "cancelled",
                "items": [],
                "input_hash": input_hash,
                "output_hash": _digest([]),
                "truncated": False,
                "adapter": adapter,
            }
        values = []
        for index, raw in enumerate(list(observations)[: _bounded(limit)]):
            item = dict(raw)
            locator = dict(item.get("locator") or {})
            if not locator:
                raise MultimodalError(
                    "invalid_locator",
                    f"observation {index} requires a region, time, page, or frame locator",
                )
            confidence = float(item.get("confidence", 0))
            if not 0 <= confidence <= 1:
                raise MultimodalError(
                    "invalid_observation", "confidence must be between zero and one"
                )
            value = {
                "kind": item.get("kind", extractor),
                "value": item.get("value"),
                "locator": locator,
                "confidence": confidence,
                "speaker": item.get("speaker"),
                "speaker_known": item.get("speaker") is not None,
                "verification_status": "unverified-extraction",
                "provenance": dict(item.get("provenance") or {}),
            }
            value["observation_id"] = (
                "media-observation:" + _digest([asset_id, extractor, value])[:24]
            )
            values.append(value)
        output_hash = _digest(values)
        extraction_id = (
            "media-extraction:"
            + _digest([namespace, asset_id, extractor, input_hash])[:24]
        )
        now = self.now()
        receipt = {
            "contract": EXTRACTION_CONTRACT,
            "namespace": namespace,
            "extraction_id": extraction_id,
            "asset_id": asset_id,
            "extractor": extractor,
            "status": "completed",
            "items": values,
            "input_hash": input_hash,
            "output_hash": output_hash,
            "truncated": len(observations) > len(values),
            "adapter": adapter,
            "created_at_ms": now,
        }
        existing = self.conn.execute(
            "SELECT receipt_json FROM multimodal_extractions WHERE extraction_id=?",
            [extraction_id],
        ).fetchone()
        if existing:
            return {**_load(existing[0], {}), "idempotent": True}
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO multimodal_extractions VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    extraction_id,
                    namespace,
                    asset_id,
                    extractor,
                    input_hash,
                    output_hash,
                    _canonical(receipt),
                    principal_id,
                    now,
                ],
            )
            for value in values:
                self.conn.execute(
                    "INSERT INTO multimodal_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        value["observation_id"],
                        extraction_id,
                        namespace,
                        asset_id,
                        value["kind"],
                        _canonical(value["value"]),
                        _canonical(value["locator"]),
                        value["confidence"],
                        value["speaker"],
                        value["verification_status"],
                        _canonical(value["provenance"]),
                        now,
                    ],
                )
            self._audit(
                namespace,
                "extract",
                extraction_id,
                principal_id,
                {"extractor": extractor, "count": len(values)},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**receipt, "idempotent": False}

    def replay(self, namespace: str, extraction_id: str, *, scopes: set[str]):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT receipt_json FROM multimodal_extractions WHERE namespace=? AND extraction_id=?",
            [namespace, extraction_id],
        ).fetchone()
        if not row:
            raise MultimodalError("extraction_not_found", "extraction was not found")
        value = _load(row[0], {})
        actual = _digest(value["items"])
        return {
            "extraction_id": extraction_id,
            "expected_hash": value["output_hash"],
            "actual_hash": actual,
            "deterministic": actual == value["output_hash"],
        }

    def link_observation(
        self,
        namespace: str,
        observation_id: str,
        target_type: str,
        target_id: str,
        relation: str,
        stance: str,
        confidence: float,
        *,
        principal_id: str,
        scopes: set[str],
    ):
        _require(scopes, WRITE_SCOPE)
        row = self.conn.execute(
            "SELECT value_json,asset_id FROM multimodal_observations WHERE namespace=? AND observation_id=?",
            [namespace, observation_id],
        ).fetchone()
        if (
            not row
            or target_type not in {"claim", "entity", "event", "source"}
            or stance not in {"supports", "contradicts", "mentions", "context"}
            or not 0 <= float(confidence) <= 1
        ):
            raise MultimodalError(
                "invalid_evidence_link",
                "valid observation, target, stance, and confidence are required",
            )
        conflicting = self.conn.execute(
            "SELECT stance FROM multimodal_evidence_links WHERE namespace=? AND target_type=? AND target_id=? AND stance<>? LIMIT 1",
            [namespace, target_type, target_id, stance],
        ).fetchone()
        conflict_group = (
            "cross-modal-conflict:" + _digest([namespace, target_type, target_id])[:20]
            if conflicting
            else None
        )
        link_id = (
            "cross-modal-link:"
            + _digest([namespace, observation_id, target_type, target_id, relation])[
                :24
            ]
        )
        payload = {
            "contract": LINK_CONTRACT,
            "namespace": namespace,
            "link_id": link_id,
            "observation_id": observation_id,
            "asset_id": row[1],
            "target_type": target_type,
            "target_id": target_id,
            "relation": relation,
            "stance": stance,
            "confidence": float(confidence),
            "verification_status": "unverified-extraction",
            "conflict_group": conflict_group,
        }
        now = self.now()
        existing = self.conn.execute(
            "SELECT payload_json FROM multimodal_evidence_links WHERE link_id=?",
            [link_id],
        ).fetchone()
        if existing:
            if _load(existing[0], {}) != payload:
                raise MultimodalError(
                    "link_conflict", "evidence link already has different content"
                )
            return {**payload, "idempotent": True}
        self.conn.execute(
            "INSERT INTO multimodal_evidence_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                link_id,
                namespace,
                observation_id,
                target_type,
                target_id,
                relation,
                stance,
                confidence,
                conflict_group,
                _canonical(payload),
                principal_id,
                now,
            ],
        )
        self._audit(
            namespace,
            "link-observation",
            link_id,
            principal_id,
            {"target": target_id},
            now,
        )
        return {**payload, "idempotent": False}

    def transform(
        self,
        namespace: str,
        parent_asset_id: str,
        child_asset_id: str,
        operation: str,
        parameters: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ):
        _require(scopes, WRITE_SCOPE)
        self.asset(namespace, parent_asset_id, scopes={READ_SCOPE})
        self.asset(namespace, child_asset_id, scopes={READ_SCOPE})
        transformation_id = (
            "media-transform:"
            + _digest(
                [namespace, parent_asset_id, child_asset_id, operation, parameters]
            )[:24]
        )
        payload = {
            "namespace": namespace,
            "transformation_id": transformation_id,
            "parent_asset_id": parent_asset_id,
            "child_asset_id": child_asset_id,
            "operation": operation,
            "parameters": dict(parameters),
        }
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO multimodal_transformations VALUES (?,?,?,?,?,?,?,?,?)",
            [
                transformation_id,
                namespace,
                parent_asset_id,
                child_asset_id,
                operation,
                _canonical(parameters),
                _canonical(payload),
                principal_id,
                now,
            ],
        )
        return payload

    def assess_authenticity(
        self,
        namespace: str,
        asset_id: str,
        finding: str,
        confidence: float,
        *,
        principal_id: str,
        scopes: set[str],
        c2pa=None,
        metadata_findings=(),
        synthetic_indicators=(),
        uncertainty=None,
        evidence=(),
    ):
        _require(scopes, REVIEW_SCOPE)
        self.asset(namespace, asset_id, scopes={READ_SCOPE})
        if (
            finding not in {"authentic", "altered", "synthetic", "inconclusive"}
            or not 0 <= float(confidence) <= 1
        ):
            raise MultimodalError(
                "invalid_authenticity",
                "supported finding and calibrated confidence are required",
            )
        authenticity_id = (
            "media-authenticity:" + _digest([namespace, asset_id, principal_id])[:24]
        )
        current = self.conn.execute(
            "SELECT r.authenticity_revision_id,r.version,r.content_hash,r.payload_json FROM multimodal_authenticity_current c JOIN multimodal_authenticity_revisions r ON r.authenticity_revision_id=c.authenticity_revision_id WHERE c.authenticity_id=?",
            [authenticity_id],
        ).fetchone()
        version = int(current[1]) + 1 if current else 1
        payload = {
            "contract": AUTHENTICITY_CONTRACT,
            "namespace": namespace,
            "authenticity_id": authenticity_id,
            "asset_id": asset_id,
            "version": version,
            "finding": finding,
            "confidence": float(confidence),
            "c2pa": c2pa,
            "metadata_findings": list(metadata_findings),
            "synthetic_indicators": list(synthetic_indicators),
            "uncertainty": uncertainty,
            "evidence": list(evidence),
        }
        content_hash = _digest({k: v for k, v in payload.items() if k != "version"})
        if current and current[2] == content_hash:
            return {**_load(current[3], {}), "idempotent": True}
        revision_id = (
            "media-authenticity-revision:"
            + _digest([authenticity_id, version, content_hash])[:24]
        )
        payload.update(
            {
                "authenticity_revision_id": revision_id,
                "predecessor_revision_id": current[0] if current else None,
            }
        )
        now = self.now()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO multimodal_authenticity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    authenticity_id,
                    namespace,
                    asset_id,
                    version,
                    finding,
                    confidence,
                    _canonical(payload),
                    content_hash,
                    current[0] if current else None,
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO multimodal_authenticity_current VALUES (?,?) ON CONFLICT(authenticity_id) DO UPDATE SET authenticity_revision_id=excluded.authenticity_revision_id",
                [authenticity_id, revision_id],
            )
            self._audit(
                namespace,
                "assess-authenticity",
                revision_id,
                principal_id,
                {"finding": finding},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**payload, "idempotent": False}

    def provenance(self, namespace: str, asset_id: str, *, scopes: set[str], limit=100):
        _require(scopes, READ_SCOPE)
        asset = self.asset(namespace, asset_id, scopes=scopes)
        transformations = [
            _load(row[0], {})
            for row in self.conn.execute(
                "SELECT payload_json FROM multimodal_transformations WHERE namespace=? AND (parent_asset_id=? OR child_asset_id=?) ORDER BY transformation_id LIMIT ?",
                [namespace, asset_id, asset_id, _bounded(limit)],
            ).fetchall()
        ]
        authenticities = [
            _load(row[0], {})
            for row in self.conn.execute(
                "SELECT r.payload_json FROM multimodal_authenticity_current c JOIN multimodal_authenticity_revisions r ON r.authenticity_revision_id=c.authenticity_revision_id WHERE r.namespace=? AND r.asset_id=? ORDER BY r.authenticity_id LIMIT ?",
                [namespace, asset_id, _bounded(limit)],
            ).fetchall()
        ]
        matches = [
            row[0]
            for row in self.conn.execute(
                "SELECT DISTINCT asset_id FROM multimodal_asset_revisions WHERE namespace=? AND asset_id<>? AND ((byte_hash IS NOT NULL AND byte_hash=?) OR (perceptual_hash IS NOT NULL AND perceptual_hash=?)) ORDER BY asset_id LIMIT ?",
                [
                    namespace,
                    asset_id,
                    asset.get("byte_hash"),
                    asset.get("perceptual_hash"),
                    _bounded(limit),
                ],
            ).fetchall()
        ]
        return {
            "contract": AUTHENTICITY_CONTRACT,
            "namespace": namespace,
            "asset_id": asset_id,
            "acquisition": asset["provenance"],
            "hashes": {
                "bytes": asset.get("byte_hash"),
                "perceptual": asset.get("perceptual_hash"),
            },
            "transformations": transformations,
            "matches": matches,
            "authenticity": authenticities,
        }
