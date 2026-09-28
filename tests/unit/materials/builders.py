"""Small builders for fictional material entries used by the store, identity and comparison tests."""

from __future__ import annotations

from src.kb import materials_records as mr

NS = "materials"
READ = "knowledge:materials:read"
WRITE = "knowledge:materials:write"
SCOPES = {READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write"}


def computed(functional, *, code="VASP", mixing=None):
    return mr.method_provenance(
        "computed",
        method="DFT (PAW)",
        functional=functional,
        mixing_scheme=mixing,
        code=code,
    )


def value(
    prop,
    number,
    unit,
    *,
    method=None,
    temperature=None,
    pressure=None,
    phase=None,
    references=None,
    uncertainty=None,
    status="active",
):
    return mr.property_value(
        prop,
        number,
        unit,
        conditions=mr.condition_set(
            temperature=temperature, pressure=pressure, phase=phase
        ),
        method=method or computed("GGA+U"),
        references=references,
        uncertainty=uncertainty,
        status=status,
    )


def entry(
    provider,
    native_id,
    values,
    *,
    formula="TiO2",
    release="2099.1.0",
    released_on="2099-01-15",
    basis="provider-stated",
    space_group=(136, "P4_2/mnm"),
    volume="31.2",
    nsites=6,
    method_class=None,
    identifiers=None,
    measurement=None,
    status="active",
    source_updated_at=None,
    references=None,
):
    structure = None
    if space_group is not None:
        structure = mr.structure(
            f"{provider}:{native_id}",
            method_class=method_class
            or ("measured" if provider == "cod" else "computed"),
            space_group={"number": space_group[0], "symbol": space_group[1]},
            lattice={
                "a": "4.59",
                "b": "4.59",
                "c": "2.96",
                "alpha": "90",
                "beta": "90",
                "gamma": "90",
            },
            nsites=nsites,
            volume=None if volume is None else {"value": volume, "unit": "Å^3"},
            measurement=measurement,
        )
    return mr.entry(
        provider,
        native_id,
        title=f"{formula} ({provider} {native_id}, fictional)",
        material_record=mr.material(formula),
        values=values,
        release_record=mr.release(release, basis=basis, released_on=released_on),
        retrieved_at="2099-02-01T00:00:00Z",
        locator=f"https://fixture.materials.example/{provider}/{native_id}",
        structure_record=structure,
        identifiers=identifiers,
        status=status,
        source_updated_at=source_updated_at,
        references=references,
    )
