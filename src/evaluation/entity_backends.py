"""Native review-only entity scoring. These backends never merge graph nodes."""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
from collections import Counter

from src.evaluation.runtime_errors import BackendError
from src.knowledge_graph.foundation.ontology import EntityType
from src.knowledge_graph.foundation.resolution import _normalize_name

FIELDS = ("name", "affiliation", "address")


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _record(value):
    if not isinstance(value, dict) or any(
        not isinstance(value.get(k), str) or not value[k] or len(value[k]) > 1000
        for k in ("id", "revision", "name")
    ):
        raise ValueError("bounded entity identity, revision and name required")
    kind = EntityType(value.get("type", ""))
    aliases = value.get("aliases", [])
    if (
        not isinstance(aliases, list)
        or len(aliases) > 20
        or any(not isinstance(v, str) or not v or len(v) > 1000 for v in aliases)
    ):
        raise ValueError("invalid aliases")
    identifiers = value.get("identifiers", {})
    if (
        not isinstance(identifiers, dict)
        or len(identifiers) > 20
        or any(
            not isinstance(k, str)
            or not isinstance(v, str)
            or not k
            or not v
            or len(k + v) > 1000
            for k, v in identifiers.items()
        )
    ):
        raise ValueError("bounded explicit identifiers required")
    for field in ("affiliation", "address"):
        if value.get(field) is not None and (
            not isinstance(value[field], str) or len(value[field]) > 1000
        ):
            raise ValueError("bounded text attributes required")
    normalized = {
        "name": _normalize_name(kind, value["name"]),
        **{
            key: " ".join((value.get(key) or "").casefold().split()) or None
            for key in ("affiliation", "address")
        },
    }
    return {
        **copy.deepcopy(value),
        "type": kind.value,
        "aliases": aliases,
        "identifiers": identifiers,
        "normalized": normalized,
    }


def _safeguards(left, right):
    shared = left["identifiers"].keys() & right["identifiers"].keys()
    conflicts = sorted(
        key for key in shared if left["identifiers"][key] != right["identifiers"][key]
    )
    return {
        "explicit_identifier_matches": sorted(shared - set(conflicts)),
        "explicit_identifier_conflicts": conflicts,
        "type_compatible": left["type"] == right["type"],
        "eligible_for_review": left["type"] == right["type"] and not conflicts,
        "automatic_merge": False,
    }


class RapidFuzzCandidates:
    """Batch fuzzy scoring over names/aliases; thresholds must be calibrated separately."""

    def __init__(self, *, threshold, max_candidates=1000):
        if (
            type(threshold) not in {int, float}
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError("a separately calibrated RapidFuzz threshold is required")
        if type(max_candidates) is not int or not 1 <= max_candidates <= 1000:
            raise ValueError("candidate bound must be 1..1000")
        self.threshold, self.max_candidates = threshold, max_candidates

    def score(self, source, candidates):
        try:
            from rapidfuzz import fuzz, process
        except ImportError as exc:
            raise BackendError(
                "optional_dependency_unavailable", "RapidFuzz is not installed"
            ) from exc
        if not isinstance(candidates, list) or len(candidates) > self.max_candidates:
            raise ValueError("candidate budget exceeded")
        left, right = _record(source), [_record(v) for v in candidates]
        if len({v["id"] for v in right}) != len(right):
            raise ValueError("candidate IDs must be unique")
        forms = [
            _normalize_name(EntityType(left["type"]), name)
            for name in [left["name"], *left["aliases"]]
        ]
        rows = []
        for candidate in right:
            guards = _safeguards(left, candidate)
            if candidate["id"] == left["id"]:
                continue
            names = [
                _normalize_name(EntityType(candidate["type"]), name)
                for name in [candidate["name"], *candidate["aliases"]]
            ]
            # Native C++ batch matrix <=21x21, one worker to avoid oversubscription.
            score = (
                float(process.cdist(forms, names, scorer=fuzz.ratio, workers=1).max())
                / 100
            )
            rows.append(
                {
                    "id": candidate["id"],
                    "revision": candidate["revision"],
                    "source_revision": left["revision"],
                    "score": score,
                    **guards,
                    "candidate": guards["eligible_for_review"]
                    and (
                        bool(guards["explicit_identifier_matches"])
                        or score >= self.threshold
                    ),
                    "field_evidence": {
                        "metric": "rapidfuzz.fuzz.ratio",
                        "name_alias_max": score,
                    },
                    "provenance": copy.deepcopy(candidate.get("provenance", {})),
                }
            )
        rows.sort(
            key=lambda row: (
                -row["eligible_for_review"],
                -bool(row["explicit_identifier_matches"]),
                -row["score"],
                row["id"],
            )
        )
        return {
            "backend": "rapidfuzz",
            "version": importlib.metadata.version("rapidfuzz"),
            "source_id": left["id"],
            "threshold": self.threshold,
            "candidates": rows,
            "ambiguous": sum(row["candidate"] for row in rows) > 1,
            "input_sha256": _hash([source, candidates]),
            "automatic_merge": False,
        }


def _level(left, right):
    if left is None or right is None:
        return -1
    if left == right:
        return 2
    if abs(len(left) - len(right)) > 2:
        return 0
    previous = list(range(len(right) + 1))
    for i, x in enumerate(left, 1):
        current = [i]
        for j, y in enumerate(right, 1):
            current.append(
                min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (x != y))
            )
        previous = current
    return 1 if previous[-1] <= 2 else 0


