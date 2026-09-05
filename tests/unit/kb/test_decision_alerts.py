import copy

import pytest

from src.kb.decision_alerts import DecisionAlertStore
from src.kb.decisions import DecisionError
from src.ingestion.revisions import DocumentRevisionStore
from tests.unit.kb.test_decisions import setup, AUTH as BASE_AUTH


def setup_alert():
    decisions, content, decision = setup()
    sources = DocumentRevisionStore(decisions.conn)
    source = sources.observe({'document_id': 'price-source', 'content': 'Prices stable.'})
    scopes = {*BASE_AUTH['scopes'], 'document:price-source:read', 'knowledge:briefs:read', 'knowledge:briefs:write', 'knowledge:briefs:deliver'}
    auth = {**BASE_AUTH, 'scopes': scopes}
    store = DecisionAlertStore(decisions.conn, now=lambda: 1000)
    condition = {'id': 'price-assumption', 'kind': 'assumption', 'assumption': 'Prices stable',
        'dependency': {'kind': 'source', 'namespace': 'r', 'id': 'price-source', 'revision': source['revision_id'], 'locator': {}}}
    watch = store.create_watch('r', decision['decision_id'], 1, [condition], **auth)
    return store, sources, content, decision, watch, auth


def test_material_source_alert_dedup_ack_and_later_decision_revision():
    store, sources, content, decision, watch, auth = setup_alert()
    assert store.poll_watch('r', watch['watch_id'], **auth)['tasks'] == []
    sources.observe({'document_id': 'price-source', 'content': 'Prices increased.'})
    alert = store.poll_watch('r', watch['watch_id'], **auth)['tasks'][0]
    assert alert['delivery']['brief_id'] and alert['after']['assessment']['status'] == 'affected'
    assert len(store.poll_watch('r', watch['watch_id'], **auth)['tasks']) == 1
    assert store.conn.execute('SELECT count(*) FROM change_brief_deliveries').fetchone()[0] == 1
    ack = store.acknowledge('r', alert['task_id'], 'Will reassess.', **auth)
    assert not ack['decision_changed_by_acknowledgement']
    assert store.inspect('r', decision['decision_id'], **auth)['revision'] == 1
    revised = copy.deepcopy(content); revised['rationale'] = 'Reviewing the revised prices.'
    store.revise('r', decision['decision_id'], 1, revised, **auth)
    linked = store.acknowledge('r', alert['task_id'], 'Will reassess.', subsequent_revision=2, **auth)
    assert linked['subsequent_decision_revision'] == 2
    assert store.poll_watch('r', watch['watch_id'], **auth)['requires_new_watch']
    with pytest.raises(DecisionError, match='current access'):
        store.list_tasks('r', watch['watch_id'], **{**auth, 'scopes': auth['scopes']-{'document:price-source:read'}})


def test_failed_delivery_recovers_same_task_and_filtered_brief(monkeypatch):
    from src.kb.change_briefs import ChangeBriefStore
    store, sources, content, decision, watch, auth = setup_alert()
    sources.observe({'document_id': 'price-source', 'content': 'Prices increased.'})
    original = ChangeBriefStore.deliver
    monkeypatch.setattr(ChangeBriefStore, 'deliver', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('delivery interrupted')))
    with pytest.raises(RuntimeError, match='interrupted'):
        store.poll_watch('r', watch['watch_id'], **auth)
    assert store.conn.execute('SELECT count(*) FROM decision_review_tasks').fetchone()[0] == 1
    monkeypatch.setattr(ChangeBriefStore, 'deliver', original)
    result = store.poll_watch('r', watch['watch_id'], **auth)
    assert len(result['tasks']) == 1 and result['tasks'][0]['delivery']
    sources.observe({'document_id': 'price-source', 'content': 'Prices corrected again.'})
    assert len(store.poll_watch('r', watch['watch_id'], **auth)['tasks']) == 2
    import json
    deliveries = store.conn.execute('SELECT payload_json FROM change_brief_deliveries').fetchall()
    assert len(deliveries)==2 and all(len(json.loads(r[0])['items'])==1 for r in deliveries)


def test_metric_threshold_ignores_missing_preliminary_and_conflicting_observations():
    from src.kb.quantitative import QuantitativeStore
    from tests.unit.kb.test_quantitative import _metric
    store, sources, content, decision, watch, auth = setup_alert()
    quantitative = QuantitativeStore(store.conn)
    metric = _metric(quantitative, namespace='r')
    auth = {**auth, 'scopes': {*auth['scopes'], 'knowledge:quantitative:read'}}
    rule = {'namespace': 'r', 'metric_id': metric['metric_id'], 'provider': 'provider', 'provider_series_id': 'prices',
        'period': '2026-Q1', 'unit_id': metric['unit_id'], 'comparison': 'gt', 'threshold': '100'}
    watch = store.create_watch('r', decision['decision_id'], 1, [{'id': 'price-limit', 'kind': 'metric_threshold', 'rule': rule}], **auth)
    assert watch['last_assessment'][0]['status'] == 'uncertain'
    def observe(vintage, value, stamp, **kw):
        return quantitative.observe('r', metric['metric_id'], '2026-Q1', value, provider='provider', provider_series_id='prices',
            vintage_id=vintage, release_at_ms=stamp, retrieved_at_ms=stamp, principal_id='provider', scopes={'knowledge:quantitative:write'},
            provenance={'source_url': 'https://example.org/generated-test-data'}, **kw)
    observe('preliminary', '110', 100, preliminary=True)
    assert store.poll_watch('r', watch['watch_id'], **auth)['tasks'] == []
    observe('final', '110', 200)
    result = store.poll_watch('r', watch['watch_id'], **auth)
    assert len(result['tasks']) == 1 and result['tasks'][0]['after']['value'] == '110'
    observe('conflicting-final', '90', 200)
    result = store.poll_watch('r', watch['watch_id'], **auth)
    assert result['assessment'][0]['status'] == 'uncertain' and len(result['tasks']) == 1
