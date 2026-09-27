"""Poll series through the existing polls connector: fieldwork, samples, publisher terms, never a result (#1947)."""

from __future__ import annotations

import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion.connectors.dataset.poll_source import (
    PROVIDER_COLUMN_MAPS,
    parse_poll_csv,
    with_options,
)
from src.ingestion.connectors.dataset.polls import check_opinion
from src.kb.elections import ElectionError, forbidden_keys
from src.kb.elections_polls import ElectionPolls, dataset_metadata
from tests.unit import elections_harness as h

SCHEMA = json.loads(
    (h.ROOT / "contracts/schemas/jsonschema/noesis-election-record-v1.json").read_text()
)
FIRST = "poll_beispiel_institut_2099-02-21.csv"
SECOND = "poll_beispiel_institut_2099-02-28.csv"
URL = "https://www.beispiel-institut.example/sonntagsfrage.csv"


def load(conn, filename, *, redistribution="allowed", body=None):
    return ElectionPolls(conn).import_release(
        "global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url=URL,
        csv_text=body if body is not None else (h.FIXTURES / filename).read_text(),
        redistribution=redistribution,
        terms_url="https://www.beispiel-institut.example/terms",
        principal_id="alice",
        scopes=h.SCOPES,
    )


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def test_the_column_map_extends_the_existing_parser_with_fieldwork_and_publisher():
    cmap = with_options(
        PROVIDER_COLUMN_MAPS["poll-release-csv"], {"Musterunion": "Musterunion"}
    )
    (reading, _) = parse_poll_csv((h.FIXTURES / FIRST).read_text(), column_map=cmap)
    m = reading.methodology
    assert (
        m.fieldwork_start,
        m.fieldwork_end,
        m.sample_n,
        m.client,
        m.publisher,
        m.published_on,
    ) == (
        "2099-02-01",
        "2099-02-05",
        1500,
        "Musterzeitung",
        "Beispiel Institut",
        "2099-02-07",
    )
    assert m.population == "eligible voters" and m.mode == "online panel"


def test_readings_keep_methodology_cite_their_release_and_are_typed_as_polls(conn):
    result = load(conn, FIRST)
    assert (
        result["status"] == "applied"
        and result["readings"] == 6
        and not result["link_only"]
    )
    series = ElectionPolls(conn).series(
        "global", scopes=h.READ_ONLY, election_id=h.DE_ELECTION
    )
    assert (
        len(series) == 3
    )  # one per party, one publisher: never combined into one number
    union = next(s for s in series if s["option"] == "Musterunion")
    assert [r["value"] for r in union["readings"]] == [33.0, 32.0]
    reading = union["readings"][0]
    assert reading["typed_as"] == "poll" and reading["record_type"] == "poll_reading"
    assert (
        reading["source_revision"]["url"] == URL
        and len(reading["source_revision"]["file_sha256"]) == 64
    )
    assert (
        reading["fieldwork_start"],
        reading["fieldwork_end"],
        reading["sample_size"],
    ) == (
        "2099-02-01",
        "2099-02-05",
        1500,
    )
    assert not list(Draft7Validator(SCHEMA).iter_errors(reading))
    assert not list(
        Draft7Validator(SCHEMA).iter_errors(
            {k: v for k, v in union.items() if k != "readings"}
        )
    )
    assert forbidden_keys(series) == []
    # The same reading lives in the observation store as a provider='poll' series (never a result vintage).
    meta = dataset_metadata(conn, reading["dataset_series_id"])
    assert meta["provider"] == "poll" and meta["metadata"]["record_type"] == "poll"
    assert meta["metadata"]["fieldwork_start"] == "2099-02-01"
    assert not conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='election_result_vintages'"
    ).fetchone()[0]


def test_re_importing_is_idempotent_and_a_cumulative_release_adds_only_new_readings(
    conn,
):
    load(conn, FIRST)
    before = conn.execute(
        "SELECT * FROM election_poll_readings ORDER BY ALL"
    ).fetchall()
    observations = conn.execute(
        "SELECT * FROM dataset_observations ORDER BY ALL"
    ).fetchall()
    assert load(conn, FIRST)["status"] == "unchanged"
    assert (
        conn.execute("SELECT * FROM election_poll_readings ORDER BY ALL").fetchall()
        == before
    )
    second = load(conn, SECOND)
    assert (second["readings"], second["revised"]) == (3, 0)
    assert (
        len(conn.execute("SELECT * FROM dataset_observations").fetchall())
        == len(observations) + 3
    )


