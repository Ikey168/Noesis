import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.cross_language import CrossLanguageStore
from src.kb.review_inbox import ReviewInboxStore
from src.kb.review_targets import ReviewTargetError

SCOPES = {'namespace:r:write', 'knowledge:inbox:read', 'knowledge:inbox:write', 'knowledge:inbox:review',
          'knowledge:cross-language:read', 'knowledge:cross-language:write', 'knowledge:cross-language:review',
          'knowledge:entity-history:read', 'knowledge:entity-history:write', 'knowledge:entity-history:review'}


def setup(count=1):
    conn = duckdb.connect()
    store = ReviewInboxStore(conn, now=lambda: 1000)
    sources = DocumentRevisionStore(conn)
    cross = CrossLanguageStore(conn)
    tasks = []
    scopes = set(SCOPES)
    for index in range(count):
        doc = 'doc:'+str(index)
        scopes.add('document:'+doc+':read')
        revision = sources.observe({'document_id': doc, 'source_id': 'publisher:a' if index<4 else 'publisher:b', 'content': 'Original '+str(index)})
        source = cross.record_text('r', 'document', doc, 'Original '+str(index), language='de', principal_id='coordinator', scopes=scopes)
        translation = cross.record_translation('r', source['text_id'], 'en', 'Translation '+str(index), {'name': 'fixture', 'version': '1'}, principal_id='coordinator', scopes=scopes)
        task = store.create('r', {'kind': 'translation', 'namespace': 'r', 'id': translation['translation_id']},
            sources=[{'document_id': doc, 'revision_id': revision['revision_id']}], domain='economic', impact=0.9, uncertainty=0.8,
            rationale='Affects the report conclusion.', principal_id='coordinator', scopes=scopes)
        tasks.append(task)
    return store, cross, tasks, scopes


def submit(store, task, scopes, reviewer, label):
    return store.submit('r', task['task_id'], task['target_revision_hash'], {'decision': label}, 'Checked against the original.', 500, 'human', principal_id=reviewer, scopes=scopes)


def test_independent_translation_votes_and_existing_review_api_routing():
    store, cross, (task,), scopes = setup()
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    store.assign('r', task['task_id'], ['alice', 'bob'], **auth)
    submit(store, task, scopes, 'alice', 'accepted')
    assert store.inspect('r', task['task_id'], principal_id='bob', scopes=scopes)['votes'] == []
    assert cross._translation('r', task['target']['id'], scopes=scopes)['status'] == 'unreviewed'
    assert submit(store, task, scopes, 'bob', 'accepted')['status'] == 'consensus_ready'
    result = store.resolve('r', task['task_id'], 'Independent agreement.', **auth)
    assert result['agreement'] and result['routed']['status'] == 'accepted'
    assert len(cross._translation('r', task['target']['id'], scopes=scopes)['review']['history']) == 1
    assert store.resolve('r', task['task_id'], 'Independent agreement.', **auth)['idempotent']


def test_disagreement_adjudication_stale_targets_and_revocation():
    store, cross, (task,), scopes = setup()
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    store.assign('r', task['task_id'], ['alice', 'bob'], **auth)
    submit(store, task, scopes, 'alice', 'accepted')
    assert submit(store, task, scopes, 'bob', 'rejected')['status'] == 'disputed'
    with pytest.raises(ReviewTargetError, match='adjudicated label'):
        store.resolve('r', task['task_id'], 'Review disagreement.', **auth)
    result = store.resolve('r', task['task_id'], 'Meaning was reversed.', adjudicated_label={'decision': 'rejected'}, **auth)
    assert not result['agreement'] and result['routed']['status'] == 'rejected'
    with pytest.raises(ReviewTargetError, match='document read'):
        store.inspect('r', task['task_id'], **{**auth, 'scopes': scopes - {'document:doc:0:read'}})
    store, cross, (task,), scopes = setup()
    store.assign('r', task['task_id'], ['alice', 'bob'], principal_id='coordinator', scopes=scopes)
    cross.review_translation('r', task['target']['id'], 'disputed', 'external', principal_id='external', scopes=scopes)
    with pytest.raises(ReviewTargetError, match='changed'):
        submit(store, task, scopes, 'alice', 'accepted')
    assert store.conn.execute('SELECT count(*) FROM review_inbox_votes').fetchone()[0] == 0


def test_priority_filters_diversity_and_assignment_access():
    store, cross, tasks, scopes = setup(5)
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    result = store.list('r', domain='economic', limit=10, per_source=2, **auth)
    assert len(result['tasks']) == 3
    assert {item['source_group'] for item in result['tasks']} == {'publisher:a', 'publisher:b'}
    assert result['tasks'][0]['priority_reasons']['source_cap'] == 2
    assert not store.list('r', domain='health', **auth)['tasks']
    assert not store.list('r', principal_id='stranger', scopes=scopes)['tasks']
    with pytest.raises(ReviewTargetError, match='coordinator'):
        store.inspect('r', tasks[0]['task_id'], principal_id='stranger', scopes=scopes)


def test_entity_consensus_routes_existing_event_and_failure_rolls_back():
    from src.kb.entity_history import EntityHistoryStore
    from tests.unit.kb.test_entity_history import entities
    store, cross, (translation,), scopes = setup()
    history = EntityHistoryStore(store.conn)
    entities(history, namespace='r')
    proposal = history.decide('r', 'review', ['entity:a', 'entity:b'], {'candidate': True}, principal_id='machine', reviewer_id='machine', scopes=scopes)
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    task = store.create('r', {'kind': 'entity', 'namespace': 'r', 'id': proposal['decision_id']}, sources=translation['sources'],
        domain='economic', impact=1, uncertainty=1, rationale='Ambiguous actor.', **auth)
    store.assign('r', task['task_id'], ['alice', 'bob'], **auth)
    submit(store, task, scopes, 'alice', 'non-match')
    submit(store, task, scopes, 'bob', 'non-match')
    with pytest.raises(Exception, match='scope'):
        store.resolve('r', task['task_id'], 'Distinct people.', **{**auth, 'scopes': scopes - {'knowledge:entity-history:review'}})
    assert store.inspect('r', task['task_id'], **auth)['status'] == 'consensus_ready'
    result = store.resolve('r', task['task_id'], 'Distinct people.', **auth)
    assert result['routed']['event_key'] == proposal['event_key'] and result['routed']['decision_type'] == 'non-match'
    assert result['routed']['revision'] == proposal['revision']+1
