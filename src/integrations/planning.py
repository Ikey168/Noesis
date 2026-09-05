"""Bounded source selection over existing planner candidates."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from .common import IntegrationError, finite, version


def select_sources(candidates, constraints, part_count, *, timeout_seconds=2):
    from ortools.sat.python import cp_model

    timeout_seconds = finite(timeout_seconds, "solver timeout", 0.01, 30)
    if len(candidates) > 1000 or part_count > 1000:
        raise IntegrationError(
            "input_limit", "Planner model exceeds 1000 sources/parts"
        )
    model = cp_model.CpModel()
    variables = [model.new_bool_var(f"source_{i}") for i in range(len(candidates))]
    scale = 1_000_000
    costs = [
        int(
            (Decimal(str(c["projected_cost"])) * scale).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        for c in candidates
    ]
    budget = int(
        (Decimal(str(constraints["budget"])) * scale).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    if budget < 0 or any(c < 0 for c in costs):
        raise IntegrationError("invalid_input", "Negative acquisition cost")
    # CP-SAT uses signed int64 arithmetic, including the full objective domain.
    if budget > 2**62 or sum(costs) * (len(candidates) + 1) + len(candidates) > 2**62:
        raise IntegrationError(
            "input_limit", "Scaled acquisition costs exceed solver integer range"
        )
    model.add(sum(v * c for v, c in zip(variables, costs)) <= budget)
    model.add(sum(variables) <= int(constraints["max_sources"]))
    for required in constraints["required_sources"]:
        model.add(
            sum(
                v
                for v, c in zip(variables, candidates)
                if c["capability"]["source_id"] == required
            )
            == 1
        )
    for part in range(part_count):
        model.add(
            sum(v for v, c in zip(variables, candidates) if part in c["covered_parts"])
            >= 1
        )
    groups = {}
    for v, c in zip(variables, candidates):
        groups.setdefault(c["capability"]["dependency_group"], []).append(v)
    group_vars = []
    for i, members in enumerate(groups.values()):
        present = model.new_bool_var(f"group_{i}")
        model.add_max_equality(present, members)
        group_vars.append(present)
    model.add(sum(group_vars) >= int(constraints["min_independence"]))
    # Explicit objective: minimum conservative projected cost, then source count.
    model.minimize(
        sum(v * (c * (len(candidates) + 1) + 1) for v, c in zip(variables, costs))
    )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = timeout_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    usable = status in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    return {
        "selected_ids": [
            c["capability"]["source_id"]
            for v, c in zip(variables, candidates)
            if usable and solver.value(v)
        ],
        "status": solver.status_name(status),
        "backend": "ortools-cp-sat",
        "version": version("ortools"),
        "objective": "minimum projected cost, then source count; coverage and independence are hard constraints",
        "cost_scale": scale,
        "cost_rounding": "costs ceiling, budget floor",
        "timeout_seconds": timeout_seconds,
    }
