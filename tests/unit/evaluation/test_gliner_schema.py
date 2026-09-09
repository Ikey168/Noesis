"""Structured GLiNER calls through dispatch, with explicitly injected SDK output."""

import copy

import duckdb
import pytest

from src.evaluation.model_backends import GLiNERBackend
from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_jobs import dispatch
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.optional_runtime import OptionalAnalysisStore

TEXT = "Die IBB fördert SolarPLUS gemäß § 3."
SCHEMA = {
    "entities": {"authority": "German public funding authority"},
    "relations": {"funds": "Authority funds a programme"},
    "structures": {
        "funding": {
            "programme": {"dtype": "str", "description": "Funding programme"},
            "legal_references": {
                "dtype": "list",
                "description": "Explicit legal provisions",
            },
            "amount": {"dtype": "str", "description": "Explicit amount"},
        }
    },
}


def span(value):
    start = TEXT.index(value)
    return {"text": value, "start": start, "end": start + len(value), "confidence": 0.8}


class SchemaBuilder:
    def __init__(self):
        self.calls = []

    def entities(self, value):
        self.calls.append(("entities", value))
        return self

    def relations(self, value):
        self.calls.append(("relations", value))
        return self

    def structure(self, name):
        self.calls.append(("structure", name))
        return self

    def field(self, name, **kw):
        self.calls.append(("field", name, kw))
        return self


class Model:
    def __init__(self):
        self.schema = SchemaBuilder()
        self.raw = {
            "entities": {"authority": [span("IBB")]},
            "relation_extraction": {
                "funds": [{"head": span("IBB"), "tail": span("SolarPLUS")}]
            },
            "funding": [
                {
                    "programme": span("SolarPLUS"),
                    "legal_references": [span("§ 3")],
                    "amount": None,
                }
            ],
        }

    def create_schema(self):
        return self.schema

    def extract(self, text, schema, **kw):
        assert text == TEXT and schema is self.schema
        assert kw == {
            "include_spans": True,
            "include_confidence": True,
            "threshold": 0.5,
        }
        return self.raw


def invoke(model, schema=SCHEMA):
    return GLiNERBackend(model=model).extract_schema(
        TEXT, schema, source_id="captured", source_revision="r1", language="de"
    )


def test_entities_relations_and_fields_preserve_source_offsets():
    model = Model()
    result = invoke(model)
    assert result["entities"][0]["text"] == "IBB"
    assert result["relations"][0]["head"]["start"] == 4
    assert result["structures"]["funding"][0]["programme"]["text"] == "SolarPLUS"
    assert result["missing_fields"] == [
        {"structure": "funding", "record": 0, "field": "amount"}
    ]
    assert result["source_revision"] == "r1" and not result["support_verified"]
    assert result["schema_sha256"] and result["threshold"] == 0.5
    assert ("relations", SCHEMA["relations"]) in model.schema.calls


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_head",
        "unknown_relation",
        "unknown_field",
        "nonspan",
        "nan_confidence",
        "oversize",
        "duplicate_label",
    ],
)
def test_invalid_native_schema_or_spans_cannot_publish(mutation):
    model = Model()
    schema = copy.deepcopy(SCHEMA)
    if mutation == "bad_head":
        model.raw["relation_extraction"]["funds"][0]["head"]["start"] = 0
    elif mutation == "unknown_relation":
        model.raw["relation_extraction"]["invented"] = []
    elif mutation == "unknown_field":
        model.raw["funding"][0]["invented"] = span("IBB")
    elif mutation == "nonspan":
        model.raw["funding"][0]["programme"] = "SolarPLUS"
    elif mutation == "nan_confidence":
        model.raw["entities"]["authority"][0]["confidence"] = float("nan")
    elif mutation == "oversize":
        model.raw["entities"]["authority"] *= 1025
    else:
        schema["structures"]["entities"] = {"name": {"dtype": "str"}}
    with pytest.raises((BackendError, ValueError)):
        invoke(model, schema)


