"""Compare optional ADWIN with native thresholds on bounded ordered streams."""

import argparse
import hashlib
import json
import random
import resource
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb

from src.kb.adwin_evaluation import PIN, ADWINStream, detector
from src.kb.knowledge_anomalies import KnowledgeAnomalyStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    auth = {"principal_id": "evaluation", "scopes": {"operator"}}
    rng = random.Random(1470)
    cases = []
    for name in ("stationary", "abrupt", "gradual"):
        values = []
        for i in range(2048):
            probability = (
                0.05
                if name == "stationary" or i < 1024
                else 0.6
                if name == "abrupt"
                else min(0.6, 0.05 + (i - 1024) * 0.55 / 512)
            )
            values.append(int(rng.random() < probability))
        cases.append(
            {
                "name": name,
                "values": values,
                "onset": None if name == "stationary" else 1024,
                "kind": "synthetic Bernoulli failure stream; known generator onset",
            }
        )
    evidence_path = Path(
        "docs/development/workflow-implementation-evidence/wikidata-berlin-live.json"
    )
    evidence_bytes = evidence_path.read_bytes()
    evidence = json.loads(evidence_bytes)
    observations = sorted(
        [*evidence["prior_observations"], evidence],
        key=lambda x: datetime.fromisoformat(x["checked_at"]),
    )
    cases.append(
        {
            "name": "recorded-wikidata-acquisition",
            "values": [int(o["status"] != "available") for o in observations],
            "onset": None,
            "kind": "two actual acquisition outcomes spanning a code fix; insufficient for drift calibration",
            "source": str(evidence_path),
            "source_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        }
    )
    conn = duckdb.connect()
    try:
        native = KnowledgeAnomalyStore(conn)
        watch = native.register_watch(
            "operations",
            "baseline",
            1,
            "event_rate",
            {"category": "operational"},
            {"window": 64, "minimum_points": 32},
            {"kind": "zscore", "threshold": 3},
            {},
            **auth,
        )
        records = []
        for case in cases:
            values = case["values"]
            candidate = detector({"delta": 0.002})
            candidate_hits = []
            start = time.perf_counter()
            for i, value in enumerate(values):
                candidate.update(value)
                if candidate.drift_detected:
                    candidate_hits.append(i)
            candidate_ms = (time.perf_counter() - start) * 1000
            baseline_hits = []
            start = time.perf_counter()
            for i in range(len(values)):
                history = [{"value": v} for v in values[max(0, i - 64) : i + 1]]
                if native.simulate(
                    "operations", watch["watch_id"], history, scopes={"operator"}
                )["detected"]:
                    baseline_hits.append(i)
            baseline_ms = (time.perf_counter() - start) * 1000
            stream = ADWINStream(conn, "operations", case["name"], **auth)
            start = time.perf_counter()
            publications = []
            for offset in range(0, len(values), 1000):
                events = [
                    {
                        "event_id": f"{case['name']}:{i}",
                        "sequence": i + 1,
                        "observed_at_ms": (i + 1) * 1000,
                        "value": v,
                        "evidence_id": f"evaluation:{case['name']}:{i}",
                    }
                    for i, v in enumerate(values[offset : offset + 1000], offset)
                ]
                publications.extend(
                    stream.consume(str(offset), events, **auth)["anomaly_ids"]
                )
            persisted_ms = (time.perf_counter() - start) * 1000
            scored = []
            for name, hits, elapsed in [
                ("native-zscore", baseline_hits, baseline_ms),
                ("river-adwin", candidate_hits, candidate_ms),
            ]:
                onset = case["onset"]
                stationary_count = onset if onset is not None else len(values)
                false_alerts = sum(i < stationary_count for i in hits)
                scored.append(
                    {
                        "backend": name,
                        "alerts": hits,
                        "detection_delay_samples": next(
                            (i - onset for i in hits if i >= onset), None
                        )
                        if onset is not None
                        else None,
                        "stationary_alerts": false_alerts,
                        "stationary_alerts_per_1000_samples": false_alerts
                        / stationary_count
                        * 1000,
                        "elapsed_ms": elapsed,
                    }
                )
            records.append(
                {
                    **{k: v for k, v in case.items() if k != "values"},
                    "samples": len(values),
                    "values_sha256": hashlib.sha256(
                        json.dumps(values).encode()
                    ).hexdigest(),
                    "results": scored,
                    "persisted_adapter_ms": persisted_ms,
                    "published_anomalies": len(publications),
                }
            )
        report = {
            "schema": "noesis-adwin-evaluation-v1",
            "river": PIN,
            "seed": 1470,
            "delta": 0.002,
            "baseline": watch,
            "records": records,
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "decision": "defer default adoption: synthetic change detection works; actual history is too short and spans a code change; keep optional operational adapter until stream-specific calibration",
        }
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "results": [
                        {
                            "case": r["name"],
                            "scores": r["results"],
                            "persisted_adapter_ms": r["persisted_adapter_ms"],
                        }
                        for r in records
                    ],
                    "process_peak_rss_kib": report["process_peak_rss_kib"],
                }
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
