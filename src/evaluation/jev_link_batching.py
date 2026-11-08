"""Agreement check between per-link and batched Jev claim-link review.

Reviews the same already planned links once per link and once per batch
size, then reports agreement by relation and verdict, latency and token use.
It never writes claim links or assessments and never enables batching: the
recommendation is advice for an operator, not a switch.
"""

from __future__ import annotations

import math
import random
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any

REVIEWED = ("supports", "duplicate", "contradicts", "supersedes")


def sample_planned_links(
    conn, *, sample: int, seed: int = 0, duplicate_threshold: float = 0.88,
    relations: Sequence[str] = REVIEWED,
) -> list[dict[str, Any]]:
    """Review items for stored ``claim_links`` rows (newest first, then sampled).

    Stored ``supersedes``/``corrects``/``retracts`` links are sampled as the
    ``supersedes`` review relation (newer claim first), as the linking pass
    reviews them.
    """
    if not relations or any(r not in REVIEWED for r in relations):
        raise ValueError(f"relations must be drawn from {REVIEWED}")
    stored = [r for r in relations if r != "supersedes"]
    if "supersedes" in relations:
        stored += ["supersedes", "corrects", "retracts"]
    placeholders = ", ".join("?" for _ in stored)
    rows = conn.execute(
        f"""
        SELECT l.relation, l.claim_a, ca.claim_text, l.claim_b, cb.claim_text, l.score
        FROM claim_links l
        JOIN argument_claims ca ON ca.claim_id = l.claim_a
        JOIN argument_claims cb ON cb.claim_id = l.claim_b
        WHERE l.relation IN ({placeholders})
        ORDER BY l.created_at DESC, l.claim_a, l.claim_b, l.relation
        """,
        stored,
    ).fetchall()
    rows = list(rows)
    random.Random(seed).shuffle(rows)
    return [
        {
            "relation": "supersedes" if relation in ("corrects", "retracts") else relation,
            "premise": (claim_a, text_a),
            "hypothesis": (claim_b, text_b),
            "similarity": float(score),
            "duplicate_threshold": duplicate_threshold,
        }
        for relation, claim_a, text_a, claim_b, text_b, score in rows[:sample]
    ]


def _outcome(review: Mapping[str, Any]) -> tuple:
    return (review["status"], review["verdict"], review["jev_relation"])


