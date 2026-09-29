"""Citations resolve to Science papers by DOI and to standards by designation only (MT10, #2088)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.document_store import DocumentStore
from src.kb.materials_citations import CitationResolver
from src.kb.materials_queries import MaterialsQueries
from src.kb.materials_store import MaterialsError, record_key
from src.kb.standards import StandardsStore
from tests.unit.materials import harness as h

WEBBOOK = record_key("nist-webbook", "C9990001")


def seed_paper(conn, doi, title="A fictional evaluated table"):
    from services.ingest.common.document_model import Document

    document = Document(
        document_id="spdoc:paper:" + doi.replace("/", "-"),
        source_type="paper",
        source_id="crossref",
        language="en",
        ingested_at=1,
        url=f"https://doi.org/{doi}",
        title=title,
        content="fictional abstract",
        authors=["A. Fictional"],
        metadata={"doi": doi},
    )
    outcome = DocumentStore(conn).upsert([document.to_dict()])
    assert not outcome.invalid, outcome.dead_letter
    return document.document_id


def seed_standard(conn, reference, native_id="990001"):
    StandardsStore(conn)
    conn.execute(
        "INSERT INTO standard_revisions VALUES (?,?,?,?,?,?,?,?)",
        [
            f"standard-revision:{native_id}",
            "global",
            "iso-open-data",
            native_id,
            reference,
            json.dumps({"reference": reference}),
            "r",
            1,
        ],
    )
    conn.execute(
        "INSERT INTO standard_current VALUES (?,?,?,?)",
        ["global", "iso-open-data", native_id, f"standard-revision:{native_id}"],
    )


@pytest.fixture()
def env():
    return h.Env().load()


def citations(dossier, prop):
    (group,) = [p for p in dossier["properties"] if p["property"]["property"] == prop]
    return [c for value in group["values"] for c in value["citations"]["value_level"]]


def test_doi_links_to_the_science_record_and_unknown_dois_stay_strings(env):
    document_id = seed_paper(env.conn, "10.99999/fict.webbook.001")
    dossier = MaterialsQueries(env.conn).properties(h.NS, WEBBOOK, scopes=h.SCOPES)
    linked = [
        c
        for c in citations(dossier, "standard_enthalpy_of_formation")
        if c["resolution"]["status"] == "linked"
    ]
    assert linked and all(
        c["resolution"]["document_id"] == document_id and c["resolution"]["revision_id"]
        for c in linked
    )
    measured = [
        c
        for c in citations(dossier, "standard_enthalpy_of_formation")
        if c.get("doi") == "10.99999/fict.webbook.002"
    ]
    assert measured[0]["resolution"] == {
        "kind": "paper",
        "status": "unresolved",
        "reason": "no Science literature record with this DOI",
    }
    (fusion,) = citations(dossier, "fusion_temperature")
    assert fusion["resolution"]["kind"] == "reference-string" and "doi" not in fusion


def test_a_similar_title_is_never_a_link(env):
    seed_paper(
        env.conn, "10.99999/other", title="Fictional, A., Evaluated fixture tables"
    )
    dossier = MaterialsQueries(env.conn).properties(h.NS, WEBBOOK, scopes=h.SCOPES)
    assert not [
        c
        for c in citations(dossier, "fusion_temperature")
        if c["resolution"]["status"] == "linked"
    ]


def test_standards_link_by_designation_only_with_the_scope_checked_at_call_time(env):
    seed_standard(env.conn, "ASTM E1269")
    queries = MaterialsQueries(env.conn)
    without = citations(
        queries.properties(h.NS, WEBBOOK, scopes=h.SCOPES), "heat_capacity_cp"
    )
    assert {c["resolution"]["reason"] for c in without if c.get("standard")} == {
        "no standards namespace was requested"
    }
    with pytest.raises(MaterialsError) as denied:
        queries.properties(h.NS, WEBBOOK, scopes=h.SCOPES, standards_namespace="global")
    assert denied.value.code == "unauthorized"
    granted = h.SCOPES | {"knowledge:standards:read", "namespace:global:read"}
    linked = citations(
        queries.properties(h.NS, WEBBOOK, scopes=granted, standards_namespace="global"),
        "heat_capacity_cp",
    )
    standards = [c for c in linked if c.get("standard")]
    assert standards and all(
        c["resolution"]
        == {
            "kind": "standard",
            "status": "linked",
            "basis": "exact reference",
            "namespace": "global",
            "technical_object_id": "standard:iso:990001",
        }
        for c in standards
    )
    assert (
        CitationResolver(
            env.conn, standards_namespace="global", scopes=granted
        ).resolve({"level": "value", "text": "x", "standard": "DIN 99999"})[
            "resolution"
        ]["status"]
        == "unresolved"
    )


def test_dataset_level_citations_are_distinguished_from_value_level(env):
    dossier = MaterialsQueries(env.conn).properties(
        h.NS, record_key("materials-project", "mp-990001"), scopes=h.SCOPES
    )
    (dataset,) = dossier["datasets"]
    assert (
        dataset["citations"][0]["doi"] == "10.1063/1.4812323"
        and dataset["citations"][0]["level"] == "dataset"
    )
    assert all(
        not v["citations"]["value_level"]
        for p in dossier["properties"]
        for v in p["values"]
    )
    assert isinstance(dossier["n"], int) and dossier["n"] > 0
