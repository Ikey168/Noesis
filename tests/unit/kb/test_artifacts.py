from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.artifacts import ArtifactError, ArtifactGraph

ROOT=Path(__file__).resolve().parents[3]


@pytest.fixture()
def graph(tmp_path):
    conn=duckdb.connect(str(tmp_path/"artifacts.duckdb"));value=ArtifactGraph(conn)
    yield value
    conn.close()


def register(graph,kind,logical,content,deps=(),complete=True):return graph.register("research",kind,logical,content,configuration={"setting":1},producer={"name":f"{kind}-builder","version":"1.0.0"},dependencies=list(deps),lineage_complete=complete,now_ms=10)


def chain(graph):
    source=register(graph,"source","source:doc",{"bytes":"v1"})
    chunk=register(graph,"chunk","chunk:doc:0",{"text":"x"},[{"dependency_id":source["artifact_id"],"kind":"source","content_hash":source["content_hash"]}])
    embedding=register(graph,"embedding","embedding:chunk",[.1],[{"dependency_id":chunk["artifact_id"],"kind":"chunk","content_hash":chunk["content_hash"]}])
    index=register(graph,"index","index:research",{"entries":1},[{"dependency_id":embedding["artifact_id"],"kind":"embedding","content_hash":embedding["content_hash"]}])
    bundle=register(graph,"bundle","bundle:result",{"answer":"x"},[{"dependency_id":chunk["artifact_id"],"kind":"chunk","content_hash":chunk["content_hash"]},{"dependency_id":index["artifact_id"],"kind":"index","content_hash":index["content_hash"]}])
    return source,chunk,embedding,index,bundle


def test_artifact_schema_all_kinds_and_shared_lineage(graph: ArtifactGraph) -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-derived-artifact-v1.json").read_text());fixture=json.loads((ROOT/"contracts/examples/knowledge-engine/artifact.json").read_text());Draft7Validator.check_schema(schema);assert not list(Draft7Validator(schema).iter_errors(fixture))
    source,chunk,embedding,index,bundle=chain(graph)
    upstream=graph.upstream(bundle["artifact_id"]);assert {edge["dependency_id"] for edge in upstream["edges"]}>={chunk["artifact_id"],index["artifact_id"],embedding["artifact_id"],source["artifact_id"]}
    assert set(graph.downstream(source["artifact_id"],namespace="research")["affected"])=={chunk["artifact_id"],embedding["artifact_id"],index["artifact_id"],bundle["artifact_id"]}
    incomplete=register(graph,"claim","claim:legacy",{},[{"dependency_id":"deleted-source","kind":"source"}],complete=False);assert graph.upstream(incomplete["artifact_id"])["incomplete"]
    enriched=register(graph,"enrichment","enrichment:doc",{},[
        {"dependency_id":"parser:pdf:2","kind":"parser"},
        {"dependency_id":"extractor:claims:1","kind":"extractor"},
        {"dependency_id":"schema:claim:3","kind":"schema"},
        {"dependency_id":"model:nli:4","kind":"model"},
        {"dependency_id":"config:run:5","kind":"configuration"},
    ])
    assert {edge["dependency_kind"] for edge in graph.upstream(enriched["artifact_id"])["edges"]}=={"parser","extractor","schema","model","configuration"}


def test_invalidation_preview_is_selective_ordered_and_side_effect_free(graph: ArtifactGraph) -> None:
    source,chunk,embedding,index,bundle=chain(graph);before=graph.conn.execute("SELECT COUNT(*) FROM knowledge_artifacts").fetchone()
    plan=graph.preview_invalidation("research",[{"dependency_id":source["artifact_id"],"reason":"source-bytes-changed"}])
    ids=[item["artifact_id"] for item in plan["artifacts"]];assert ids.index(chunk["artifact_id"])<ids.index(embedding["artifact_id"])<ids.index(index["artifact_id"])
    assert bundle["artifact_id"] in ids and plan["side_effect_free"] and graph.conn.execute("SELECT COUNT(*) FROM knowledge_artifacts").fetchone()==before
    no_op=graph.preview_invalidation("other",[{"dependency_id":source["artifact_id"],"reason":"model-changed"}]);assert not no_op["artifacts"]


def test_rebuild_checkpoint_failure_resume_atomic_watermark_and_idempotency(graph: ArtifactGraph) -> None:
    source,*_=chain(graph);plan=graph.preview_invalidation("research",[{"dependency_id":source["artifact_id"],"reason":"source-changed"}]);builders={kind:(lambda old,kind=kind:{"rebuilt":kind,"from":old["content_hash"]}) for kind in ["chunk","embedding","index","bundle"]}
    with pytest.raises(ArtifactError,match="interruption"):graph.rebuild(plan,builders,fail_after=2,max_concurrency=1,now_ms=100)
    assert graph.watermark("research")==0 and graph.conn.execute("SELECT COUNT(*) FROM knowledge_artifacts WHERE status='active'").fetchone()==(5,)
    result=graph.rebuild(plan,builders,max_concurrency=2,now_ms=200);assert result["status"]=="committed" and result["watermark"]==1 and not result["mixed_generations_visible"]
    again=graph.rebuild(plan,builders);assert again["idempotent"] and again["watermark"]==1


def test_cancellation_noop_namespace_and_model_fanout(graph: ArtifactGraph) -> None:
    source,*_=chain(graph);plan=graph.preview_invalidation("research",[{"dependency_id":source["artifact_id"],"reason":"model-version-changed"}])
    with pytest.raises(ArtifactError,match="cancelled"):graph.rebuild(plan,{},cancelled=lambda:True)
    assert graph.watermark("research")==0
    empty=graph.preview_invalidation("private",[{"dependency_id":source["artifact_id"]}]);result=graph.rebuild(empty,{})
    assert result["status"]=="no-op" and result["watermark"]==0