def train_splink_policy(pairs, *, prior_probability=0.01):
    """Fit explicit labelled TRAIN pairs with add-one m/u smoothing.

    The explicit population prior is not inferred from case-control label balance.
    Test rows never participate in fitting; origin metadata is not human identity
    certification and model/fixture labels remain labelled as such.
    """
    if (
        not isinstance(pairs, list)
        or not 2 <= len(pairs) <= 10000
        or not 0 < prior_probability < 1
    ):
        raise ValueError("bounded labelled training pairs and prior required")
    counts = {field: {True: Counter(), False: Counter()} for field in FIELDS}
    ids, groups, origins, outcomes = set(), set(), set(), set()
    for pair in pairs:
        if (
            pair.get("split") != "train"
            or type(pair.get("match")) is not bool
            or not pair.get("id")
            or pair["id"] in ids
        ):
            raise ValueError("unique explicitly labelled train pairs required")
        if pair.get("label_origin") not in {
            "independent-human",
            "assisted-human",
            "model",
            "fixture",
        } or not pair.get("group_id"):
            raise ValueError("training provenance and related-document group required")
        left, right = _record(pair["left"]), _record(pair["right"])
        if pair["match"] and not _safeguards(left, right)["eligible_for_review"]:
            raise ValueError(
                "positive label conflicts with authoritative identity/type"
            )
        for field in FIELDS:
            level = _level(left["normalized"][field], right["normalized"][field])
            if level >= 0:
                counts[field][pair["match"]][level] += 1
        ids.add(pair["id"])
        groups.add(pair["group_id"])
        origins.add(pair["label_origin"])
        outcomes.add(pair["match"])
    if outcomes != {True, False}:
        raise ValueError("positive and negative training examples required")
    parameters = {
        field: {
            name: [
                (counts[field][truth][level] + 1)
                / (sum(counts[field][truth].values()) + 3)
                for level in (0, 1, 2)
            ]
            for name, truth in (("m", True), ("u", False))
        }
        for field in FIELDS
    }
    policy = {
        "contract": "noesis-splink-policy-v1",
        "fields": list(FIELDS),
        "parameters": parameters,
        "prior_probability": prior_probability,
        "training_ids": sorted(ids),
        "training_groups": sorted(groups),
        "label_origins": sorted(origins),
        "training_sha256": _hash(pairs),
        "smoothing": "add-one",
        "normalization": "noesis-name-and-casefold-whitespace-v1",
        "adoption": "not-established",
    }
    return {**policy, "sha256": _hash(policy)}


