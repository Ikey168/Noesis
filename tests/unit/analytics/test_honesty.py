"""Unit tests for the statistical-honesty convention (src/analytics/honesty.py)."""

from src.analytics.honesty import (
    REQUIRED_FIELDS,
    analytic_envelope,
    honesty_output_schema,
    interval,
    is_interval,
    validate_analytic_output,
)


def test_interval_orders_bounds_and_validates():
    iv = interval(0.5, 0.6, 0.4)  # lo/hi passed backwards
    assert iv["lo"] == 0.4 and iv["hi"] == 0.6
    assert is_interval(iv)


def test_is_interval_rejects_malformed():
    assert not is_interval({"value": 0.5, "lo": 0.4, "hi": 0.6})  # no level
    assert not is_interval({"value": 0.5, "lo": 0.6, "hi": 0.4, "level": 0.95})  # value outside
    assert not is_interval({"value": True, "lo": 0, "hi": 1, "level": 0.95})  # bool
    assert not is_interval({"value": 0.5, "lo": 0.4, "hi": 0.6, "level": 2})  # level > 1
    assert not is_interval("nope")


def test_envelope_carries_required_fields():
    env = analytic_envelope(12, "robust z", ["stable baseline"], flagged=3)
    assert all(k in env for k in REQUIRED_FIELDS)
    assert env["n"] == 12
    assert env["assumptions"] == ["stable baseline"]
    assert env["flagged"] == 3


def test_validate_accepts_well_formed_output():
    env = analytic_envelope(5, "m", ["a"], composite=interval(0.5, 0.4, 0.6))
    assert validate_analytic_output(env, interval_fields=["composite"]) == []


def test_validate_rejects_missing_uncertainty_fields():
    # The core contract: an output missing n/method/assumptions fails.
    assert validate_analytic_output({"composite": interval(0.5, 0.4, 0.6)})
    assert validate_analytic_output({"n": 5, "method": "m"})  # no assumptions
    assert "invalid 'n'" in " ".join(validate_analytic_output({"n": -1, "method": "m", "assumptions": []}))
    assert validate_analytic_output({"n": 5, "method": "", "assumptions": []})


def test_validate_rejects_naked_point_estimate():
    # A headline figure given as a bare float (not an interval) is a violation.
    env = analytic_envelope(5, "m", ["a"], composite=0.5)
    errors = validate_analytic_output(env, interval_fields=["composite"])
    assert any("not a well-formed interval" in e for e in errors)
    # And a missing interval field is a violation.
    env2 = analytic_envelope(5, "m", ["a"])
    assert any("missing interval field" in e for e in validate_analytic_output(env2, interval_fields=["composite"]))


def test_validate_exempts_error_responses():
    assert validate_analytic_output({"error": "warehouse locked"}) == []


def test_validate_rejects_non_object():
    assert validate_analytic_output([1, 2, 3]) == ["output is not an object"]


def test_honesty_output_schema_requires_the_fields():
    schema = honesty_output_schema({"outlet": {"type": "string"}}, required=["outlet"])
    assert schema["type"] == "object"
    for field in REQUIRED_FIELDS:
        assert field in schema["properties"]
        assert field in schema["required"]
    assert "outlet" in schema["properties"] and "outlet" in schema["required"]
