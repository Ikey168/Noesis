"""Contract tests for every analytic tool's output (R5 #596 done-when).

Builds a sample output from each analytic's payload function against a
seeded in-memory warehouse and asserts it satisfies the honesty contract;
then proves that stripping the uncertainty fields makes the same output
fail. Also checks the analytic tools' MCP outputSchemas advertise the
required fields.
"""

import pytest

from src.analytics.anomalies import detect_anomalies_payload
from src.analytics.confidence import (
    score_confidence_payload,
    stance_significance_payload,
)
from src.analytics.framework import run_job
from src.analytics.anomalies import AnomalyJob
from src.analytics.honesty import REQUIRED_FIELDS, honesty_output_schema, validate_analytic_output



def _anomaly_sample(seed, conn, lock):
    rows = []
    for day, n in [
        ("2025-06-01", 3), ("2025-06-02", 4), ("2025-06-03", 2),
        ("2025-06-04", 4), ("2025-06-05", 3), ("2025-06-06", 30),
    ]:
        rows += [("climate", day, 0.1)] * n
    seed.news(conn, rows)
    run_job(AnomalyJob(threshold=3.0), conn=conn, lock=lock, log_mlflow=False)
    return detect_anomalies_payload(conn, topic="climate")


def _confidence_sample(seed, conn, lock=None):
    seed.outlet_scores(
        conn,
        [
            ("Reuters", "2025-06-09", 0.74, 0.7, 0.82, 0.66),
            ("Reuters", "2025-06-16", 0.77, 0.71, 0.82, 0.78),
            ("Reuters", "2025-06-23", 0.73, 0.68, 0.79, 0.72),
        ],
    )
    return score_confidence_payload(conn, "Reuters", resamples=300)


def _significance_sample(seed, conn, lock=None):
    seed.source_stances(
        conn,
        [
            ("OutletA", "energy", "supportive", 18),
            ("OutletA", "energy", "critical", 2),
            ("OutletB", "energy", "critical", 19),
            ("OutletB", "energy", "supportive", 3),
        ],
    )
    return stance_significance_payload(conn, "OutletA", "OutletB", "energy", resamples=300)


# (builder, interval_fields) for every analytic tool.
SAMPLES = [
    ("detect_anomalies", _anomaly_sample, []),
    ("score_confidence", _confidence_sample, ["composite"]),
    ("stance_significance", _significance_sample, ["divergence"]),
]


@pytest.mark.parametrize("name,builder,interval_fields", SAMPLES)
def test_every_analytic_output_is_honesty_valid(name, builder, interval_fields, seed, conn, lock):
    payload = builder(seed, conn, lock)
    assert validate_analytic_output(payload, interval_fields=interval_fields) == [], name


@pytest.mark.parametrize("name,builder,interval_fields", SAMPLES)
def test_stripping_uncertainty_fails_the_contract(name, builder, interval_fields, seed, conn, lock):
    payload = builder(seed, conn, lock)
    # Drop the sample size: every analytic must then fail the contract.
    stripped = {k: v for k, v in payload.items() if k != "n"}
    assert validate_analytic_output(stripped, interval_fields=interval_fields), name
    # Replace a headline interval with a naked point estimate: also fails.
    for field in interval_fields:
        naked = dict(payload)
        naked[field] = naked[field]["value"]
        assert validate_analytic_output(naked, interval_fields=interval_fields), f"{name}.{field}"


def test_honesty_schema_used_by_tools_advertises_fields():
    # The schema the tools attach via honesty_output_schema requires the fields.
    schema = honesty_output_schema({"windows": {"type": "array"}})
    for field in REQUIRED_FIELDS:
        assert field in schema["required"]
