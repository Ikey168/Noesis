import copy

import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.authored_reports import ReportError
from src.kb.report_updates import ReportUpdateStore
from src.kb.evidence_changes import EvidenceResolver
from tests.unit.kb.test_authored_reports import AUTH as REPORT_AUTH, CONTENT

AUTH = {**REPORT_AUTH, 'scopes': {*REPORT_AUTH['scopes'], 'document:doc:read'}}


def setup():
    conn = duckdb.connect()
    sources = DocumentRevisionStore(conn)
    payload = {'document_id': 'doc', 'content': 'The reported value increased.', 'metadata': {}}
    source = sources.observe(payload)
    content = copy.deepcopy(CONTENT)
    dep = content['sections'][0]['assertions'][0]['dependencies'][0]
    dep['revision'] = dep['locator']['revision_id'] = source['revision_id']
    content['sections'].append({'id': 'unrelated', 'title': 'Author notes', 'assertions': [{'id': 'a3', 'text': 'A separate concern.', 'kind': 'commentary', 'dependencies': [], 'citations': []}]})
    store = ReportUpdateStore(conn)
    state = store.create('r', 'report', content, **AUTH)
    return store, sources, payload, state


def test_committed_changes_pending_coverage_and_unrelated_sections():
    store, sources, payload, report = setup()
    baseline = store.assess('r', report['report_id'], **AUTH)
    assert baseline['sections'][0]['status'] == 'current'
    pending = sources.observe({**payload, 'content': 'The value decreased.', 'metadata': {'source_pack_id': 'pack', 'source_pack_run_id': 'pending'}})
    scan = store.assess('r', report['report_id'], **AUTH)
    assert scan['sections'][0]['status'] == 'uncertain' and scan['coverage'] == 'incomplete'
    store.conn.execute('UPDATE document_revision_records SET committed_watermark=1 WHERE revision_id=?', [pending['revision_id']])
    scan = store.assess('r', report['report_id'], **AUTH)
    assert scan['sections'][0]['status'] == 'affected' and scan['sections'][1]['status'] == 'current'
    assert store.assess('r', report['report_id'], **AUTH)['assessment_id'] == scan['assessment_id']
    change = scan['sections'][0]['assertions'][0]['dependencies'][0]
    assert change['before']['revision_id'] != change['after']['revision_id']
    with pytest.raises(ReportError, match='affected'):
        store.propose('r', scan['assessment_id'], 'a3', **AUTH)


def test_individual_acceptance_rejection_history_and_concurrent_author_changes():
    store, sources, payload, report = setup()
    sources.observe({**payload, 'metadata': {'lifecycle': 'retracted'}})
    scan = store.assess('r', report['report_id'], **AUTH)
    proposal = store.propose('r', scan['assessment_id'], 'a1', **AUTH)
    assert proposal['proposal']['reasons'] == ['confirmed_withdrawal']
    unrelated = copy.deepcopy(report['content'])
    unrelated['sections'][1]['title'] = 'New author title'
    store.revise('r', report['report_id'], 1, unrelated, **AUTH)
    accepted = store.decide_proposal('r', proposal['proposal_id'], 'accept', 'Retain the warning pending reassessment.', **AUTH)
    assert accepted['status'] == 'accepted'
    current = store.inspect('r', report['report_id'], **AUTH)
    assert current['revision'] == 3 and current['content']['sections'][1]['title'] == 'New author title'
    assert store.inspect('r', report['report_id'], revision=1, **AUTH)['content'] == report['content']
    assert store.decide_proposal('r', proposal['proposal_id'], 'accept', 'Retain the warning pending reassessment.', **AUTH)['idempotent']
    assert store.export('r', report['report_id'], **AUTH)['bibliography'] == report['content']['bibliography']
    rejected = store.propose('r', scan['assessment_id'], 'a1', replacement={**proposal['proposal']['before'], 'text': 'Author alternative.'}, **AUTH)
    assert store.decide_proposal('r', rejected['proposal_id'], 'reject', 'Prefer the explicit warning.', **AUTH)['status'] == 'rejected'
    assert store.inspect('r', report['report_id'], **AUTH)['revision'] == 3


