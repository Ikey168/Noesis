import duckdb
import pytest

from src.kb.forecasts import ForecastStore, ForecastError

AUTH = {"principal_id": "alice", "scopes": {"knowledge:forecasts:read", "knowledge:forecasts:write", "namespace:r:write"}}
EVIDENCE = [{"kind": "source", "id": "release", "revision": "v1", "namespace": "r"}]


def setup():
    clock = [100]
    store = ForecastStore(duckdb.connect(), now=lambda: clock[0])
    return store, clock


def create(store, key="f", **kwargs):
    return store.create("r", key, question="Will the value exceed ten?", outcome_rule="Official final release exceeds 10 units.",
        resolution_at_ms=1000, probability=0.8, evidence=EVIDENCE, **kwargs, **AUTH)


def test_cutoff_probability_history_and_retrospective_outcome_correction():
    store, clock = setup()
    forecast = create(store)
    fid = forecast["forecast_id"]
    clock[0] = 200
    store.revise("r", fid, 1, probability=0.6, evidence=EVIDENCE, rationale="New information", **AUTH)
    assert store.inspect("r", fid, cutoff_ms=150, **AUTH)["probability"] == 0.8
    clock[0] = 1000
    store.resolve("r", fid, 0, status="resolved", outcome=1, evidence=EVIDENCE, rationale="Final release", forecast_revision=2, **AUTH)
    score = store.score("r", [fid], cutoff_ms=150, **AUTH)
    assert score["mean_brier"] == pytest.approx(0.04) and score["scored_count"] == 1
    assert score["baseline"]["mean_brier"] == 0.25 and score["ranking"] is None
    assert score["reliability_bins"][8]["count"] == 1
    assert score["reliability_bins"][8]["wilson_95_interval"][0] < 0.3
    clock[0] = 1200
    store.resolve("r", fid, 1, status="resolved", outcome=0, evidence=EVIDENCE, rationale="Corrected official release", forecast_revision=2, **AUTH)
    assert store.score("r", [fid], cutoff_ms=150, **AUTH)["mean_brier"] == pytest.approx(0.64)
    assert store.score("r", [fid], cutoff_ms=150, outcome_cutoff_ms=1100, **AUTH)["mean_brier"] == pytest.approx(0.04)
    assert create(store)["idempotent"]
    with pytest.raises(ForecastError, match="cannot change"):
        store.revise("r", fid, 2, probability=1, evidence=[], rationale="Hindsight", **AUTH)


def test_rule_changes_and_unresolved_disputed_cancelled_are_not_silently_scored():
    store, clock = setup()
    ids = [create(store, str(i))["forecast_id"] for i in range(4)]
    clock[0] = 200
    store.revise("r", ids[0], 1, probability=0.7, evidence=EVIDENCE, rationale="Clarify units", outcome_rule="Ten seasonally adjusted units", **AUTH)
    clock[0] = 1000
    for fid, status, revision in [(ids[0], "resolved", 2), (ids[1], "disputed", 1), (ids[2], "cancelled", 1)]:
        store.resolve("r", fid, 0, status=status, outcome=1 if status == "resolved" else None, evidence=EVIDENCE,
                      rationale="Reviewed", forecast_revision=revision, **AUTH)
    score = store.score("r", ids, cutoff_ms=150, **AUTH)
    assert score["scored_count"] == 0 and score["mean_brier"] is None
    assert {item["reason"] for item in score["excluded"]} == {"rule-changed-after-cutoff", "disputed", "cancelled", "unresolved"}
    assert store.score("r", ids, cutoff_ms=50, **AUTH)["scored_count"] == 0


def test_probabilities_explicit_and_current_access_required():
    store, clock = setup()
    fid = create(store)["forecast_id"]
    for value in [True, float("nan"), 2, "0.8"]:
        with pytest.raises(ForecastError):
            store.revise("r", fid, 1, probability=value, evidence=[], rationale="Bad value", **AUTH)
    assert store.inspect("r", fid, **AUTH)["revision"] == 1
    with pytest.raises(ForecastError, match="namespace"):
        store.score("r", [fid], cutoff_ms=150, principal_id="alice", scopes={"knowledge:forecasts:read"})
    with pytest.raises(ForecastError, match="deadline"):
        store.resolve("r", fid, 0, status="resolved", outcome=1, evidence=EVIDENCE, rationale="Too early", forecast_revision=1, **AUTH)


def test_quantitative_match_is_review_only_and_exactly_scoped():
    from src.kb.quantitative import QuantitativeStore
    store, clock = setup()
    QuantitativeStore(store.conn)
    rule = {"namespace": "r", "metric_id": "m", "provider": "official", "provider_series_id": "series",
            "period": "2026-Q1", "unit_id": "u", "comparison": "gt", "threshold": "10"}
    fid = create(store, resolution_match=rule)["forecast_id"]
    clock[0] = 1000
    assert store.propose_resolution("r", fid, **AUTH)["reason"] == "no-matching-observation"
    store.conn.execute("""INSERT INTO quantitative_observations VALUES
        ('o','r','m','official','series','2026-Q1','11',false,'u',NULL,NULL,NULL,900,900,'v1','none',false,NULL,
         '{"source":"release"}',1,'{}','{}','alice','hash',900)""")
    proposal = store.propose_resolution("r", fid, **AUTH)
    assert proposal["proposed_outcome"] == 1 and proposal["requires_review"] and not proposal["published"]
    assert store.inspect("r", fid, **AUTH)["outcome"]["status"] == "unresolved"
    store.resolve("r", fid, 0, status="resolved", outcome=proposal["proposed_outcome"], evidence=proposal["evidence"],
                  rationale="Reviewed pinned official observation against the rule", forecast_revision=1, **AUTH)
    assert store.inspect("r", fid, **AUTH)["outcome"]["evidence"][0]["revision"] == "v1"
    store.conn.execute("UPDATE quantitative_observations SET preliminary=true")
    assert store.propose_resolution("r", fid, **AUTH)["proposed_outcome"] is None
