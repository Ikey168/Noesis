import asyncio

from src.mcp_host.catalog import _mutability
from tools.knowledge_engine_mcp import server
from tests.unit.kb.test_report_updates import setup, AUTH


def test_public_report_assessment_review_and_export(monkeypatch):
    store, sources, payload, report = setup()
    monkeypatch.setattr(server, '_connection', lambda *, read_only: store.conn.cursor())
    monkeypatch.setattr(server, '_context', lambda: ('alice', AUTH['scopes']))
    sources.observe({**payload, 'content': 'Corrected value.'})
    tools = asyncio.run(server.mcp.get_tools())
    assessment = tools['assess_authored_report_changes'].fn(namespace='r', report_id=report['report_id'])
    proposal = tools['propose_authored_report_edit'].fn(namespace='r', assessment_id=assessment['assessment_id'], assertion_id='a1')
    assert proposal['status'] == 'pending', proposal
    result = tools['decide_authored_report_edit'].fn(namespace='r', proposal_id=proposal['proposal_id'], decision='accept', rationale='Flag for review.')
    assert result['status'] == 'accepted', result
    exported = tools['export_authored_report'].fn(namespace='r', report_id=report['report_id'])
    assert exported['report']['revision'] == 2
    assert 'Evidence update requires review' in exported['markdown']
    assert _mutability('assess_authored_report_changes') == 'write'
    assert _mutability('inspect_authored_report_edit') == 'read'
