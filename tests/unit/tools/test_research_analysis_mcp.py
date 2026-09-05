import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server
from tests.unit.kb.test_research_analysis import setup, AUTH, Runtime


def test_public_analysis_execution_package_and_access(monkeypatch):
    from src.kb.analysis_runtime import PodmanNotebookRuntime
    store, manifest = setup()
    # Keep a live database; each public operation uses its own compatible cursor.
    monkeypatch.setattr(server, '_connection', lambda *, read_only: store.conn.cursor())
    scopes = {*AUTH['scopes'], 'knowledge:packages:read'}
    monkeypatch.setattr(server, '_context', lambda: ('alice', scopes))
    monkeypatch.setattr(PodmanNotebookRuntime, 'execute', Runtime().execute)
    tools = asyncio.run(server.mcp.get_tools())
    state = tools['register_research_analysis'].fn(namespace='r', request_key='analysis', manifest=manifest)
    run = tools['execute_research_analysis'].fn(namespace='r', analysis_id=state['analysis_id'], request_key='run')
    assert run['status'] == 'complete', run
    compare = tools['compare_research_analysis_runs'].fn(namespace='r', left_run_id=run['run_id'], right_run_id=run['run_id'])
    assert compare['equal']
    package = tools['export_research_analysis_package'].fn(namespace='r', run_id=run['run_id'])
    assert package['status'] == 'complete', package
    from src.kb.research_packages import ResearchPackageStore
    assert ResearchPackageStore(store.conn).verify(package)['valid']
    scopes.remove('namespace:economic:read')
    assert tools['export_research_analysis_package'].fn(namespace='r', run_id=run['run_id'])['error']['code'] == 'unauthorized'
    assert _mutability('execute_research_analysis') == 'write'
    assert _mutability('export_research_analysis_package') == 'read'
    assert 'knowledge:analysis:execute' in _required_scopes('knowledge_engine_mcp', 'write', 'execute_research_analysis')
