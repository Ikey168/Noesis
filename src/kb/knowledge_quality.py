"""Auditable multidimensional knowledge quality features and policy simulations."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Sequence

POLICY_CONTRACT = "noesis-quality-policy-v1"
ASSESSMENT_CONTRACT = "noesis-quality-assessment-v1"
COLLECTION_CONTRACT = "noesis-quality-collection-v1"
RANKING_CONTRACT = "noesis-quality-ranking-v1"
HEALTH_CONTRACT = "noesis-quality-health-v1"
READ_SCOPE = "knowledge:quality:read"
WRITE_SCOPE = "knowledge:quality:write"
CALCULATE_SCOPE = "knowledge:quality:calculate"
REVIEW_SCOPE = "knowledge:quality:review"
DIMENSIONS = (
    "coverage",
    "provenance",
    "independence",
    "freshness",
    "contradiction",
    "methodology",
    "reproducibility",
    "uncertainty",
)
OBJECT_TYPES = {
    "source",
    "document",
    "claim",
    "entity",
    "event",
    "dataset",
    "answer",
    "bundle",
}
_DDL = """
CREATE TABLE IF NOT EXISTS quality_policy_revisions(policy_revision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,policy_id TEXT NOT NULL,version TEXT NOT NULL,content_hash TEXT NOT NULL,payload_json TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,policy_id,version));
CREATE TABLE IF NOT EXISTS quality_assessments(assessment_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,generation BIGINT NOT NULL,policy_revision_id TEXT NOT NULL,input_hash TEXT NOT NULL,payload_json TEXT NOT NULL,assessment_hash TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,object_type,object_id,generation,policy_revision_id,input_hash));
CREATE TABLE IF NOT EXISTS quality_overrides(override_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,object_id TEXT NOT NULL,dimension TEXT NOT NULL,value DOUBLE,reason TEXT NOT NULL,reviewer_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,object_id,dimension,reviewer_id));
CREATE TABLE IF NOT EXISTS quality_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class QualityError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(v):
    return hashlib.sha256(_canon(v).encode()).hexdigest()


def _load(v, d):
    return d if v is None else json.loads(v) if isinstance(v, str) else v


def _req(s, r):
    if r not in s and "operator" not in s:
        raise QualityError("unauthorized", f"missing required scope {r}")


def _bounded(v, m=500):
    return min(max(int(v), 1), m)


def _score(v):
    if v is None:
        return None
    x = float(v)
    if not 0 <= x <= 1:
        raise QualityError(
            "invalid_feature", "quality values must be between zero and one"
        )
    return x


class QualityStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        conn.execute(_DDL) if initialize else None

    def _audit(self, n, o, i, p, d, t):
        self.conn.execute(
            "INSERT OR IGNORE INTO quality_audit VALUES (?,?,?,?,?,?,?)",
            [
                "quality-audit:" + _hash([n, o, i, p, d, t])[:24],
                n,
                o,
                i,
                p,
                _canon(d),
                t,
            ],
        )

    def register_policy(
        self,
        namespace,
        policy_id,
        version,
        dimensions,
        *,
        principal_id,
        scopes,
        domain_overrides=None,
        calibration=None,
        threshold=0.5,
        generation=0,
        valid_from_ms=None,
        valid_to_ms=None,
        observed_at_ms=None,
        producer=None,
        policy_context=None,
        provenance=None,
    ):
        _req(scopes, WRITE_SCOPE)
        unknown = set(dimensions) - set(DIMENSIONS)
        if unknown:
            raise QualityError(
                "invalid_policy", f"unknown dimensions: {sorted(unknown)}"
            )
        normalized = {
            d: {
                "weight": float(dict(dimensions[d]).get("weight", 1)),
                "default": _score(dict(dimensions[d]).get("default"))
                if "default" in dict(dimensions[d])
                else None,
                "transparent_default": bool(
                    dict(dimensions[d]).get("transparent_default", True)
                ),
            }
            for d in dimensions
        }
        if any(v["weight"] < 0 for v in normalized.values()):
            raise QualityError("invalid_policy", "weights cannot be negative")
        now = self.now()
        p = {
            "contract": POLICY_CONTRACT,
            "namespace": namespace,
            "policy_id": policy_id,
            "version": version,
            "dimensions": normalized,
            "domain_overrides": dict(domain_overrides or {}),
            "calibration": dict(calibration or {}),
            "threshold": float(threshold),
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed_at_ms if observed_at_ms is not None else now,
            "producer": dict(producer or {}),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
        }
        h = _hash(p)
        rid = "quality-policy:" + h[:24]
        p["policy_revision_id"] = rid
        row = self.conn.execute(
            "SELECT content_hash,payload_json FROM quality_policy_revisions WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if row:
            if row[0] != h:
                raise QualityError(
                    "version_conflict", "policy version has different content"
                )
            return {**_load(row[1], {}), "idempotent": True}
        self.conn.execute(
            "INSERT INTO quality_policy_revisions VALUES (?,?,?,?,?,?,?,?)",
            [rid, namespace, policy_id, version, h, _canon(p), principal_id, now],
        )
        self._audit(namespace, "register-policy", rid, principal_id, {}, now)
        return {**p, "idempotent": False}

    def policy(self, namespace, rid, *, scopes):
        _req(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM quality_policy_revisions WHERE namespace=? AND policy_revision_id=?",
            [namespace, rid],
        ).fetchone()
        if not r:
            raise QualityError("policy_not_found", "quality policy not found")
        return _load(r[0], {})

    def assess(
        self,
        namespace,
        object_type,
        object_id,
        generation,
        policy_revision_id,
        features,
        *,
        input_lineage,
        principal_id,
        scopes,
        domain=None,
        valid_from_ms=None,
        valid_to_ms=None,
        observed_at_ms=None,
        producer=None,
        policy_context=None,
        provenance=None,
        cancel_requested=False,
    ):
        _req(scopes, CALCULATE_SCOPE)
        policy = self.policy(namespace, policy_revision_id, scopes={READ_SCOPE})
        if object_type not in OBJECT_TYPES:
            raise QualityError("invalid_object_type", "unsupported quality object")
        if cancel_requested:
            return {
                "contract": ASSESSMENT_CONTRACT,
                "namespace": namespace,
                "status": "cancelled",
                "object_type": object_type,
                "object_id": object_id,
                "dimensions": {},
                "input_hash": _hash(input_lineage),
            }
        overrides = dict(policy["domain_overrides"].get(domain, {}) if domain else {})
        dims = {}
        defaults = []
        for name, config in policy["dimensions"].items():
            raw = features.get(name, overrides.get(name, config["default"]))
            value = _score(raw)
            if name not in features and raw is not None:
                defaults.append(
                    {
                        "dimension": name,
                        "value": value,
                        "source": "domain-override"
                        if name in overrides
                        else "policy-default",
                    }
                )
            dims[name] = {
                "value": value,
                "known": value is not None,
                "lineage": [
                    dict(v) for v in input_lineage if v.get("dimension") in {None, name}
                ],
            }
        flags = []
        if features.get("inaccessible_sources"):
            flags.append("inaccessible-sources")
        if features.get("retracted"):
            flags.append("retracted-input")
        input_hash = _hash([features, input_lineage])
        known = [
            (n, v["value"], policy["dimensions"][n]["weight"])
            for n, v in dims.items()
            if v["known"] and policy["dimensions"][n]["weight"] > 0
        ]
        den = sum(v[2] for v in known)
        composite = None if not den else sum(v * w for _, v, w in known) / den
        missing = [n for n, v in dims.items() if not v["known"]]
        width = min(0.5, 0.05 + 0.05 * len(missing) + (0.1 if flags else 0))
        interval = (
            None
            if composite is None
            else [max(0, composite - width), min(1, composite + width)]
        )
        now = self.now()
        p = {
            "contract": ASSESSMENT_CONTRACT,
            "namespace": namespace,
            "status": "completed",
            "object_type": object_type,
            "object_id": object_id,
            "generation": int(generation),
            "policy_revision_id": policy_revision_id,
            "dimensions": dims,
            "composite": composite,
            "uncertainty_interval": interval,
            "missing_dimensions": missing,
            "transparent_defaults": defaults,
            "flags": flags,
            "input_lineage": [dict(v) for v in input_lineage],
            "input_hash": input_hash,
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed_at_ms if observed_at_ms is not None else now,
            "producer": dict(producer or {}),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
        }
        ah = _hash(p)
        aid = (
            "quality-assessment:"
            + _hash(
                [
                    namespace,
                    object_type,
                    object_id,
                    generation,
                    policy_revision_id,
                    input_hash,
                ]
            )[:24]
        )
        p.update({"assessment_id": aid, "assessment_hash": ah})
        row = self.conn.execute(
            "SELECT payload_json FROM quality_assessments WHERE assessment_id=?", [aid]
        ).fetchone()
        if row:
            return {**_load(row[0], {}), "idempotent": True}
        self.conn.execute(
            "INSERT INTO quality_assessments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                aid,
                namespace,
                object_type,
                object_id,
                generation,
                policy_revision_id,
                input_hash,
                _canon(p),
                ah,
                principal_id,
                now,
            ],
        )
        self._audit(namespace, "assess", aid, principal_id, {"flags": flags}, now)
        return {**p, "idempotent": False}

    def get(self, namespace, assessment_id, *, scopes):
        _req(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM quality_assessments WHERE namespace=? AND assessment_id=?",
            [namespace, assessment_id],
        ).fetchone()
        if not r:
            raise QualityError("assessment_not_found", "quality assessment not found")
        return _load(r[0], {})

    def replay(self, namespace, assessment_id, *, scopes):
        p = self.get(namespace, assessment_id, scopes=scopes)
        expected = p["assessment_hash"]
        actual = _hash(
            {
                k: v
                for k, v in p.items()
                if k not in {"assessment_id", "assessment_hash"}
            }
        )
        return {
            "assessment_id": assessment_id,
            "expected_hash": expected,
            "actual_hash": actual,
            "deterministic": expected == actual,
        }

    def collection(
        self,
        namespace,
        assessment_ids: Sequence[str],
        *,
        scopes,
        limit=500,
        calibration_samples=None,
        reference_distribution=None,
    ):
        _req(scopes, CALCULATE_SCOPE)
        ids = list(assessment_ids)[: _bounded(limit)]
        items = [self.get(namespace, v, scopes={READ_SCOPE}) for v in ids]
        warnings = []
        samples = list(calibration_samples or [])
        if samples and len(samples) < 30:
            warnings.append("small-calibration-sample")
        if (
            samples
            and reference_distribution
            and abs(
                sum(samples) / len(samples)
                - float(reference_distribution.get("mean", 0))
            )
            > 0.2
        ):
            warnings.append("calibration-distribution-drift")
        groups = {}
        for item in items:
            groups.setdefault(
                next(
                    (
                        str(v.get("independence_group"))
                        for v in item["input_lineage"]
                        if v.get("independence_group")
                    ),
                    item["assessment_id"],
                ),
                [],
            ).append(item)
        group_values = [
            sum(v["composite"] for v in group if v["composite"] is not None)
            / len([v for v in group if v["composite"] is not None])
            for group in groups.values()
            if any(v["composite"] is not None for v in group)
        ]
        mean = None if not group_values else sum(group_values) / len(group_values)
        interval = (
            None
            if mean is None
            else [
                max(0, mean - 0.1 / math.sqrt(max(len(group_values), 1))),
                min(1, mean + 0.1 / math.sqrt(max(len(group_values), 1))),
            ]
        )
        p = {
            "contract": COLLECTION_CONTRACT,
            "namespace": namespace,
            "assessment_ids": ids,
            "dimensions": {
                d: [v["dimensions"].get(d, {"value": None})["value"] for v in items]
                for d in DIMENSIONS
            },
            "composite": mean,
            "uncertainty_interval": interval,
            "independent_groups": len(groups),
            "warnings": warnings,
            "bounded": len(assessment_ids) > len(ids),
        }
        p["collection_hash"] = _hash(p)
        return p

    def rank(
        self,
        namespace,
        assessment_ids,
        *,
        scopes,
        threshold=None,
        descending=True,
        user_overrides=None,
    ):
        _req(scopes, READ_SCOPE)
        items = [
            self.get(namespace, v, scopes=scopes) for v in list(assessment_ids)[:500]
        ]
        overrides = dict(user_overrides or {})
        ranked = []
        for item in items:
            score = overrides.get(item["object_id"], item["composite"])
            cut = float(
                threshold
                if threshold is not None
                else self.policy(namespace, item["policy_revision_id"], scopes=scopes)[
                    "threshold"
                ]
            )
            ranked.append(
                {
                    "assessment_id": item["assessment_id"],
                    "object_id": item["object_id"],
                    "score": score,
                    "meets_threshold": score is not None and score >= cut,
                    "retained": True,
                    "override": item["object_id"] in overrides,
                    "dimensions": item["dimensions"],
                }
            )
        ranked.sort(
            key=lambda v: (
                (-(v["score"] if v["score"] is not None else -1))
                if descending
                else (v["score"] if v["score"] is not None else 2),
                v["assessment_id"],
            )
        )
        return {
            "contract": RANKING_CONTRACT,
            "namespace": namespace,
            "items": ranked,
            "low_scores_retained": True,
            "ranking_hash": _hash(ranked),
        }

    def simulate(self, namespace, assessment_ids, policy_revision_id, *, scopes):
        _req(scopes, READ_SCOPE)
        policy = self.policy(namespace, policy_revision_id, scopes=scopes)
        items = []
        for aid in list(assessment_ids)[:500]:
            old = self.get(namespace, aid, scopes=scopes)
            known = [
                (d, v["value"], policy["dimensions"].get(d, {"weight": 0})["weight"])
                for d, v in old["dimensions"].items()
                if v["known"] and d in policy["dimensions"]
            ]
            den = sum(v[2] for v in known)
            score = None if not den else sum(v * w for _, v, w in known) / den
            items.append(
                {
                    "assessment_id": aid,
                    "before": old["composite"],
                    "after": score,
                    "delta": None
                    if score is None or old["composite"] is None
                    else score - old["composite"],
                }
            )
        return {
            "namespace": namespace,
            "policy_revision_id": policy_revision_id,
            "items": items,
            "side_effect_free": True,
            "simulation_hash": _hash(items),
        }

    def compare_policies(self, namespace, left_policy_id, right_policy_id, *, scopes):
        _req(scopes, READ_SCOPE)
        left = self.policy(namespace, left_policy_id, scopes=scopes)
        right = self.policy(namespace, right_policy_id, scopes=scopes)
        differences = {
            name: {
                "left": left["dimensions"].get(name),
                "right": right["dimensions"].get(name),
            }
            for name in DIMENSIONS
            if left["dimensions"].get(name) != right["dimensions"].get(name)
        }
        value = {
            "namespace": namespace,
            "left_policy_revision_id": left_policy_id,
            "right_policy_revision_id": right_policy_id,
            "dimension_differences": differences,
            "thresholds": {"left": left["threshold"], "right": right["threshold"]},
        }
        value["comparison_hash"] = _hash(value)
        return value

    def override(
        self,
        namespace,
        object_id,
        dimension,
        value,
        reason,
        *,
        reviewer_id,
        principal_id,
        scopes,
    ):
        _req(scopes, REVIEW_SCOPE)
        value = _score(value)
        oid = (
            "quality-override:"
            + _hash([namespace, object_id, dimension, reviewer_id])[:24]
        )
        now = self.now()
        self.conn.execute(
            "INSERT INTO quality_overrides VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(namespace,object_id,dimension,reviewer_id) DO UPDATE SET value=excluded.value,reason=excluded.reason,created_at_ms=excluded.created_at_ms",
            [oid, namespace, object_id, dimension, value, reason, reviewer_id, now],
        )
        return {
            "override_id": oid,
            "object_id": object_id,
            "dimension": dimension,
            "value": value,
            "reason": reason,
            "reviewer_id": reviewer_id,
        }

    def health(self, namespace, assessment_ids, *, scopes, limit=500):
        _req(scopes, READ_SCOPE)
        items = [
            self.get(namespace, v, scopes=scopes)
            for v in list(assessment_ids)[: _bounded(limit)]
        ]
        missing = {
            d: sum(not v["dimensions"].get(d, {"known": False})["known"] for v in items)
            for d in DIMENSIONS
        }
        flags = {
            f: sum(f in v["flags"] for v in items)
            for f in {x for v in items for x in v["flags"]}
        }
        p = {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "objects": len(items),
            "missing_dimensions": missing,
            "flags": flags,
            "degraded": any(missing.values()) or any(flags.values()),
            "assessments_retained": len(items),
        }
        p["health_hash"] = _hash(p)
        return p