def compare(
    items: Sequence[Mapping[str, Any]],
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Agreement of ``candidate`` with ``baseline`` (per-link) reviews.

    ``verdict_agreement`` is measured over links both paths answered;
    ``outcome_agreement`` requires identical status, verdict and Jev relation
    over every link. Status disagreements are tallied separately so a batch
    failure cannot pass as agreement.
    """
    if not len(items) == len(baseline) == len(candidate):
        raise ValueError("items and reviews must align")
    by_relation: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"links": 0, "both_answered": 0, "verdict_agree": 0, "outcome_agree": 0}
    )
    verdicts: Counter = Counter()
    statuses: Counter = Counter()
    for item, base, cand in zip(items, baseline, candidate, strict=True):
        row = by_relation[item["relation"]]
        row["links"] += 1
        row["outcome_agree"] += _outcome(base) == _outcome(cand)
        statuses[(base["status"], cand["status"])] += 1
        if base["status"] == cand["status"] == "answered":
            row["both_answered"] += 1
            row["verdict_agree"] += base["verdict"] == cand["verdict"]
            verdicts[(base["verdict"], cand["verdict"])] += 1
    report_rows = {}
    for relation, row in sorted(by_relation.items()):
        report_rows[relation] = {
            **row,
            "verdict_agreement": _rate(row["verdict_agree"], row["both_answered"]),
            "outcome_agreement": _rate(row["outcome_agree"], row["links"]),
        }
    total = {key: sum(r[key] for r in by_relation.values())
             for key in ("links", "both_answered", "verdict_agree", "outcome_agree")}
    return {
        "links": total["links"],
        "both_answered": total["both_answered"],
        "verdict_agreement": _rate(total["verdict_agree"], total["both_answered"]),
        "outcome_agreement": _rate(total["outcome_agree"], total["links"]),
        "by_relation": report_rows,
        "verdict_pairs": {f"{a}->{b}": n for (a, b), n in sorted(verdicts.items(), key=str)},
        "status_pairs": {f"{a}->{b}": n for (a, b), n in sorted(statuses.items(), key=str)},
    }


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def usage(stats: Sequence[Mapping[str, Any]], wall_s: float) -> dict[str, Any]:
    """Request count, latency and token totals for one review path."""
    latencies = [s["latency_s"] for s in stats if s.get("latency_s") is not None]
    tokens = [s["input_tokens"] for s in stats if s.get("input_tokens") is not None]
    settled = [s["settled_usd_micros"] for s in stats if s.get("settled_usd_micros") is not None]
    return {
        "requests": len(stats),
        "failed_requests": sum(s.get("status") != "completed" for s in stats),
        "wall_s": round(wall_s, 3),
        "request_latency_p50_s": _percentile(latencies, 0.5),
        "request_latency_p95_s": _percentile(latencies, 0.95),
        "input_tokens": sum(tokens) if tokens else None,
        "requests_without_usage": len(stats) - len(tokens),
        "settled_usd_micros": sum(settled) if settled else None,
    }


def run_validation(
    items: Sequence[Mapping[str, Any]],
    make_reviewer: Callable[[Any], Any],
    *,
    batch_sizes: Sequence[int] = (25, 120),
    run_prefix: str | None = None,
    min_agreement: float = 0.95,
    min_links_per_relation: int = 20,
    max_drift: float = 0.02,
    repeat_baseline: bool = True,
) -> dict[str, Any]:
    """Review ``items`` per link (twice) and at each batch size, and compare.

    ``make_reviewer(batch_config_or_None)`` returns a fresh reviewer. Run IDs
    are unique per invocation so no path replays another's stored results.

    The per-link path runs twice (``repeat_baseline``) so batching drift can
    be told apart from Jev's run-to-run variation: each batched comparison
    reports the per-link-vs-per-link agreement next to it. A relation is
    recommended for batching only when its batched verdict agreement reaches
    ``min_agreement`` and is within ``max_drift`` of the repeat agreement,
    on at least ``min_links_per_relation`` links answered by both paths.
    Each relation is judged on its own (``recommended_relations``), so
    batching can be enabled per relation via ``BatchConfig.relations``.
    """
    from src.kb.claim_link_batch import BatchConfig

    prefix = run_prefix or f"jev-batch-validation-{int(time.time() * 1000)}"
    items = list(items)
    paths: dict[str, Any] = {}

    def review(name, batch):
        reviewer = make_reviewer(batch)
        started = time.monotonic()
        reviews = reviewer.review_all(f"{prefix}:{name}", items)
        paths[name] = {"reviews": reviews, "usage": usage(reviewer.stats, time.monotonic() - started)}

    review("per_link", None)
    if repeat_baseline:
        review("per_link_repeat", None)
    for size in batch_sizes:
        # Batch every relation here so each gets its own agreement verdict.
        review(f"batch_{size}", BatchConfig(max_links=size, relations=REVIEWED))
    baseline = paths["per_link"]["reviews"]
    repeat = (
        compare(items, baseline, paths["per_link_repeat"]["reviews"]) if repeat_baseline else None
    )
    report: dict[str, Any] = {
        "links": len(items),
        "relations": dict(Counter(item["relation"] for item in items)),
        "per_link": paths["per_link"]["usage"],
        "per_link_repeat": (
            {"usage": paths["per_link_repeat"]["usage"], "agreement": repeat} if repeat else None
        ),
        "batched": {},
        "thresholds": {"min_agreement": min_agreement,
                       "min_links_per_relation": min_links_per_relation,
                       "max_drift": max_drift},
        "production_switch": False,
    }
    for size in batch_sizes:
        name = f"batch_{size}"
        agreement = compare(items, baseline, paths[name]["reviews"])
        report["batched"][name] = {
            "max_links": size,
            "usage": paths[name]["usage"],
            "agreement": agreement,
            "repeat_baseline": _baseline_view(repeat),
            **_recommendation(agreement, repeat, min_agreement, min_links_per_relation, max_drift),
        }
    return report


def _baseline_view(repeat: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if repeat is None:
        return None
    return {
        "verdict_agreement": repeat["verdict_agreement"],
        "outcome_agreement": repeat["outcome_agreement"],
        "by_relation": {
            relation: {"verdict_agreement": row["verdict_agreement"],
                       "outcome_agreement": row["outcome_agreement"]}
            for relation, row in repeat["by_relation"].items()
        },
    }


def _recommendation(agreement, repeat, min_agreement, min_links, max_drift) -> dict[str, Any]:
    reasons: list[str] = []
    recommended: list[str] = []
    per_relation: dict[str, Any] = {}
    for relation in REVIEWED:
        row = agreement["by_relation"].get(relation)
        base = (repeat or {}).get("by_relation", {}).get(relation)
        why = []
        if row is None or row["both_answered"] < min_links:
            why.append(f"fewer than {min_links} answered {relation} links")
        else:
            if row["verdict_agreement"] < min_agreement:
                why.append(f"{relation} verdict agreement {row['verdict_agreement']} < {min_agreement}")
            if base and base["verdict_agreement"] is not None and (
                row["verdict_agreement"] < base["verdict_agreement"] - max_drift
            ):
                why.append(
                    f"{relation} verdict agreement {row['verdict_agreement']} drifts more than"
                    f" {max_drift} below the per-link repeat {base['verdict_agreement']}"
                )
        per_relation[relation] = {"recommended": not why, "reasons": why}
        if why:
            reasons.extend(why)
        else:
            recommended.append(relation)
    present = [r for r in REVIEWED if r in agreement["by_relation"]]
    return {
        "recommended": bool(present) and all(per_relation[r]["recommended"] for r in present),
        "recommended_relations": recommended,
        "by_relation_recommendation": per_relation,
        "not_recommended_because": reasons,
    }