class SplinkCandidates:
    """Native Splink inference with non-executable, hash-checked policy parameters."""

    def __init__(self, policy, *, max_records=200):
        core = {key: value for key, value in policy.items() if key != "sha256"}
        if (
            policy.get("contract") != "noesis-splink-policy-v1"
            or _hash(core) != policy.get("sha256")
            or policy.get("fields") != list(FIELDS)
        ):
            raise ValueError("invalid or changed Splink policy")
        if type(max_records) is not int or not 2 <= max_records <= 200:
            raise ValueError("Splink exact-evaluation limit must be 2..200 records")
        for field in FIELDS:
            for key in ("m", "u"):
                values = policy["parameters"][field][key]
                if (
                    len(values) != 3
                    or any(type(v) not in {int, float} or not 0 < v < 1 for v in values)
                    or not math.isclose(sum(values), 1)
                ):
                    raise ValueError("invalid fitted comparison probabilities")
        if not 0 < policy["prior_probability"] < 1:
            raise ValueError("invalid match prior")
        self.policy, self.max_records = copy.deepcopy(policy), max_records

    def predict(self, records, *, evaluation_group_ids=()):
        if not 2 <= len(records) <= self.max_records:
            raise ValueError("Splink record budget exceeded")
        if set(evaluation_group_ids) & set(self.policy["training_groups"]):
            raise ValueError("training/evaluation group leakage")
        try:
            import duckdb
            import pandas as pd
            from splink import DuckDBAPI, Linker, SettingsCreator
        except ImportError as exc:
            raise BackendError(
                "optional_dependency_unavailable", "install the Splink evaluation extra"
            ) from exc
        clean = [_record(record) for record in records]
        by_id = {row["id"]: row for row in clean}
        if len(by_id) != len(clean):
            raise ValueError("unique record identities required")
        comparisons = []
        for field in FIELDS:
            parameters = self.policy["parameters"][field]
            conditions = [
                f'"{field}_l" = "{field}_r"',
                f'levenshtein("{field}_l", "{field}_r") <= 2',
                "ELSE",
            ]
            levels = [
                {
                    "sql_condition": f'"{field}_l" IS NULL OR "{field}_r" IS NULL',
                    "is_null_level": True,
                }
            ]
            for level, condition in zip((2, 1, 0), conditions, strict=True):
                levels.append(
                    {
                        "sql_condition": condition,
                        "m_probability": parameters["m"][level],
                        "u_probability": parameters["u"][level],
                    }
                )
            comparisons.append(
                {"output_column_name": field, "comparison_levels": levels}
            )
        settings = SettingsCreator(
            link_type="dedupe_only",
            comparisons=comparisons,
            blocking_rules_to_generate_predictions=[
                'l."entity_type" = r."entity_type"'
            ],
            probability_two_random_records_match=self.policy["prior_probability"],
            retain_intermediate_calculation_columns=True,
        )
        frame = pd.DataFrame(
            [
                {
                    **row["normalized"],
                    "unique_id": row["id"],
                    "entity_type": row["type"],
                }
                for row in clean
            ]
        ).astype(dict.fromkeys(FIELDS, "string"))
        conn = duckdb.connect(config={"threads": 2, "memory_limit": "512MB"})
        try:
            linker = Linker(
                frame, settings, DuckDBAPI(conn), set_up_basic_logging=False
            )
            predictions = linker.inference.predict(
                threshold_match_probability=0.0
            ).as_record_dict()
            rows = []
            for value in predictions:
                left, right = (
                    by_id[str(value["unique_id_l"])],
                    by_id[str(value["unique_id_r"])],
                )
                probability = float(value["match_probability"])
                if not math.isfinite(probability) or not 0 <= probability <= 1:
                    raise BackendError(
                        "invalid_model_output", "invalid native Splink probability"
                    )
                rows.append(
                    {
                        "left_id": left["id"],
                        "right_id": right["id"],
                        "left_revision": left["revision"],
                        "right_revision": right["revision"],
                        "score": probability,
                        **_safeguards(left, right),
                        "field_evidence": {
                            field: {
                                "comparison_level": int(value["gamma_" + field]),
                                "left": left["normalized"][field],
                                "right": right["normalized"][field],
                            }
                            for field in FIELDS
                        },
                    }
                )
        finally:
            conn.close()
        rows.sort(
            key=lambda row: (
                -row["eligible_for_review"],
                -bool(row["explicit_identifier_matches"]),
                -row["score"],
                row["left_id"],
                row["right_id"],
            )
        )
        return {
            "backend": "splink",
            "version": importlib.metadata.version("splink"),
            "policy_sha256": self.policy["sha256"],
            "input_sha256": _hash(records),
            "candidates": rows,
            "automatic_merge": False,
            "score_semantics": "posterior under explicit prior and fitted m/u; not verified identity",
        }
