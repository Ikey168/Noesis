"""Federal statutes on the Legal work/expression model (#2105, FL02-FL05): identity, history, idempotency, schema."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from src.kb.legal import LegalError, LegalStore
from src.kb.legal_federal import FederalStatutes
from src.kb.schema_registry import READ_SCOPE as SCHEMA_READ
from src.kb.schema_registry import SchemaRegistry
from tests.unit import federal_statutes_harness as h


def versions(conn):
    store = FederalStatutes(LegalStore(conn))
    work = store.statute_work(h.NS, "MPHG")
    return work, store.versions(h.NS, work["work_id"])


def test_statute_is_one_work_with_observed_and_source_stated_versions():
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    h.apply(conn, "ris", h.ris_pages(), at="2030-04-05")
    work, items = versions(conn)
    assert work["work_kind"] == "statute" and work["native_id"] == "statute:mphg"
    assert work["identifiers"]["jurabk"] == "MPHG" and work["identifiers"][
        "eli_work"
    ] == ["eli/bund/bgbl-1/2029/101"]
    assert work["identifiers"]["gii_doknr"] == ["BJNR999010029"]
    assert sorted(v["validity_basis"] for v in items) == [
        "observed",
        "source_stated",
        "source_stated",
    ]
    observed = next(v for v in items if v["validity_basis"] == "observed")
    assert observed["validity_from"] is None and observed["observed_on"] == [
        "2030-03-01"
    ]
    assert (
        observed["stand"][0]["comment"].startswith("Zuletzt geändert")
        and observed["source_id"]
    )
    assert all(v["text_sha256"] and v["native_id"] for v in items)
    # Round trip: the stored record keeps the source's native identifiers unchanged.
    row = conn.execute(
        "SELECT record_json FROM legal_versions WHERE version_id=?",
        [observed["version_id"]],
    ).fetchone()
    assert json.loads(row[0])["fields"]["doknr"] == "BJNR999010029"


def test_replays_add_nothing_changes_append_and_earlier_versions_are_immutable():
    conn = h.connection()
    first = h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    passages_before = conn.execute(
        "SELECT count(*), string_agg(text, '|' ORDER BY ordinal) FROM legal_passages"
    ).fetchone()
    again = h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    assert (
        first["statute_versions"] == 1
        and again["statute_versions"] == 0
        and again["statute_observations"] == 0
    )
    unchanged = h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-20")
    assert unchanged["unchanged"] == 1 and unchanged["statute_versions"] == 0
    changed = h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    assert changed["statute_versions"] == 1
    assert (
        conn.execute(
            "SELECT count(*), string_agg(text, '|' ORDER BY ordinal) FROM legal_passages WHERE "
            "version_id IN (SELECT version_id FROM legal_statute_versions WHERE first_observed_at_ms=?)",
            [h.ms("2030-03-01")],
        ).fetchone()
        == passages_before
    )
    # A return to the earlier text is a new version, never a reactivation of the first one.
    reverted = h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-08-01")
    assert reverted["statute_versions"] == 1
    _, items = versions(conn)
    assert [
        v["observed_on"] for v in sorted(items, key=lambda v: v["first_observed_at_ms"])
    ] == [["2030-03-01", "2030-03-20"], ["2030-06-01"], ["2030-08-01"]]


def test_late_arriving_earlier_observations_keep_history_without_false_changes():
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    # An older fetch of the same later text arrives late: an earlier sighting of that version.
    late_same = h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-05-20")
    assert late_same["statute_versions"] == 0 and late_same["statute_observations"] == 1
    # An older fetch with different text arrives late: its own version, dated before the later one.
    late_other = h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    assert late_other["statute_versions"] == 1
    _, items = versions(conn)
    ordered = sorted(items, key=lambda v: min(v["sightings_ms"]))
    assert [v["observed_on"] for v in ordered] == [
        ["2030-03-01"],
        ["2030-05-20", "2030-06-01"],
    ]


def expression(pages, day="2030-04-01"):
    return next(
        m["item"]["workExample"]
        for m in pages[0]["body"]["member"]
        if day in m["item"]["workExample"]["legislationIdentifier"]
    )


def test_source_stated_corrections_late_history_and_replays():
    conn = h.connection()
    h.apply(conn, "ris", h.ris_pages(("2030-04-01",)), at="2030-04-05")
    assert (
        h.apply(conn, "ris", h.ris_pages(("2030-04-01",)), at="2030-04-06")["unchanged"]
        == 1
    )
    corrected = h.ris_pages(("2030-04-01",))
    expression(corrected).update(
        {"temporalCoverage": "2030-04-01/2030-11-30", "dateModified": "2030-05-01"}
    )
    result = h.apply(conn, "ris", corrected, at="2030-05-02")
    assert result["corrections"] == 1
    _, items = versions(conn)
    current = [v for v in items if v["current"]]
    assert (
        len(current) == 1
        and current[0]["validity_to"] == "2030-11-30"
        and current[0]["corrects_version_id"]
    )
    # A late-arriving older statement (by the source's own date) is history, not a correction.
    older = h.ris_pages(("2030-04-01",))
    expression(older).update(
        {"temporalCoverage": "2030-04-01/2030-10-31", "dateModified": "2030-03-01"}
    )
    late = h.apply(conn, "ris", older, at="2030-05-03")
    assert late["corrections"] == 0 and late["statute_versions"] == 1
    _, items = versions(conn)
    assert [v["validity_to"] for v in items if v["current"]] == ["2030-11-30"]
    # A reversion to the first statement (newer source date) is a new correction, not a reactivation.
    reverted = h.ris_pages(("2030-04-01",))
    expression(reverted)["dateModified"] = "2030-06-01"
    assert h.apply(conn, "ris", reverted, at="2030-06-02")["corrections"] == 1
    _, items = versions(conn)
    assert [v["validity_to"] for v in items if v["current"]] == ["2030-12-31"]
    assert len([v for v in items if v["validity_to"] == "2030-12-31"]) == 2


def test_amendment_act_is_a_work_keyed_by_its_bgbl_citation_with_instructions():
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    counts = h.apply(conn, "bgbl", h.bgbl_pages(), at="2030-03-15")
    assert counts["amendments"] == 5 and counts["works"] == 1
    assert h.apply(conn, "bgbl", h.bgbl_pages(), at="2030-03-16")["versions"] == 0
    row = conn.execute(
        "SELECT native_id, work_kind, identifiers_json FROM legal_works WHERE work_kind='amendment_act'"
    ).fetchone()
    assert row[:2] == ("bgbl-1/2030/nr-45", "amendment_act")
    assert json.loads(row[2])["bgbl_citation"] == "BGBl. 2030 I Nr. 45"
    facts = conn.execute(
        "SELECT fact_kind, value FROM legal_facts ORDER BY fact_kind"
    ).fetchall()
    assert facts == [("enactment", "2030-03-12"), ("publication", "2030-03-14")]
    targets = conn.execute(
        "SELECT provision, status, target_work_id IS NOT NULL FROM legal_amendments ORDER BY ordinal"
    ).fetchall()
    assert targets == [
        (None, "ambiguous", True),
        ("§5", "resolved", True),
        ("§5/abs2", "resolved", True),
        ("§8a", "resolved", True),
        (None, "statute_not_in_set", False),
    ]


def test_invalid_statute_records_are_refused():
    conn = h.connection()
    records, _ = h.drain("gii", h.gii_pages("2030-03-01"))
    broken = copy.deepcopy(records)
    broken[0]["legal_record"]["fields"]["validity_basis"] = "source_stated"
    with pytest.raises(LegalError) as caught:
        LegalStore(conn).project(h.NS, broken, run_id="r", source_id="s")
    assert caught.value.code == "invalid_record"


def test_existing_cellar_court_and_berlin_records_are_unaffected_and_the_schema_is_backward_compatible():
    from tests.unit.domains.test_legal_pack import install, run

    conn = h.connection()
    value, runtime = install(conn)
    receipt = run(runtime, value, "fixture-1")
    assert receipt["status"] == "complete"
    kinds = dict(
        conn.execute(
            "SELECT work_kind, count(*) FROM legal_works GROUP BY work_kind"
        ).fetchall()
    )
    assert "statute" not in kinds and "amendment_act" not in kinds
    assert (
        conn.execute("SELECT count(*) FROM legal_statute_versions").fetchone()[0] == 0
    )
    registry = SchemaRegistry(conn, initialize=False)
    old = registry.resolve("schema", "legal-record", "1.0.0", scopes={SCHEMA_READ})
    new = registry.resolve("schema", "legal-record", "^1.0.0", scopes={SCHEMA_READ})
    assert new["semantic_version"] == "1.1.0"
    assert (
        SchemaRegistry.compare_content(old["content"], new["content"])["classification"]
        != "breaking"
    )
    rows = conn.execute("SELECT record_json FROM legal_versions").fetchall()
    assert rows
    old_validator, new_validator = (
        jsonschema.Draft7Validator(old["content"]),
        jsonschema.Draft7Validator(new["content"]),
    )
    for (record_json,) in rows:
        record = json.loads(record_json)
        page = {
            "id": "x",
            "title": record["title"],
            "language": record["language"],
            "url": "https://x",
            "legal_record": {**record, "sections": []},
            "legal_page": {},
        }
        assert not list(old_validator.iter_errors(page)) and not list(
            new_validator.iter_errors(page)
        )
    federal, _ = h.drain("gii", h.gii_pages("2030-03-01"))
    assert not list(new_validator.iter_errors(federal[0])) and list(
        old_validator.iter_errors(federal[0])
    )
