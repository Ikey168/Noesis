"""Independent exhaustive oracle for bounded source-selection constraints."""

import itertools
import random

import pytest

pytest.importorskip("ortools")

from src.integrations.planning import select_sources


def test_cp_sat_matches_exhaustive_optimum_and_infeasibility():
    randomizer = random.Random(1513)
    for _ in range(60):
        candidates = [
            {
                "capability": {
                    "source_id": f"source-{i}",
                    "dependency_group": f"group-{i % 3}",
                },
                "covered_parts": [
                    p for p in range(3) if randomizer.choice([True, False])
                ],
                "projected_cost": randomizer.randint(0, 6),
            }
            for i in range(6)
        ]
        required = ["source-0"] if randomizer.choice([True, False]) else []
        budget = randomizer.randint(0, 12)
        maximum = randomizer.randint(1, 6)
        independence = randomizer.randint(1, 3)
        # Enumerate subsets directly; do not reuse the solver's arithmetic/model.
        feasible = []
        for count in range(maximum + 1):
            for subset in itertools.combinations(candidates, count):
                ids = {c["capability"]["source_id"] for c in subset}
                groups = {c["capability"]["dependency_group"] for c in subset}
                covered = {p for c in subset for p in c["covered_parts"]}
                cost = sum(c["projected_cost"] for c in subset)
                if (
                    set(required) <= ids
                    and covered == {0, 1, 2}
                    and len(groups) >= independence
                    and cost <= budget
                ):
                    feasible.append((cost, count, ids))
        result = select_sources(
            candidates,
            {
                "budget": budget,
                "max_sources": maximum,
                "min_independence": independence,
                "required_sources": required,
            },
            3,
        )
        if not feasible:
            assert result["status"] == "INFEASIBLE"
            assert result["selected_ids"] == []
        else:
            assert result["status"] == "OPTIMAL"
            ids = set(result["selected_ids"])
            selected_cost = sum(
                c["projected_cost"]
                for c in candidates
                if c["capability"]["source_id"] in ids
            )
            assert (selected_cost, len(ids)) == min(
                (cost, count) for cost, count, _ in feasible
            )
            assert any(candidate_ids == ids for _, _, candidate_ids in feasible)
