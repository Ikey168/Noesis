"""Condition-aware comparison and bounded property-range search (MT09, #2087)."""

from __future__ import annotations

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.materials_comparison import MaterialsComparison, not_comparable_reasons
from src.kb.materials_identity import MaterialsIdentity
from src.kb.materials_store import MaterialsError, MaterialsStore, record_key
from tests.unit.materials import builders as b
from tests.unit.materials import harness as h

SCOPES = h.SCOPES | {
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
}
MP = record_key("materials-project", "mp-990001")


@pytest.fixture(scope="module")
def reviewed():
    env = h.Env().load()
    identity = MaterialsIdentity(env.conn)
    for candidate in identity.propose(h.NS, principal_id="alice", scopes=SCOPES)[
        "candidates"
    ]:
        if MP in (candidate["left_key"], candidate["right_key"]):
            identity.review(
                h.NS,
                candidate["candidate_id"],
                "accept",
                "stated cross-reference or structure",
                principal_id="bob",
                scopes=SCOPES,
            )
    return env


def compare(env, material, prop):
    return MaterialsComparison(env.conn).compare(h.NS, material, prop, scopes=h.SCOPES)


def test_reviewed_phase_aligns_computed_values_and_labels_functionals(reviewed):
    result = compare(reviewed, MP, "band_gap")
    assert result["scope"]["level"] == "phase" and result["n"] == 4
    (group,) = result["aligned"]
    assert group["method_class"] == "computed" and group["mixed_functionals"] is True
    assert group["functionals"] == ["GGA/GGA+U", "OptB88vdW", "PBE", "TBmBJ"]
    assert "different functionals" in group["functional_note"]
    assert "unstated for every value" in group["conditions_note"]
    assert {v["provider"] for v in group["values"]} == {
        "materials-project",
        "jarvis-dft",
        "oqmd",
    }
    keys = set(result) | {k for g in result["aligned"] for k in g}
    assert not keys & {
        "consensus",
        "average",
        "mean",
        "best_estimate",
        "recommended_value",
    }
    assert isinstance(result["n"], int) and result["method"] and result["assumptions"]
    verified = verify_bundle(result["evidence_bundle"])
    assert verified.valid, verified.errors


def test_composition_request_never_aligns_unmatched_records_or_polymorphs():
    env = h.Env().load()
    result = compare(env, "TiO2", "formation_energy_per_atom")
    assert result["scope"]["level"] == "composition"
    for group in result["aligned"]:
        assert (
            len({v["record_key"] for v in group["values"]}) == 1
        )  # only values of one record align
    reasons = {
        r
        for item in result["not_comparable"]
        for other in item["reasons"]
        for r in other["reasons"]
    }
    assert "no reviewed phase-level match between the records" in reasons


def test_measured_and_evaluated_values_and_different_temperatures_are_not_comparable(
    reviewed,
):
    enthalpy = compare(
        reviewed,
        record_key("nist-webbook", "C9990001"),
        "standard_enthalpy_of_formation",
    )
    assert enthalpy["aligned"] == []
    reasons = [
        r
        for item in enthalpy["not_comparable"]
        for other in item["reasons"]
        for r in other["reasons"]
    ]
    assert (
        "method class differs (evaluated vs measured)" in reasons
        or "method class differs (measured vs evaluated)" in reasons
    )
    heat = compare(reviewed, record_key("nist-webbook", "C9990001"), "heat_capacity_cp")
    assert heat["aligned"] == []
    assert any(
        r.startswith("temperature differs (298.15 K vs 500 K)")
        or r.startswith("temperature differs (500 K")
        for item in heat["not_comparable"]
        for other in item["reasons"]
        for r in other["reasons"]
    )


def test_values_that_must_be_rejected_as_not_comparable():
    store = MaterialsStore(duckdb.connect(":memory:"))
    measured = b.value(
        "fusion_temperature", "2116", "K", method=b.computed("PBE"), temperature=None
    )
    records = [
        b.entry(
            "oqmd",
            "1",
            [
                b.value(
                    "bulk_modulus",
                    "200",
                    "GPa",
                    method=b.computed("PBE"),
                    temperature={"value": "300", "unit": "K"},
                )
            ],
        ),
        b.entry(
            "oqmd",
            "2",
            [b.value("bulk_modulus", "201", "GPa", method=b.computed("PBE"))],
        ),
        b.entry(
            "oqmd",
            "3",
            [b.value("bulk_modulus", "202", "kbar-ish", method=b.computed("PBE"))],
        ),
    ]
    del measured
    store.apply(
        b.NS, records, run_id="r", principal_id="p", scopes=b.SCOPES, observed_at_ms=1
    )
    values = {
        v["native_id"]: {**v, "phase_group": "same"} for v in store.current_values(b.NS)
    }
    assert "temperature stated on one side only" in not_comparable_reasons(
        values["1"], values["2"]
    )
    assert any(
        "unit unknown" in r for r in not_comparable_reasons(values["2"], values["3"])
    )


def test_search_is_bounded_exact_and_cites_values(reviewed):
    search = MaterialsComparison(reviewed.conn).search(
        h.NS,
        [{"property": "band_gap", "min": "1700", "max": "1900", "unit": "meV"}],
        method_class="computed",
        scopes=h.SCOPES,
    )
    assert {(hit["provider"], hit["native_id"]) for hit in search["hits"]} == {
        ("materials-project", "mp-990001"),
        ("jarvis-dft", "JVASP-990101"),
        ("oqmd", "99000301"),
    }
    assert all(
        v["normalized"]["normalized_unit"] == "eV"
        for hit in search["hits"]
        for v in hit["values"]
    )
    assert search["n"] == 3 and "absence is not evidence" in search["assumptions"][0]
    hot = MaterialsComparison(reviewed.conn).search(
        h.NS,
        [{"property": "heat_capacity_cp", "min": "60", "unit": "J/(mol*K)"}],
        conditions={"temperature": {"value": "226.85", "unit": "°C"}},
        scopes=h.SCOPES,
    )
    (hit,) = hot["hits"]
    assert hit["provider"] == "nist-webbook" and [
        v["value"] for v in hit["values"]
    ] == ["63.4"]
    none = MaterialsComparison(reviewed.conn).search(
        h.NS,
        [{"property": "band_gap", "min": "1.7"}],
        method_class="measured",
        scopes=h.SCOPES,
    )
    assert none["hits"] == []
    with pytest.raises(MaterialsError):
        MaterialsComparison(reviewed.conn).search(
            h.NS, [{"property": "band_gap", "min": "1", "unit": "GPa"}], scopes=h.SCOPES
        )


def test_not_ready_before_any_source_ran():
    with pytest.raises(MaterialsError) as error:
        MaterialsComparison(duckdb.connect(":memory:")).compare(
            h.NS, "TiO2", "band_gap", scopes=h.SCOPES
        )
    assert error.value.code == "not_ready"
