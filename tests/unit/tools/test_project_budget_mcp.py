import asyncio

import duckdb

from tools.knowledge_engine_mcp import server
from tests.unit.kb.test_project_reservations import create, AUTH


def test_public_budget_reservation_and_settlement(monkeypatch):
    store, project = create(duckdb.connect())
    monkeypatch.setattr(server, '_connection', lambda *, read_only: store.conn.cursor())
    monkeypatch.setattr(server, '_context', lambda: (AUTH['principal_id'], AUTH['scopes']))
    tools = asyncio.run(server.mcp.get_tools())
    kwargs = {'namespace': 'r', 'project_id': project['project_id'], 'reservation_id': 'work', 'costs': {'requests': 8}}
    assert tools['reserve_research_project_budget'].fn(**kwargs)['status'] == 'held'
    assert tools['inspect_research_project_budget'].fn(namespace='r', project_id=project['project_id'])['available']['requests'] == 2
    assert tools['settle_research_project_budget'].fn(**{**kwargs, 'costs': {'requests': 6}})['status'] == 'settled'
    assert tools['inspect_research_project_budget'].fn(namespace='r', project_id=project['project_id'])['spent']['requests'] == 6
