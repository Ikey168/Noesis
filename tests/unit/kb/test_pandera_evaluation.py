import base64
import io

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

pytest.importorskip("pandera")

from src.kb.dataset_intelligence import (
    DatasetIntelligenceError,
    DatasetIntelligenceStore,
)
from src.kb.pandera_evaluation import validate_release
from tests.unit.kb.test_dataset_intelligence import _dataset, _release

AUTH = {"principal_id": "operator", "scopes": {"operator"}}
CONSTRAINTS = {
    "version": "berlin-v1",
    "ranges": {"value": {"min": 0, "max": 100}},
    "unique": ["geo", "period"],
    "cross_fields": [],
}


def setup_store():
    conn = duckdb.connect()
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    dataset = _dataset(store)
    release = _release(store, dataset, "berlin-2025")
    return conn, store, release, dataset["tables"][0]["table_id"]


def test_german_csv_reports_coercion_constraints_and_replay():
    conn, store, release, table_id = setup_store()
    try:
        csv = "geo;period;value\nDE;2024;12,5\nDE;2024;120\nDE;2025;-1\n"
        result = validate_release(
            store,
            "economic",
            release["release_id"],
            table_id,
            content=csv,
            delimiter=";",
            coercion="de-DE",
            constraints=CONSTRAINTS,
            **AUTH,
        )
        assert result["status"] == "rejected"
        assert result["rows"] == 3 and result["coercion_count"] >= 3
        assert result["error_count"] >= 3
        assert {entry["phase"] for entry in result["errors"]} == {"validation"}
        assert any("range" in entry["check"] for entry in result["errors"])
        assert any("unique" in entry["check"] for entry in result["errors"])
        assert result["errors"][0]["row_index"] is not None
        assert result["release_provenance"] == release["provenance"]
        replay = validate_release(
            store,
            "economic",
            release["release_id"],
            table_id,
            content=csv,
            delimiter=";",
            coercion="de-DE",
            constraints=CONSTRAINTS,
            **AUTH,
        )
        assert replay == result
        assert conn.execute("SELECT count(*) FROM pandera_validations").fetchone() == (
            1,
        )
    finally:
        conn.close()


def test_valid_parquet_and_cross_field_dates_preserve_original_bytes():
    conn, store, release, table_id = setup_store()
    try:
        frame = pd.DataFrame(
            [
                {"geo": "DE", "period": "2024", "value": 12.5},
                {"geo": "BE", "period": "2024", "value": 8.0},
            ]
        )
        output = io.BytesIO()
        pq.write_table(pa.Table.from_pandas(frame, preserve_index=False), output)
        encoded = base64.b64encode(output.getvalue()).decode()
        result = validate_release(
            store,
            "economic",
            release["release_id"],
            table_id,
            content=encoded,
            format="parquet",
            constraints={**CONSTRAINTS, "unique": []},
            **AUTH,
        )
        assert result["status"] == "validated"
        assert result["rows"] == 2
        assert result["original_bytes"] == len(output.getvalue())
        assert result["original_payload_hash"]
        assert result["production_ingestion_changed"] is False
        assert result["config"]["release_hash"] == release["release_hash"]
    finally:
        conn.close()


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"coercion": "none"}, "rejected"),
        ({"max_rows": 1}, "row_limit"),
        ({"content": "geo,period,value,extra\nDE,2024,1,x\n"}, "unknown_column"),
        (
            {
                "constraints": {
                    "version": "x",
                    "ranges": {"value": {"min": "bad"}},
                    "unique": [],
                    "cross_fields": [],
                }
            },
            "invalid_checks",
        ),
    ],
)
def test_limits_schema_drift_and_deterministic_failures(kwargs, code):
    conn, store, release, table_id = setup_store()
    try:
        values = {
            "content": "geo,period,value\nDE,2024,1\n",
            "format": "csv",
            "constraints": CONSTRAINTS,
            **AUTH,
        }
        values.update(kwargs)
        if code == "row_limit":
            values["content"] = "geo,period,value\nDE,2024,1\nDE,2025,2\n"
        if code == "rejected":
            values["content"] = "geo,period,value\nDE,2024,not-a-number\n"
        if code == "unknown_column":
            values["constraints"] = {**CONSTRAINTS, "ranges": {}}
        if code == "invalid_checks":
            values["content"] = "geo,period,value\nDE,2024,1\n"
        result = validate_release(
            store, "economic", release["release_id"], table_id, **values
        )
        if code == "rejected":
            assert result["status"] == "rejected"
        else:
            assert code == "unknown_column" and any(
                e["check"] == code for e in result["errors"]
            )
    except DatasetIntelligenceError as failure:
        assert failure.code == code
    finally:
        conn.close()


def test_authorization_and_input_bounds_are_explicit():
    conn, store, release, table_id = setup_store()
    try:
        values = {
            "content": "geo,period,value\nDE,2024,1\n",
            "format": "csv",
            "constraints": CONSTRAINTS,
        }
        with pytest.raises(DatasetIntelligenceError, match="namespace writer"):
            validate_release(
                store,
                "economic",
                release["release_id"],
                table_id,
                **values,
                principal_id="reader",
                scopes={"knowledge:dataset:read"},
            )
        with pytest.raises(DatasetIntelligenceError, match="2 MB"):
            validate_release(
                store,
                "economic",
                release["release_id"],
                table_id,
                content="x" * 2_000_001,
                format="csv",
            constraints={**CONSTRAINTS, "unique": []},
                **AUTH,
            )
    finally:
        conn.close()


def test_date_missing_and_cross_field_checks_keep_locators():
    conn = duckdb.connect()
    try:
        store = DatasetIntelligenceStore(conn, now=lambda: 1000)
        dataset = _dataset(
            store,
            native="berlin-bounds",
            tables=[
                {
                    "name": "bounds",
                    "identity": "bounds",
                    "primary_key": ["geo", "period"],
                    "columns": [
                        {"name": "geo", "type": "string", "nullable": False},
                        {"name": "period", "type": "date", "nullable": False},
                        {"name": "lower", "type": "number", "nullable": True},
                        {"name": "upper", "type": "number", "nullable": True},
                    ],
                }
            ],
        )
        release = _release(store, dataset, "bounds-2025")
        table_id = dataset["tables"][0]["table_id"]
        result = validate_release(
            store,
            "economic",
            release["release_id"],
            table_id,
            content="geo,period,lower,upper\nDE,2025-01-01,10,20\nBE,2025-01-02,,5\nDE,2025-01-03,30,20\n",
            constraints={
                "version": "bounds-v1",
                "ranges": {},
                "unique": [],
                "cross_fields": [{"left": "lower", "op": "le", "right": "upper"}],
            },
            **AUTH,
        )
        assert result["status"] == "rejected"
        assert any("cross" in item["check"] for item in result["errors"])
        assert result["coercion_count"] >= 6
        assert all("row_index" in item and "column" in item for item in result["errors"])
    finally:
        conn.close()
