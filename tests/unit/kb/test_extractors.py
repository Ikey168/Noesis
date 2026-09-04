from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.extractors import ExtractorError, ExtractorRegistry

ROOT=Path(__file__).resolve().parents[3]


class Claims:
    def extract(self,value):
        if value.get("fail"):raise RuntimeError("one bad document")
        return [] if not value.get("text") else [{"output_type":"claim","text":value["text"]}]


@pytest.fixture()
def registry(tmp_path):
    conn=duckdb.connect(str(tmp_path/"extractors.duckdb"));value=ExtractorRegistry(conn)
    yield value
    conn.close()


def definition(version="1.0.0",minimum=10):
    value=json.loads((ROOT/"contracts/examples/knowledge-engine/extractor.json").read_text());value["semantic_version"]=version;value["configuration"]["minimum_length"]=minimum;return value


def test_contract_registration_immutability_and_capabilities(registry: ExtractorRegistry) -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-extractor-definition-v1.json").read_text());Draft7Validator.check_schema(schema);assert not list(Draft7Validator(schema).iter_errors(definition()))
    first=registry.register(definition(),Claims());assert registry.register(definition())["extractor_id"]==first["extractor_id"]
    assert first["configuration_hash"] and first["implementation"]["rule_version"]=="rules-1"
    with pytest.raises(ExtractorError,match="different content"):registry.register(definition(minimum=20))


def test_outputs_empty_unavailable_failure_isolation_and_side_by_side(registry: ExtractorRegistry) -> None:
    first=registry.register(definition(),Claims());inputs=[{"id":"a","object_type":"Document","revision":1,"text":"claim"},{"id":"b","object_type":"Document","revision":1},{"id":"c","object_type":"Document","revision":1,"fail":True}]
    run=registry.run(first["extractor_id"],"research",inputs,now_ms=100)
    assert {item["status"] for item in run["outputs"]}=={"produced","empty","failed"} and run["failures"]
    second=registry.register(definition("2.0.0"),Claims());registry.run(second["extractor_id"],"research",inputs[:1],now_ms=200)
    assert registry.conn.execute("SELECT COUNT(*) FROM knowledge_extractor_outputs WHERE input_id='a'").fetchone()==(2,)
    unavailable=registry.run(first["extractor_id"],"other",inputs[:1],available=False);assert unavailable["outputs"][0]["status"]=="unavailable"


def test_selective_reprocessing_plan(registry: ExtractorRegistry) -> None:
    old=registry.register(definition(),Claims());new=registry.register(definition("2.0.0"),Claims());registry.run(old["extractor_id"],"research",[{"id":"a","object_type":"Document","revision":1,"text":"x"},{"id":"b","object_type":"Document","revision":1,"text":"y"}])
    plan=registry.plan_reprocessing("fixture-claims",new["extractor_id"],"research",input_ids=["b"])
    assert [item["input_id"] for item in plan["inputs"]]==["b"] and not plan["overwrites_prior_outputs"]
