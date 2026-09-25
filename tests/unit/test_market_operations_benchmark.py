from scripts.market_operations_benchmark import run_benchmark


def test_local_operations_benchmark_is_bounded_and_labels_cost_limitations():
    result = run_benchmark(25)

    assert result["evidence_kind"] == "local_benchmark"
    assert result["observed"]["accepted_rows"] == 25
    assert result["observed"]["ingest_rows_per_second"] > 0
    assert result["observed"]["provider_health_state"] == "healthy"
    assert result["cost"] == {
        "provider_requests": 0,
        "provider_cost_micros": 0,
        "infrastructure_cost_status": "not_measured",
    }
    assert any("not production" in item for item in result["limitations"])
