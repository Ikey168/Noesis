import asyncio

from src.mcp_host.catalog import _mutability
from tools.knowledge_engine_mcp import server
from tests.unit.kb.test_review_inbox import setup


def test_public_review_routing_and_explicit_dataset_release(monkeypatch):
    store, cross, (task,), scopes = setup()
    scopes.add('knowledge:inbox:datasets')
    principal = ['coordinator']
    monkeypatch.setattr(server, '_connection', lambda *, read_only: store.conn.cursor())
    monkeypatch.setattr(server, '_context', lambda: (principal[0], scopes))
    tools = asyncio.run(server.mcp.get_tools())
    assigned = tools['assign_review_inbox_task'].fn(namespace='r', task_id=task['task_id'], reviewers=['alice', 'bob'])
    assert assigned['status'] == 'assigned', assigned
    for reviewer in ['alice', 'bob']:
        principal[0] = reviewer
        result = tools['submit_review_inbox_annotation'].fn(namespace='r', task_id=task['task_id'], expected_target_hash=task['target_revision_hash'],
            label={'decision': 'accepted'}, rationale='Checked source.', effort_ms=500, annotation_origin='human')
        assert 'error' not in result, result
    principal[0] = 'coordinator'
    assert tools['resolve_review_inbox_task'].fn(namespace='r', task_id=task['task_id'], rationale='Consensus.')['routed']['status'] == 'accepted'
    draft = tools['build_review_annotation_dataset'].fn(namespace='r', task_ids=[task['task_id']])
    assert draft['status'] == 'draft', draft
    assert tools['export_review_annotation_dataset'].fn(namespace='r', release_id=draft['release_id'])['error']['code'] == 'dataset_not_released'
    assert tools['release_review_annotation_dataset'].fn(namespace='r', release_id=draft['release_id'], rationale='Reviewed release.')['status'] == 'released'
    assert tools['export_review_annotation_dataset'].fn(namespace='r', release_id=draft['release_id'])['rows'][0]['agreement']
    assert _mutability('resolve_review_inbox_task') == 'write'
    assert _mutability('evaluate_review_annotation_predictions') == 'read'