def test_affected_author_edits_new_evidence_and_revoked_access_block_acceptance():
    store, sources, payload, report = setup()
    sources.observe({**payload, 'content': 'Corrected value.'})
    scan = store.assess('r', report['report_id'], **AUTH)
    proposal = store.propose('r', scan['assessment_id'], 'a1', **AUTH)
    with pytest.raises(ReportError, match='current access'):
        store.inspect_proposal('r', proposal['proposal_id'], **{**AUTH, 'scopes': AUTH['scopes'] - {'document:doc:read'}})
    sources.observe({**payload, 'content': 'Corrected again.'})
    with pytest.raises(ReportError, match='evidence changed again'):
        store.decide_proposal('r', proposal['proposal_id'], 'accept', 'Reviewed.', **AUTH)
    content = copy.deepcopy(report['content'])
    content['sections'][0]['assertions'][0]['text'] = 'Author edited this assertion.'
    store.revise('r', report['report_id'], 1, content, **AUTH)
    with pytest.raises(ReportError, match='affected assertion changed'):
        store.decide_proposal('r', proposal['proposal_id'], 'accept', 'Reviewed.', **AUTH)


def test_artifact_and_published_entity_decision_dependencies():
    from src.kb.artifacts import ArtifactGraph
    from src.kb.entity_history import EntityHistoryStore, REVIEW_SCOPE, EXECUTE_SCOPE
    conn = duckdb.connect()
    resolver = EvidenceResolver(conn, {'operator'})
    graph = ArtifactGraph(conn)
    kwargs = dict(configuration={}, producer={'name': 'test', 'version': '1'}, dependencies=[])
    first = graph.register('r', 'summary', 'figure', {'value': 1}, **kwargs)
    dep = {'kind': 'artifact', 'namespace': 'r', 'id': first['artifact_id'], 'revision': first['artifact_id'], 'locator': {}}
    assert resolver.compare(dep)['status'] == 'current'
    graph.register('r', 'summary', 'figure', {'value': 2}, **kwargs)
    assert resolver.compare(dep)['status'] == 'affected'
    entities = EntityHistoryStore(conn, now=lambda: 100)
    from tests.unit.kb.test_entity_history import entities as seed_entities
    seed_entities(entities, namespace='r')
    one = entities.decide('r', 'match', ['entity:a', 'entity:b'], {}, reviewer_id='alice', principal_id='alice', scopes={REVIEW_SCOPE})
    entities.publish_rebuild('r', one['decision_id'], 1, [], principal_id='alice', scopes={EXECUTE_SCOPE})
    dep = {'kind': 'entity', 'namespace': 'r', 'id': 'entity:a', 'revision': one['decision_id'], 'locator': {}}
    assert resolver.compare(dep)['status'] == 'current'
    two = entities.decide('r', 'non-match', ['entity:a', 'entity:b'], {}, reviewer_id='bob', principal_id='bob', scopes={REVIEW_SCOPE})
    assert resolver.compare(dep)['status'] == 'uncertain'
    entities.now = lambda: 200
    entities.publish_rebuild('r', two['decision_id'], 2, [], principal_id='alice', scopes={EXECUTE_SCOPE})
    assert resolver.compare(dep)['status'] == 'affected'


def test_claim_states_and_calculation_input_revisions():
    from src.kb.claim_timelines import ClaimTimelineStore
    from tests.unit.kb.test_claim_timelines import _add_claim, _state
    from src.kb.quantitative import QuantitativeStore
    from tests.unit.kb.test_quantitative import _metric, _observe
    conn = duckdb.connect()
    resolver = EvidenceResolver(conn, {'operator'})
    claims = ClaimTimelineStore(conn)
    _add_claim(conn, 'c1', 'The reported value increased.')
    first = _state(claims, 'c1')
    dep = {'kind': 'claim', 'namespace': 'economic', 'id': 'c1', 'revision': first['state_id'], 'locator': {}}
    assert resolver.compare(dep)['status'] == 'current'
    _state(claims, 'c1', source_retracted=True)
    assert resolver.compare(dep)['reason'] == 'confirmed_withdrawal'
    quantitative = QuantitativeStore(conn)
    metric = _metric(quantitative)
    observation = _observe(quantitative, metric['metric_id'])
    calculation = quantitative.transform_frequency('economic', [observation], from_frequency='quarterly', to_frequency='annual', aggregation='sum', principal_id='analyst', scopes={'knowledge:quantitative:calculate'})
    dep = {'kind': 'calculation', 'namespace': 'economic', 'id': calculation['calculation_id'], 'revision': calculation['calculation_id'], 'locator': {}}
    assert resolver.compare(dep)['status'] == 'current'
    newer = _observe(quantitative, metric['metric_id'], vintage='v2', value='99', release_at_ms=200, retrieved_at_ms=210, revision_of=observation['observation_id'])
    change = resolver.compare(dep)
    assert change['status'] == 'affected' and change['reason'] == 'calculation_inputs_revised'
    assert change['details'][0]['after']['observation_id'] == newer['observation_id']
    assert change['before']['calculation_id'] == change['after']['calculation_id']
