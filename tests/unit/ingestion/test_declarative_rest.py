from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src.ingestion.connectors.rest import (
    DeclarativeAPIConnector,
    DeclarativeAPIError,
    manifest_from_openapi,
)

ROOT=Path(__file__).resolve().parents[3]


def manifest(): return json.loads((ROOT/"contracts/examples/knowledge-engine/declarative-api.json").read_text())
def public_dns(_): return ["8.8.8.8"]


def test_contract_and_openapi_allowlist() -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-declarative-api-source-v1.json").read_text());Draft7Validator.check_schema(schema);assert not list(Draft7Validator(schema).iter_errors(manifest()))
    spec={"info":{"title":"papers"},"paths":{"/papers":{"get":{"operationId":"listPapers","parameters":[{"name":"topic","in":"query"}]}},"/write":{"post":{"operationId":"write"}}}}
    value=manifest_from_openapi(spec,base_url="https://api.example.test",allowed_hosts=["api.example.test"],operation_ids=["listPapers"],mapping={},license_name="CC0")
    assert [item["operation_id"] for item in value["operations"]]==["listPapers"]
    with pytest.raises(DeclarativeAPIError):manifest_from_openapi(spec,base_url="https://api.example.test",allowed_hosts=["api.example.test"],operation_ids=["write"],mapping={},license_name="CC0")


def test_ssrf_credentials_and_unbounded_limits_are_rejected() -> None:
    for url,hosts,dns in [("http://api.example.test",["api.example.test"],public_dns),("https://user:pass@api.example.test",["api.example.test"],public_dns),("https://localhost",["localhost"],lambda _:["127.0.0.1"])]:
        value=manifest();value["base_url"]=url;value["allowed_hosts"]=hosts
        with pytest.raises(DeclarativeAPIError):DeclarativeAPIConnector(value,dns_resolver=dns)
    value=manifest();value["pagination"]["max_pages"]=0
    with pytest.raises(DeclarativeAPIError,match="max_pages"):DeclarativeAPIConnector(value,dns_resolver=public_dns)


def test_pagination_mapping_validators_provenance_and_secret_redaction() -> None:
    calls=[]
    def transport(**kwargs):
        calls.append(kwargs);cursor=kwargs["params"].get("cursor");items=[{"id":"p1","source_type":"paper","language":"en","fetched_at":100,"title":"One","abstract":"A"}] if not cursor else [{"id":"p2","source_type":"paper","language":"en","fetched_at":101,"title":"Two","abstract":"B"}]
        return {"status":200,"headers":{"ETag":"v1"},"content":json.dumps({"data":{"items":items,"next":"two" if not cursor else None}})}
    value=manifest();value["operations"][0]["secret_headers"]={"Authorization":"api-token"}
    connector=DeclarativeAPIConnector(value,transport=transport,secret_resolver=lambda _:"Bearer TOPSECRET",dns_resolver=public_dns)
    docs=connector.run("papers",{"topic":"science"})
    assert [doc.document_id for doc in docs]==["p1","p2"] and len(calls)==2
    provenance=docs[0].metadata["api_provenance"]
    assert provenance["source_license"]=="CC-BY-4.0" and provenance["request_identity"]
    assert "TOPSECRET" not in json.dumps(connector.describe()) and connector.validators["papers"]=={"If-None-Match":"v1"}


def test_parameter_schema_drift_size_and_pagination_guards() -> None:
    value=manifest()
    connector=DeclarativeAPIConnector(value,transport=lambda **_:{"content":"{}"},dns_resolver=public_dns)
    with pytest.raises(DeclarativeAPIError,match="parameter"):list(connector.discover({"operation_id":"papers","evil":"x"}))
    with pytest.raises(DeclarativeAPIError,match="array"):connector.run("papers")
    connector=DeclarativeAPIConnector(value,transport=lambda **_:{"content":b"x"*2_000_001},dns_resolver=public_dns)
    with pytest.raises(DeclarativeAPIError,match="byte"):connector.run("papers")
    value["pagination"]["max_pages"]=1
    connector=DeclarativeAPIConnector(value,transport=lambda **_:{"content":json.dumps({"data":{"items":[],"next":"again"}})},dns_resolver=public_dns)
    with pytest.raises(DeclarativeAPIError,match="max_pages"):connector.run("papers")
