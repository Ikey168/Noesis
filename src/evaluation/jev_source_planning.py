"""Equal-budget yield and objective coverage comparison for source relevance."""

from collections.abc import Mapping, Sequence


def evaluate_source_relevance(cases: Sequence[Mapping]) -> dict:
    if not isinstance(cases, Sequence) or not 1 <= len(cases) <= 10_000:
        raise ValueError("bounded comparison cases required")
    baseline_yield = advised_yield = baseline_coverage = advised_coverage = 0.0
    for case in cases:
        if not isinstance(case, Mapping) or case.get("label_origin") not in {"independent-human", "fixture"}:
            raise ValueError("independent human or explicit fixture labels required")
        if not case.get("objective_input_hash") or not case.get("capability_version_set"):
            raise ValueError("versioned objective and capability set required")
        budget = case.get("budget")
        if type(budget) not in {int, float} or budget < 0:
            raise ValueError("nonnegative equal budget required")
        for side in ("baseline", "advised"):
            spend = case.get(side + "_spend")
            yield_score = case.get(side + "_evidence_yield")
            coverage_score = case.get(side + "_objective_coverage")
            if (type(spend) not in {int, float} or not 0 <= spend <= budget or
                type(yield_score) not in {int, float} or not 0 <= yield_score <= 1 or
                type(coverage_score) not in {int, float} or not 0 <= coverage_score <= 1):
                raise ValueError("both plans need bounded spend, yield, and coverage")
        baseline_yield += case["baseline_evidence_yield"]
        advised_yield += case["advised_evidence_yield"]
        baseline_coverage += case["baseline_objective_coverage"]
        advised_coverage += case["advised_objective_coverage"]
    count = len(cases)
    return {"contract": "noesis-jev-source-planning-evaluation-v1", "cases": count,
            "baseline_mean_yield": baseline_yield / count,
            "advised_mean_yield": advised_yield / count,
            "baseline_mean_coverage": baseline_coverage / count,
            "advised_mean_coverage": advised_coverage / count,
            "yield_delta": (advised_yield - baseline_yield) / count,
            "coverage_delta": (advised_coverage - baseline_coverage) / count,
            "automatic_source_plan_acceptance_enabled": False}
