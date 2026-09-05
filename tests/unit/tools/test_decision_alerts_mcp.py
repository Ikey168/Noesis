import asyncio

from tools.knowledge_engine_mcp import server
from tests.unit.kb.test_decision_alerts import setup_alert


def test_public_decision_alert_delivery_and_acknowledgement(monkeypatch):
    store, sources, content, decision, watch, auth = setup_alert()
    monkeypatch.setattr(server, '_connection', lambda *, read_only: store.conn.cursor())
    monkeypatch.setattr(server, '_context', lambda: (auth['principal_id'], auth['scopes']))
    tools = asyncio.run(server.mcp.get_tools())
    sources.observe({'document_id': 'price-source', 'content': 'Prices increased.'})
    poll = tools['poll_decision_condition_watch'].fn(namespace='r', watch_id=watch['watch_id'])
    assert len(poll['tasks']) == 1, poll
    task = poll['tasks'][0]
    ack = tools['acknowledge_decision_review_task'].fn(namespace='r', task_id=task['task_id'], rationale='Review scheduled.')
    assert not ack['decision_changed_by_acknowledgement'], ack
    listed = tools['list_decision_review_tasks'].fn(namespace='r', watch_id=watch['watch_id'])
    assert listed['tasks'][0]['status'] == 'acknowledged'
    assert store.inspect('r', decision['decision_id'], **auth)['revision'] == 1