def test_optional_store_routes_schema_and_overrides_supplied_source(monkeypatch):
    from src.evaluation import model_backends

    model = Model()
    monkeypatch.setattr(
        model_backends, "GLiNERBackend", lambda **kw: GLiNERBackend(model=model)
    )
    conn = duckdb.connect()
    row = DocumentRevisionStore(conn).observe(
        {"document_id": "captured", "content": TEXT, "language": "de"}
    )

    def executor(op, payload, **kwargs):
        assert payload["text"] == TEXT
        return {"status": "completed", "result": dispatch(op, payload)}

    result = OptionalAnalysisStore(conn, executor=executor).run(
        "r",
        "schema",
        "gliner2",
        {"text": "caller supplied false text", "schema": SCHEMA},
        source_refs=[{"document_id": "captured", "revision_id": row["revision_id"]}],
        principal_id="a",
        scopes={"operator"},
    )
    assert result["artifact"] and result["outcome"]["result"]["relations"]
    conn.close()


def test_installed_sdk_description_qualified_relation_keys_are_normalized():
    model = Model()
    values = model.raw["relation_extraction"]["funds"]
    model.raw["relation_extraction"] = {
        "funds: " + SCHEMA["relations"]["funds"]: values,
        "funds": [],
    }
    result = invoke(model)
    assert result["relations"][0]["label"] == "funds"
    assert result["relations"][0]["native_label"].startswith("funds: ")


def test_empty_native_relation_alias_at_root_is_accepted():
    model = Model()
    model.raw["funds: " + SCHEMA["relations"]["funds"]] = []
    assert invoke(model)["relations"][0]["label"] == "funds"


@pytest.mark.parametrize("value", [None, {}, "", [{"head": "IBB"}]])
def test_root_relation_alias_cannot_hide_unvalidated_data(value):
    model = Model()
    model.raw["funds: " + SCHEMA["relations"]["funds"]] = value
    with pytest.raises(BackendError):
        invoke(model)


def test_unknown_root_relation_alias_is_rejected():
    model = Model()
    model.raw["funds: undeclared description"] = []
    with pytest.raises(BackendError):
        invoke(model)


def test_recorded_native_root_aliases_preserve_relation_endpoints():
    import json
    from pathlib import Path

    recorded = json.loads(
        (
            Path(__file__).parents[2]
            / "fixtures/workflow_review/gliner2_native_relation_aliases.json"
        ).read_text()
    )

    class RecordedModel:
        def create_schema(self):
            return SchemaBuilder()

        def extract(self, text, schema, **kwargs):
            assert text == recorded["text"]
            return copy.deepcopy(recorded["raw"])

    result = GLiNERBackend(model=RecordedModel()).extract_schema(
        recorded["text"],
        {"relations": recorded["schema"]},
        source_id=recorded["source_id"],
        source_revision="native-capture",
        language="en",
    )
    assert len(result["relations"]) == 1
    assert result["relations"][0]["label"] == "opposite"
    assert result["relations"][0]["head"]["text"] == "Buell"
    assert result["relations"][0]["tail"]["text"] == "William Rosecrans"


def test_recorded_native_schema_output_replays_with_exact_source_spans():
    import json
    from pathlib import Path

    recorded = json.loads(
        (
            Path(__file__).parents[2]
            / "fixtures/workflow_review/gliner2_native_schema.json"
        ).read_text()
    )

    class RecordedModel:
        def create_schema(self):
            return SchemaBuilder()

        def extract(self, text, schema, **kwargs):
            assert text == recorded["text"]
            return copy.deepcopy(recorded["output"])

    result = GLiNERBackend(model=RecordedModel()).extract_schema(
        recorded["text"],
        recorded["schema"],
        source_id="captured-native-fixture",
        source_revision="authored-r1",
        language="de",
    )
    assert len(result["entities"]) == 3 and len(result["relations"]) == 1
    assert result["relations"][0]["label"] == "funds"
    assert result["structures"]["award"][0]["amount"]["text"] == "2 Millionen Euro"
    assert not result["support_verified"]
