"""Unit tests for the R6 analytic modules (lead_lag, narratives, drift, kg)."""

from src.analytics.honesty import validate_analytic_output
from src.analytics.lead_lag import LeadLagJob, lead_lag_payload
from src.analytics.narratives import NarrativeJob, cluster_narratives_payload
from src.analytics.drift import forecast_topic_payload, semantic_drift_payload
from src.analytics.kg_analytics import kg_centrality_payload, kg_communities_payload
from src.analytics.framework import read_results, run_job


# ---------------------------------------------------------------------------
# lead_lag (#599)
# ---------------------------------------------------------------------------


def _lead_lag_rows():
    # Reuters spikes 2 days before The Guardian on the same topic.
    rows = []
    reuters_days = ["2025-06-02", "2025-06-05", "2025-06-08"]
    guardian_days = ["2025-06-04", "2025-06-07", "2025-06-10"]
    quiet = ["2025-06-01", "2025-06-03", "2025-06-06", "2025-06-09"]
    for d in quiet:
        rows.append({"source": "Reuters", "category": "energy", "publish_date": d, "title": "x"})
        rows.append({"source": "The Guardian", "category": "energy", "publish_date": d, "title": "x"})
    for d in reuters_days:
        rows += [{"source": "Reuters", "category": "energy", "publish_date": d, "title": "x"}] * 6
    for d in guardian_days:
        rows += [{"source": "The Guardian", "category": "energy", "publish_date": d, "title": "x"}] * 6
    return rows


def test_lead_lag_ranks_leader_first(seed, conn):
    seed.articles(conn, _lead_lag_rows())
    payload = lead_lag_payload(conn, "energy")
    assert validate_analytic_output(payload) == []
    ranking = payload["outlets"]
    assert ranking[0]["outlet"] == "Reuters"  # publishes first -> agenda-setter
    assert ranking[0]["lead_score"] >= ranking[-1]["lead_score"]
    assert payload["pairs"]


def test_lead_lag_requires_topic(seed, conn):
    seed.articles(conn, _lead_lag_rows())
    assert "error" in lead_lag_payload(conn, "")


def test_lead_lag_job_precomputes(seed, conn, lock):
    seed.articles(conn, _lead_lag_rows())
    result = run_job(LeadLagJob(), conn=conn, lock=lock, log_mlflow=False)
    assert result["job"] == "lead_lag"
    assert read_results(conn, "analytics_lead_lag")


# ---------------------------------------------------------------------------
# cluster_narratives (#600)
# ---------------------------------------------------------------------------


def test_cluster_narratives_groups_storylines(seed, conn):
    rows = []
    for _ in range(5):
        rows.append({"title": "solar subsidy grid renewable cost", "category": "energy", "publish_date": "2025-06-01"})
    for _ in range(5):
        rows.append({"title": "nuclear reactor safety inspection", "category": "energy", "publish_date": "2025-06-02"})
    seed.articles(conn, rows)
    payload = cluster_narratives_payload(conn, "energy")
    assert validate_analytic_output(payload) == []
    assert len(payload["clusters"]) >= 2
    top = payload["clusters"][0]
    assert top["size"] >= 3 and 0 <= top["cohesion"] <= 1


def test_cluster_narratives_job(seed, conn, lock):
    rows = [{"title": "solar subsidy grid renewable", "category": "energy", "publish_date": "2025-06-01"} for _ in range(4)]
    seed.articles(conn, rows)
    result = run_job(NarrativeJob(), conn=conn, lock=lock, log_mlflow=False)
    assert result["job"] == "cluster_narratives"


# ---------------------------------------------------------------------------
# semantic_drift + forecast_topic (#602)
# ---------------------------------------------------------------------------


def test_semantic_drift_detects_context_shift(seed, conn):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = []
    for i in range(6):  # early: energy about emissions
        rows.append({"title": "energy emissions treaty global", "publish_date": (now - timedelta(days=80 - i)).date().isoformat()})
    for i in range(6):  # late: energy about subsidy/security
        rows.append({"title": "energy subsidy security domestic", "publish_date": (now - timedelta(days=10 - i % 10)).date().isoformat()})
    seed.articles(conn, rows)
    payload = semantic_drift_payload(conn, "energy", window=90)
    assert validate_analytic_output(payload, interval_fields=["drift"]) == []
    assert payload["drift"]["value"] > 0.2  # meaningful drift
    assert payload["rising_terms"]


def test_semantic_drift_too_few_mentions(seed, conn):
    seed.articles(conn, [{"title": "energy policy", "publish_date": "2025-06-01"}])
    payload = semantic_drift_payload(conn, "energy")
    assert "note" in payload
    assert validate_analytic_output(payload) == []


def test_forecast_topic_always_has_interval(seed, conn):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    rows = []
    for day in range(20):
        for _ in range(3 + day % 4):  # varying daily volume
            rows.append({"category": "energy", "publish_date": (now - timedelta(days=20 - day)).date().isoformat()})
    seed.articles(conn, rows)
    payload = forecast_topic_payload(conn, "energy", horizon=5)
    assert validate_analytic_output(payload) == []
    assert len(payload["points"]) == 5
    for point in payload["points"]:
        band = point["forecast"]
        assert band["lo"] <= band["value"] <= band["hi"]  # never a bare point
        assert band["lo"] >= 0  # coverage volume clamped non-negative


def test_forecast_too_little_history(seed, conn):
    seed.articles(conn, [{"category": "energy", "publish_date": "2025-06-01"}])
    payload = forecast_topic_payload(conn, "energy")
    assert "note" in payload
    assert validate_analytic_output(payload) == []


# ---------------------------------------------------------------------------
# kg_communities + kg_centrality (#601)
# ---------------------------------------------------------------------------

NODES = ["p1", "p2", "p3", "o1", "o2", "o3"]
EDGES = [("p1", "p2"), ("p2", "p3"), ("p1", "p3"), ("o1", "o2"), ("o2", "o3"), ("o1", "o3")]
NAMES = {n: n.upper() for n in NODES}


def test_kg_communities_payload():
    payload = kg_communities_payload(NODES, EDGES, NAMES, kg="finance")
    assert validate_analytic_output(payload) == []
    assert payload["community_count"] == 2
    assert payload["kg"] == "finance"
    assert all("members" in c for c in payload["communities"])


def test_kg_centrality_payload():
    payload = kg_centrality_payload(NODES, EDGES, NAMES, top=3)
    assert validate_analytic_output(payload) == []
    assert len(payload["nodes_ranked"]) == 3
    top = payload["nodes_ranked"][0]
    assert "centrality" in top and "community" in top and "degree" in top
    assert top["node"] in NAMES.values()  # names mapped
