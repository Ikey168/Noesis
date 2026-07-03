"""
Calibration for the review-gated OSINT tools (M7.3, review-gate criterion 2).

The gated tools (``geolocate_claims``, ``narrative_coordination`` in
``src/osint/gated.py``) stay behind the ``NOESIS_OSINT_GATED_TOOLS`` flag until,
among other criteria, their thresholds are calibrated against a labeled fixture
with a documented false-positive rate. This module is that calibration:

* :func:`calibrate_coordination` sweeps ``narrative_coordination``'s
  ``min_similarity`` over a labeled set of coordinated vs coincidental cohorts
  and reports, per threshold, the false-positive and true-positive rates, then
  recommends the smallest threshold whose FPR is within the target.
* :func:`geolocate_person_refusal_rate` measures that ``geolocate_claims`` never
  emits a *person* location on a labeled set of person entities (its most
  abusable failure mode), so its person-location false-positive rate is 0.

Stdlib + duckdb (imported lazily); no network, no live model.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

TARGET_FPR = 0.1
DEFAULT_LEVELS = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def _build_warehouse(cohort: Sequence[Tuple[str, str]]):
    """A tiny in-memory warehouse: one document per (source, claim_text)."""
    import duckdb

    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, "
        "document_id VARCHAR, source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    for i, (source, text) in enumerate(cohort):
        did = f"d{i}"
        conn.execute(
            "INSERT INTO news_articles (id, url, source, publish_date) VALUES (?,?,?,?)",
            [did, f"http://x/{i}", source, "2026-06-01"],
        )
        conn.execute(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type) "
            "VALUES (?,?,?,?)",
            [f"k{i}", text, did, "news"],
        )
    return conn


def calibrate_coordination(
    scenarios: List[Dict[str, Any]], levels: Optional[Sequence[float]] = None
) -> Dict[str, Any]:
    """Sweep ``min_similarity`` over labeled scenarios. Each scenario is
    ``{"cohort": [(source, claim_text), ...], "coordinated": bool}``. Returns the
    per-threshold FPR/TPR and the recommended threshold (smallest with
    ``fpr <= TARGET_FPR`` and a usable ``tpr``)."""
    from src.osint.gated import narrative_coordination

    levels = list(levels or DEFAULT_LEVELS)
    rows: List[Dict[str, Any]] = []
    for level in levels:
        fp = tp = pos = neg = 0
        for sc in scenarios:
            conn = _build_warehouse(sc["cohort"])
            try:
                out = narrative_coordination(conn, min_similarity=level)
            finally:
                conn.close()
            flagged = out.get("count", 0) > 0
            if sc["coordinated"]:
                pos += 1
                tp += 1 if flagged else 0
            else:
                neg += 1
                fp += 1 if flagged else 0
        rows.append(
            {
                "min_similarity": level,
                "fpr": round(fp / neg, 4) if neg else 0.0,
                "tpr": round(tp / pos, 4) if pos else 1.0,
                "false_positives": fp,
                "true_positives": tp,
            }
        )
    recommended = next(
        (r for r in rows if r["fpr"] <= TARGET_FPR and r["tpr"] >= 0.5), None
    )
    if recommended is None:
        recommended = min(rows, key=lambda r: (r["fpr"], -r["tpr"]))
    return {"target_fpr": TARGET_FPR, "levels": rows, "recommended": recommended}


def geolocate_person_refusal_rate(person_entities: Sequence[str]) -> Dict[str, Any]:
    """Measure ``geolocate_claims``'s person-location false-positive rate on a
    labeled set of person entities: it must refuse each (never emit a location),
    so the rate is 0. Returns the count refused and the FPR."""
    from src.osint.gated import geolocate_claims

    conn = _build_warehouse([("Alpha Wire", "A person spoke at a summit in Paris.")])
    # Register the people as person entities so the guardrail applies.
    conn.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, "
        "actor_name VARCHAR, entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    for i, person in enumerate(person_entities):
        conn.execute(
            "INSERT INTO document_actors (document_id, actor_name, entity_id, role) VALUES (?,?,?,?)",
            ["d0", person, f"person:{i}", "speaker"],
        )
    refused = 0
    try:
        for person in person_entities:
            out = geolocate_claims(conn, entity=person)
            if out.get("code") == "person_geolocation_refused" or not out.get("locations"):
                refused += 1
    finally:
        conn.close()
    n = len(person_entities)
    return {"people": n, "refused": refused, "person_location_fpr": (n - refused) / n if n else 0.0}
