"""BD02 (#2507): record contracts, schema registration and round trips."""

import json

import duckdb
import pytest

from src.ingestion.biodiversity_sources import replay_native_fixture
from src.kb import biodiversity_records as br
from tests.unit.biodiversity import harness

URL = "https://api.gbif.org/v1/occurrence/search"


def _occurrence(**over):
    published = {"gbif_id": "1", "dataset_key": "d1", "coordinates": {"latitude": 52.5, "longitude": 13.4},
                 "coordinate_uncertainty_m": 30.0,
                 "generalisation": br.generalisation(coordinates_published=True), **over}
    return br.statement("occurrence", "gbif", "1", subject_name="Passer domesticus",
                        source={"url": URL}, as_published=published)


def test_every_record_type_round_trips_through_its_constructor():
    records = []
    for source in harness.manifest()["sources"]:
        fixture = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())
        records += [r["biodiversity_record"] for r in replay_native_fixture(source, fixture)]
    assert {r["record_type"] for r in records} == set(br.RECORD_TYPES)
    for record in records:
        assert br.validate_statement(record) == record


def test_schema_registers_and_validates_every_fixture_statement():
    from src.kb.schema_registry import SchemaRegistry

    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = br.register_schemas(conn, principal_id="bio-service", scopes=scopes)
    assert [m["name"] for m in modules] == ["noesis-biodiversity-record"]
    assert br.register_schemas(conn, principal_id="bio-service", scopes=scopes)[0]["idempotent_replay"]
    registry = SchemaRegistry(conn)
    reference = {"kind": "schema", "name": "noesis-biodiversity-record", "version": "1.0.0"}
    for source in harness.manifest()["sources"]:
        fixture = json.loads((harness.ROOT / source["fixture"]["path"]).read_text())
        for item in replay_native_fixture(source, fixture):
            result = registry.validate_instance(reference, item["biodiversity_record"], scopes=scopes)
            assert result["valid"], result["errors"]
    broken = _occurrence()
    broken["as_published"]["coordinate_uncertainty_m"] = -1
    assert not registry.validate_instance(reference, broken, scopes=scopes)["valid"]


def test_unknowns_are_listed_and_forbidden_fields_are_rejected():
    record = _occurrence()
    assert "event_date" in record["unknowns"] and record["as_published"]["event_date"] is None
    with pytest.raises(br.BiodiversityError) as error:
        _occurrence(abundance=3)
    assert error.value.code in {"invalid_biodiversity_record", "forbidden_field"}
    with pytest.raises(br.BiodiversityError):
        br.statement("occurrence", "gbif", "1", subject_name=None, source={"url": URL},
                     as_published={"gbif_id": "1", "dataset_key": "d", "coordinates": {"latitude": 1.0,
                                                                                      "longitude": 1.0},
                                   "generalisation": {"generalised": False, "coordinates_withheld": True}})


def test_assessments_keep_scope_latest_designation_and_licence_tier():
    base = {"assessment_id": "1", "taxon_id": "2", "category": "NT", "year_published": "2021", "latest": True,
            "scope": {"kind": "global", "label": "Global"}, "licence_tier": "reference-only"}
    record = br.statement("conservation_assessment", "iucn", "1", subject_name="Lutra lutra",
                          source={"url": "https://www.iucnredlist.org/species/2/1"}, as_published=base)
    assert record["as_published"]["latest"] is True and "criteria" in record["unknowns"]
    for bad in ({"category": "Near Threatened"}, {"latest": "yes"}, {"licence_tier": "full"},
                {"scope": {"kind": "national", "label": "DE"}}):
        with pytest.raises(br.BiodiversityError):
            br.statement("conservation_assessment", "iucn", "1", subject_name=None,
                         source={"url": "https://www.iucnredlist.org/x"}, as_published={**base, **bad})


def test_taxa_keep_checklist_version_and_synonyms_keep_their_accepted_name():
    base = {"native_id": "X", "scientific_name": "Corvus cornix", "rank": "species", "status": "synonym",
            "status_class": br.status_class("synonym"),
            "checklist": {"dataset_key": "310002", "version": "2026-08-12", "released": "2026-08-12"}}
    with pytest.raises(br.BiodiversityError):
        br.statement("taxon", "col", "X", subject_name=None, source={"url": "https://api.checklistbank.org/x"},
                     as_published=base)
    ok = br.statement("taxon", "col", "X", subject_name=None, source={"url": "https://api.checklistbank.org/x"},
                      as_published={**base, "accepted": {"id": "Y", "scientific_name": "Corvus corone"}})
    assert ok["subject"] == {"key": "col:X", "kind": "taxon", "name": None}


def test_generalisation_text_states_a_precision_that_is_parsed_never_inferred():
    assert br.parse_generalisation_precision("Coordinates generalised to 10 km grid") == 10_000.0
    assert br.parse_generalisation_precision("rounded to 0.1 degree") == pytest.approx(11_132.0)
    assert br.parse_generalisation_precision("generalised") is None
    flags = br.generalisation(information_withheld="Coordinates withheld", coordinates_published=False)
    assert flags["coordinates_withheld"] and not flags["generalised"]
    assert br.licence("http://creativecommons.org/licenses/by-nc/4.0/legalcode")["commercial_use"] is False
    assert br.licence("something else")["id"] is None
