#!/usr/bin/env python3
"""Execute the optional datasketch MinHash LSH text-reuse benchmark."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import sys
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.evaluation.minhash_reuse import (
    MinHashReuseIndex,
    ReuseRecord,
    exhaustive_adjudication,
    jaccard,
    shingles,
)


def _records(values):
    return [
        ReuseRecord(
            record_id=value["record_id"],
            text=value["text"],
            revision=value["revision"],
            provenance_links=tuple(value.get("provenance_links") or ()),
        )
        for value in values
    ]


def _candidate_pairs(index: MinHashReuseIndex, records: list[ReuseRecord]):
    pairs = set()
    decisions = set()
    for record in records:
        for row in index.adjudicate(record, exact_threshold=0.5):
            pair = tuple(sorted((record.record_id, row["record_id"])))
            pairs.add(pair)
            if row["reuse_decision"]:
                decisions.add(pair)
    return pairs, decisions


def _score_threshold(records: list[ReuseRecord], threshold: float):
    index = MinHashReuseIndex(threshold=threshold, seed=17)
    for record in records:
        index.add(record)
    expected = exhaustive_adjudication(records, exact_threshold=0.5)
    candidates, decisions = _candidate_pairs(index, records)
    return {
        "threshold": threshold,
        "candidate_recall": len(candidates & expected) / len(expected)
        if expected
        else 1.0,
        "candidate_pairs": len(candidates),
        "final_decision_recall": len(decisions & expected) / len(expected)
        if expected
        else 1.0,
        "final_false_decisions": len(decisions - expected),
    }


def _synthetic_records(size: int) -> list[ReuseRecord]:
    output = []
    for i in range(size):
        group = i // 10
        variant = i % 10
        core = (
            f"research evidence group {group} describes policy measure budget outcome "
            f"jurisdiction Berlin source identifier {group}"
        )
        if variant == 1:
            text = core
        elif variant == 2:
            text = "context preface " + core + " additional commentary"
        else:
            text = f"unique document {i} discusses topic {variant} and observation {i * 17}"
        output.append(ReuseRecord(f"scale-{i}", text, "r1"))
    return output


def _scale(size: int, threshold: float):
    records = _synthetic_records(size)
    tracemalloc.start()
    start = time.perf_counter()
    index = MinHashReuseIndex(threshold=threshold, seed=17)
    for record in records:
        index.add(record)
    build_seconds = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    start = time.perf_counter()
    candidate_pairs, decisions = _candidate_pairs(index, records)
    candidate_seconds = time.perf_counter() - start

    sets = {r.record_id: shingles(r.text, 5) for r in records}
    start = time.perf_counter()
    exhaustive = set()
    for i, left in enumerate(records):
        for right in records[i + 1 :]:
            if jaccard(sets[left.record_id], sets[right.record_id]) >= 0.5:
                exhaustive.add(tuple(sorted((left.record_id, right.record_id))))
    exhaustive_seconds = time.perf_counter() - start

    state = index.export_state()
    state_bytes = len(json.dumps(state, separators=(",", ":")).encode())
    start = time.perf_counter()
    index.add(
        ReuseRecord(
            records[0].record_id, records[0].text + " revised observation", "r2"
        )
    )
    update_seconds = time.perf_counter() - start
    start = time.perf_counter()
    index.remove(records[1].record_id)
    delete_seconds = time.perf_counter() - start
    return {
        "records": size,
        "build_seconds": build_seconds,
        "candidate_query_seconds_all_records": candidate_seconds,
        "exhaustive_pair_scan_seconds": exhaustive_seconds,
        "candidate_pairs": len(candidate_pairs),
        "final_decisions": len(decisions),
        "exhaustive_decisions": len(exhaustive),
        "final_matches_exhaustive": decisions == exhaustive,
        "serialized_state_bytes": state_bytes,
        "peak_tracemalloc_bytes": peak,
        "update_seconds": update_seconds,
        "delete_seconds": delete_seconds,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/workflow_review/minhash_reuse.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    development = _records(fixture["development"])
    test = _records(fixture["test"])

    curve = [
        _score_threshold(development, value) for value in (0.2, 0.3, 0.35, 0.4, 0.5)
    ]
    viable = [
        row
        for row in curve
        if row["candidate_recall"] == 1.0 and row["final_false_decisions"] == 0
    ]
    # Candidate generation should bias toward recall. When several development
    # thresholds preserve every known reuse pair without changing final exact
    # decisions, select the lowest LSH threshold rather than the sparsest/highest
    # threshold. Exact Jaccard/provenance still controls the final decision.
    selected = min(viable, key=lambda row: row["threshold"])

    tracemalloc.start()
    start = time.perf_counter()
    index = MinHashReuseIndex(threshold=selected["threshold"], seed=17)
    for record in test:
        index.add(record)
    build_seconds = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    expected = exhaustive_adjudication(test, exact_threshold=0.5)
    start = time.perf_counter()
    candidates, decisions = _candidate_pairs(index, test)
    query_seconds = time.perf_counter() - start

    state = index.export_state()
    replay = MinHashReuseIndex.from_state(json.loads(json.dumps(state)))
    replay_candidates, replay_decisions = _candidate_pairs(replay, test)

    changed = ReuseRecord(
        "test-original",
        "Completely revised gardening notice unrelated to the prior energy evidence",
        "r2",
    )
    start = time.perf_counter()
    index.add(changed)
    update_seconds = time.perf_counter() - start
    start = time.perf_counter()
    removed = index.remove("test-unrelated")
    delete_seconds = time.perf_counter() - start

    scaling = [_scale(size, selected["threshold"]) for size in (50, 200, 1000)]
    report = {
        "contract": "noesis-minhash-reuse-evaluation-v1",
        "package": {
            "distribution": "datasketch",
            "version": importlib.metadata.version("datasketch"),
        },
        "fixture_contract": fixture["contract"],
        "label_origin": fixture["label_origin"],
        "configuration": {
            "shingle_size": 5,
            "num_perm": 128,
            "seed": 17,
            "exact_adjudication_threshold": 0.5,
            "selected_lsh_threshold": selected["threshold"],
        },
        "development_threshold_curve": curve,
        "test": {
            "records": len(test),
            "expected_exact_or_provenance_pairs": sorted(
                [list(pair) for pair in expected]
            ),
            "candidate_pairs": sorted([list(pair) for pair in candidates]),
            "final_decisions": sorted([list(pair) for pair in decisions]),
            "candidate_recall": len(candidates & expected) / len(expected)
            if expected
            else 1.0,
            "final_decision_recall": len(decisions & expected) / len(expected)
            if expected
            else 1.0,
            "final_false_decisions": len(decisions - expected),
            "build_seconds": build_seconds,
            "query_seconds_all_records": query_seconds,
            "peak_tracemalloc_bytes": peak,
        },
        "replay": {
            "candidate_pairs_identical": replay_candidates == candidates,
            "final_decisions_identical": replay_decisions == decisions,
            "version": replay.version,
        },
        "revision_and_delete": {
            "updated_revision": index.records["test-original"].revision,
            "update_seconds": update_seconds,
            "deleted_unrelated_record": removed,
            "delete_seconds": delete_seconds,
        },
        "scaling": scaling,
        "mandatory_provenance_links_bypass_lsh_misses": True,
        "production_default_changed": False,
        "decision": (
            "retain optional candidate path for further evaluation: largest measured corpus improves query time without changing exact decisions; production default remains unchanged"
            if scaling[-1]["final_matches_exhaustive"]
            and scaling[-1]["candidate_query_seconds_all_records"]
            < scaling[-1]["exhaustive_pair_scan_seconds"]
            else "defer production adoption: no exact-decision-preserving query-time advantage at the largest measured corpus size; retain exhaustive baseline"
        ),
        "limitations": [
            "Ground truth is exact-shingle/provenance behavior on an authored held-out regression corpus, not independent human semantic judgments.",
            "tracemalloc measures Python allocations rather than complete process RSS.",
            "MinHash is only lexical candidate generation and does not establish source independence or semantic equivalence.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
