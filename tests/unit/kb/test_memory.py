from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.memory import MemoryError, MemoryStore

ROOT=Path(__file__).resolve().parents[3]
SCOPES={"knowledge:memory:read","knowledge:memory:write","knowledge:memory:admin","namespace:research:read","tenant:tenant-a"}
SCOPE={"namespace":"research","tenant_id":"tenant-a","task_id":"task-1"}


@pytest.fixture()
def store(tmp_path: Path):
    conn=duckdb.connect(str(tmp_path/"memory.duckdb")); value=MemoryStore(conn)
    yield value
    conn.close()


def memory(content="alpha",**overrides):
    value={"kind":"semantic","scope":SCOPE,"subject":{"id":"topic-1"},"content":{"fact":content},"content_type":"value","epistemic_status":"observation","author":"alice","evidence":[{"source":"doc-1"}],"confidence":.8,"sensitivity":"private","lifecycle":{"decay_half_life_ms":1000}}
    value.update(overrides); return value


def remember(store,key="one",**overrides): return store.remember(memory(**overrides),key,principal_id="alice",scopes=SCOPES,created_at_ms=1000)


def test_schema_and_fixtures() -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-memory-object-v1.json").read_text()); Draft7Validator.check_schema(schema)
    fixture=json.loads((ROOT/"contracts/examples/knowledge-memory/preference.json").read_text()); assert fixture["epistemic_status"]=="user-confirmed"


def test_idempotent_remember_exact_dedup_and_scope_isolation(store: MemoryStore) -> None:
    first=remember(store); assert remember(store)==first
    duplicate=remember(store,"two"); assert duplicate["deduplicated"] is True
    with pytest.raises(MemoryError,match="reused"): store.remember(memory(content="other"),"one",principal_id="alice",scopes=SCOPES)
    with pytest.raises(MemoryError,match="authorized"): store.retrieve("alpha",SCOPE,principal_id="mallory",scopes={"knowledge:memory:read","namespace:research:read","tenant:other"})


def test_retrieval_ranking_decay_explanation_and_validity(store: MemoryStore) -> None:
    remember(store,"alpha",content="replication evidence",confidence=.9)
    store.remember(memory(content="unrelated",scope={**SCOPE,"task_id":None},valid_to_ms=1500),"old",principal_id="alice",scopes=SCOPES,created_at_ms=500)
    result=store.retrieve("replication",SCOPE,principal_id="alice",scopes=SCOPES,at_ms=2000)
    assert len(result["results"])==1 and result["results"][0]["memory"]["confidence"]==.9
    assert result["results"][0]["explanation"]["decay_does_not_rewrite_confidence"] is True


def test_correction_preserves_history_and_contradictions_are_not_merged(store: MemoryStore) -> None:
    first=remember(store); corrected=store.correct(first["memory_id"],{"content":{"fact":"beta"}},"correction",principal_id="alice",scopes=SCOPES,reason="new evidence")
    assert corrected["before"]["state"]=="superseded" and corrected["after"]["supersedes_id"]==first["memory_id"]
    third=remember(store,"third",content="gamma",author="bob")
    conflicts=store.contradictions(third["memory_id"],principal_id="alice",scopes=SCOPES,record=True)
    assert conflicts["contradictions"] and conflicts["silently_replaced"] is False


def test_consolidation_keeps_sources_and_marks_inference(store: MemoryStore) -> None:
    one=remember(store,"one",content="repeat",author="a"); two=remember(store,"two",content="repeat",author="b")
    result=store.consolidate([one["memory_id"],two["memory_id"]],{"summary":"repeat"},"summary",principal_id="alice",scopes=SCOPES)
    assert result["summary"]["epistemic_status"]=="inferred-summary" and result["sources_preserved"]
    assert all(store.get(mid,principal_id="alice",scopes=SCOPES)["state"]=="active" for mid in result["source_memories"])


def test_lifecycle_archive_expire_legal_hold_and_forget(store: MemoryStore) -> None:
    value=remember(store)
    store.set_policy(SCOPE,{"archive_after_ms":500},principal_id="alice",scopes=SCOPES)
    assert store.apply_lifecycle(SCOPE,principal_id="alice",scopes=SCOPES,at_ms=2000)["archived"]==1
    store.set_policy(SCOPE,{"legal_hold":True},principal_id="alice",scopes=SCOPES)
    with pytest.raises(MemoryError,match="legal hold"): store.forget(value["memory_id"],principal_id="alice",scopes=SCOPES,reason="request")
    assert store.apply_lifecycle(SCOPE,principal_id="alice",scopes=SCOPES,at_ms=9999)["legal_hold"]


def test_standard_mcp_import_export_mapping_and_token_budget(store: MemoryStore) -> None:
    payload=json.loads((ROOT/"contracts/examples/knowledge-memory/standard-mcp.json").read_text())
    imported=store.import_standard_mcp(payload,SCOPE,"interop",principal_id="alice",scopes=SCOPES)
    assert len(imported["imported"])==1 and imported["mapping_report"]["relations"]==1
    exported=store.export_standard_mcp(SCOPE,principal_id="alice",scopes=SCOPES)
    assert exported["entities"] and exported["mapping_report"]["unsupported"]
    with pytest.raises(MemoryError,match="budget"): store.import_standard_mcp(payload,SCOPE,"tiny",principal_id="alice",scopes=SCOPES,token_budget=1)


def test_ambiguous_identity_and_invalid_summary_are_rejected(store: MemoryStore) -> None:
    with pytest.raises(MemoryError,match="subject"): store.remember(memory(subject=None),"bad",principal_id="alice",scopes=SCOPES)
    with pytest.raises(MemoryError,match="evidence"): store.remember(memory(epistemic_status="inferred-summary",evidence=[]),"summary",principal_id="alice",scopes=SCOPES)
