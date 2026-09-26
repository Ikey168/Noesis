"""Acceptance-matrix gate (C08.7): every matrix row maps to exactly one named test.

The architecture doc's matrix table must list each row with this test ID, and
each ID must name a test that exists. The tests themselves run with the rest
of ``tests/unit/composition`` in CI; this module guards the mapping.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs/architecture/pack-workflow-composition.md"
FIRST = "tests/unit/composition/test_first_composition.py"

MATRIX = {
    "Two packs consume Geospatial": f"{FIRST}::test_one_geospatial_binding_consumed_by_both_roots",
    "OSINT disabled, Research active": f"{FIRST}::test_disabling_osint_keeps_research_spatial_operations_and_evidence",
    "Required provider missing or ambiguous":
        f"{FIRST}::test_required_provider_missing_or_ambiguous_blocks_before_execution",
    "Optional acquisition unavailable":
        f"{FIRST}::test_optional_acquisition_unavailable_runs_local_analysis_with_missing_source_coverage",
    "Contract/ontology conflict": f"{FIRST}::test_contract_or_ontology_conflict_rejects_the_composition",
    "Upgrade changes a pinned provider/source":
        f"{FIRST}::test_source_revision_preview_blocks_incompatible_and_keeps_historical_pins",
    "Crash during activation": f"{FIRST}::test_crash_during_activation_keeps_the_previous_generation",
    "Crash after a workflow mutation":
        f"{FIRST}::test_crash_after_a_workflow_mutation_reconciles_and_resumes_under_the_original_digest",
    "Access revoked after preflight": f"{FIRST}::test_access_revoked_after_preflight_is_rechecked_everywhere",
    "Several consumers acquire from one account":
        "tests/unit/composition/test_sources.py::"
        "test_aggregate_account_limit_holds_across_consumers_and_is_a_distinct_blocker",
    "Legacy manifest/session/report":
        f"{FIRST}::test_legacy_manifests_keep_identity_and_public_behavior_through_the_adapter",
    "Fixture-only public recipe run":
        "tests/unit/composition/test_workflows.py::"
        "test_fixture_runs_cannot_claim_dispatch_and_dispatch_mode_needs_the_dispatcher",
}


@pytest.mark.parametrize(("row", "test_id"), sorted(MATRIX.items()))
def test_every_matrix_row_names_an_existing_test(row, test_id):
    path, name = test_id.split("::")
    module = importlib.import_module(path.removesuffix(".py").replace("/", "."))
    assert callable(getattr(module, name, None)), test_id


def test_the_architecture_doc_maps_every_row_to_its_test():
    text = DOC.read_text()
    section = text[text.index("## Acceptance matrix"):text.index("## Effect on the existing expansion backlog")]
    rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| ---")][1:]
    assert {row.split(" | ")[0].lstrip("| ").strip() for row in rows} == set(MATRIX)
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert all(cells), row  # no empty cells
        assert f"`{MATRIX[cells[0]].split('::')[1]}`" in cells[-1], row
