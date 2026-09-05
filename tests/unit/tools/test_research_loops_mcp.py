import asyncio

import duckdb

from tools.knowledge_engine_mcp import server
from src.kb.research_loop_runtime import ProductionResearchRuntime
from tests.unit.kb.test_research_loops import setup, Runtime, AUTH


def test_public_loop_creation_execution_inspection_and_access(tmp_path,monkeypatch):
    path=str(tmp_path/'loop.duckdb')
    store,project,loop,bindings,limits=setup(duckdb.connect(path))
    principal=['alice']
    scopes=[{'operator'}]
    monkeypatch.setattr(server,'_context',lambda:(principal[0],scopes[0]))
    monkeypatch.setattr(server,'_connection',lambda *,read_only:duckdb.connect(path,read_only=read_only))
    for method in ['__init__','acquire','derive','query']:
        monkeypatch.setattr(ProductionResearchRuntime,method,getattr(Runtime,method))
    monkeypatch.setattr(ProductionResearchRuntime,'calls',[],raising=False)
    tools=asyncio.run(server.mcp.get_tools())
    created=tools['create_persistent_research_loop'].fn(namespace='projects',project_id=project['project_id'],request_key='public',bindings=bindings,limits=limits)
    assert created['status']=='ready',created
    result=tools['run_persistent_research_loop'].fn(namespace='projects',loop_id=created['loop_id'],wait_ms=1000)
    assert result['status']=='completed',result
    inspected=tools['inspect_persistent_research_loop'].fn(namespace='projects',loop_id=created['loop_id'])
    assert inspected['state']['completed_iterations']==2
    principal[0]='stranger';scopes[0]={'knowledge:projects:read','namespace:projects:read','namespace:economic:read','namespace:scientific:read'}
    assert tools['inspect_persistent_research_loop'].fn(namespace='projects',loop_id=created['loop_id'])['error']['code']=='unauthorized'
