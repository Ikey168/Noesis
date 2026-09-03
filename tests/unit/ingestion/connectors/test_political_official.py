from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from src.ingestion.connectors.base import PermanentFetchError, RawDocument
from src.ingestion.connectors.political_official import (
    DEFAULT_CATALOG,
    PoliticalOfficialConnector,
    live_smoke,
    load_source_catalog,
    validate_source_catalog,
)
from src.ingestion.connectors.registry import is_registered

ROOT = Path(__file__).resolve().parents[4]


def test_official_catalog_is_governed_and_represents_all_source_classes():
    payload = json.loads(DEFAULT_CATALOG.read_text())
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-political-source-v1.json").read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(payload))
    assert validate_source_catalog(payload) == []
    sources = load_source_catalog()
    assert {source["source_class"] for source in sources.values()} == {
        "executive", "regulatory", "electoral", "parliamentary"
    }
    for source in sources.values():
        assert source["canonical_url"].startswith("https://")
        assert source["identifier_fields"] and source["license"] and source["update_cadence"]


def test_all_offline_fixtures_parse_with_manifest_provenance():
    connector = PoliticalOfficialConnector()
    refs = list(connector.discover({"offline": True}))
    documents = [document for ref in refs for document in connector.parse(connector.fetch(ref))]
    assert len(refs) == len(documents) == 4
    assert {document.metadata["source_manifest_id"] for document in documents} == set(connector.sources)
    for document in documents:
        assert document.source_type == "note"
        assert document.url.startswith("https://example.invalid/")
        assert document.metadata["jurisdiction"]
        assert document.metadata["issuing_institution"]
        assert document.metadata["official_identifier"]
        assert json.loads(document.metadata["source_time_receipt"])["parser_version"] == "1.0.0"


def test_parser_fails_closed_on_source_mismatch_unknown_type_and_bad_time():
    connector = PoliticalOfficialConnector()
    ref = next(iter(connector.discover({"offline": True, "source_ids": ["ca-elections-results"]})))
    raw = connector.fetch(ref)
    payload = json.loads(raw.content)
    payload["source_id"] = "wrong"
    with pytest.raises(ValueError, match="does not match"):
        connector.parse(RawDocument(ref, json.dumps(payload)))
    payload["source_id"] = ref.source_id
    payload["records"][0]["document_type"] = "press_release"
    with pytest.raises(ValueError, match="not allowed"):
        connector.parse(RawDocument(ref, json.dumps(payload)))
    payload["records"][0]["document_type"] = "election_result"
    payload["records"][0]["issued_at"] = "32 Smarch"
    with pytest.raises(ValueError, match="malformed time"):
        connector.parse(RawDocument(ref, json.dumps(payload)))


def test_live_fetch_is_doubly_opt_in_and_never_calls_network(monkeypatch):
    called = False

    def opener(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("network should not be called")

    monkeypatch.setenv("NOESIS_POLITICAL_LIVE", "1")
    connector = PoliticalOfficialConnector(opener=opener)
    ref = next(iter(connector.discover("us-federal-register-executive")))
    with pytest.raises(PermanentFetchError, match="disabled"):
        connector.fetch(ref)
    assert called is False
    assert is_registered("political-official")


def test_live_smoke_is_one_explicit_reachability_request(tmp_path, monkeypatch):
    payload = json.loads(DEFAULT_CATALOG.read_text())
    payload["sources"][0]["live"]["enabled"] = True
    catalog = tmp_path / "political-sources.json"
    catalog.write_text(json.dumps(payload))
    requests = []

    class Headers:
        @staticmethod
        def get_content_type():
            return "application/json"

    class Response:
        headers = Headers()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b"{}"

    def opener(request, timeout):
        requests.append((request.full_url, timeout))
        return Response()

    monkeypatch.setenv("NOESIS_POLITICAL_LIVE", "1")
    monkeypatch.setattr("urllib.request.urlopen", opener)
    result = live_smoke("us-federal-register-executive", catalog_path=catalog)
    assert result["reachable"] is True
    assert result["parsed"] is False
    assert len(requests) == 1