def test_a_corrected_figure_is_a_new_reading_revision_and_the_old_one_is_kept(conn):
    load(conn, FIRST)
    body = (
        (h.FIXTURES / FIRST)
        .read_text()
        .replace(
            "2099-02-07,1500,online panel,eligible voters,Sonntagsfrage "
            "Bundestag,31,33,21",
            "2099-02-08,1500,online panel,eligible "
            "voters,Sonntagsfrage Bundestag,31,34,21",
        )
    )
    result = load(conn, "x", body=body)
    assert result["revised"] == 1
    polls = ElectionPolls(conn)
    union = next(
        s
        for s in polls.series("global", scopes=h.READ_ONLY)
        if s["option"] == "Musterunion"
    )
    assert [r["value"] for r in union["readings"]] == [34.0, 32.0]
    history = polls.readings(
        "global", scopes=h.READ_ONLY, current_only=False, date_to="2099-02-05"
    )
    assert sorted(r["value"] for r in history if r["option"] == "Musterunion") == [
        33.0,
        34.0,
    ]


def test_series_without_confirmed_terms_are_link_only_metadata(conn):
    result = load(conn, FIRST, redistribution="unknown")
    assert result["link_only"] is True
    series = ElectionPolls(conn).series("global", scopes=h.READ_ONLY)
    reading = series[0]["readings"][0]
    assert (
        reading["value"] is None
        and reading["sample_size"] == 1500
        and "link-only" in reading["note"]
    )
    assert reading["dataset_series_id"] is None
    assert not conn.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='dataset_observations'"
    ).fetchone()[0]


def test_check_opinion_still_answers_from_the_stored_poll_series(conn):
    load(conn, FIRST)
    envelope = check_opinion(
        conn,
        "A majority support Musterunion.",
        topic=h.DE_ELECTION,
        option="Musterunion",
    )
    assert envelope["verdict"] == "contradicted" and "series_id" in envelope


def test_imports_are_validated_and_scoped(conn):
    with pytest.raises(ElectionError) as exc:
        load(conn, FIRST, redistribution="maybe")
    assert exc.value.code == "invalid_terms"
    with pytest.raises(ElectionError) as exc:
        ElectionPolls(conn).import_release(
            "global",
            publisher="Other",
            election_id=h.DE_ELECTION,
            source_url=URL,
            csv_text=(h.FIXTURES / FIRST).read_text(),
            redistribution="allowed",
            principal_id="a",
            scopes=h.SCOPES,
        )
    assert exc.value.code == "invalid_release"
    with pytest.raises(ElectionError) as exc:
        ElectionPolls(conn).import_release(
            "global",
            publisher="Beispiel Institut",
            election_id=h.DE_ELECTION,
            source_url=URL,
            csv_text=(h.FIXTURES / FIRST).read_text(),
            redistribution="allowed",
            principal_id="a",
            scopes=h.READ_ONLY,
        )
    assert exc.value.code == "unauthorized"


def test_polls_sharing_publisher_and_end_date_are_separate_stored_series(conn):
    """Another client, question or fieldwork start on the same end date is another poll (review fix)."""
    header, row = (h.FIXTURES / FIRST).read_text().splitlines()[:2]
    other_client = row.replace("Musterzeitung", "Beispielsender").replace(
        ",31,33,21", ",30,34,22"
    )
    other_start = row.replace("2099-02-01", "2099-02-03").replace(
        ",31,33,21", ",29,35,20"
    )
    load(conn, "x", body="\n".join([header, row, other_client, other_start]) + "\n")
    readings = [
        r
        for r in ElectionPolls(conn).readings("global", scopes=h.READ_ONLY)
        if r["option"] == "Musterunion"
    ]
    assert sorted(r["value"] for r in readings) == [33.0, 34.0, 35.0]
    stored = {r["dataset_series_id"] for r in readings}
    assert len(stored) == 3
    values = {
        conn.execute(
            "SELECT value FROM dataset_observations WHERE series_id=?", [series_id]
        ).fetchone()[0]
        for series_id in stored
    }
    assert values == {33.0, 34.0, 35.0}  # nothing overwritten in the observation store
