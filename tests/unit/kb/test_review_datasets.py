import pytest

from src.kb.review_datasets import ReviewDatasetStore, DATASET_SCOPE
from src.kb.review_targets import ReviewTargetError
from tests.unit.kb.test_review_inbox import setup, submit


def reviewed(count=2):
    original, cross, tasks, scopes = setup(count)
    store = ReviewDatasetStore(original.conn, now=lambda: 2000)
    scopes.add(DATASET_SCOPE)
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    for task in tasks:
        store.assign('r', task['task_id'], ['alice', 'bob'], **auth)
        submit(store, task, scopes, 'alice', 'accepted')
        submit(store, task, scopes, 'bob', 'accepted')
        store.resolve('r', task['task_id'], 'Independent agreement.', **auth)
    return store, tasks, auth


def test_explicit_release_provenance_effort_and_no_automatic_training():
    store, tasks, auth = reviewed()
    draft = store.build_dataset('r', [t['task_id'] for t in tasks], **auth)
    assert len(draft['rows']) == 2 and draft['effort']['self_reported_ms'] == 2000
    assert len(draft['rows'][0]['annotators']) == 2
    with pytest.raises(ReviewTargetError, match='explicit release'):
        store.export_dataset('r', draft['release_id'], **auth)
    released = store.release_dataset('r', draft['release_id'], 'Reviewed release for evaluation.', **auth)
    assert released['status'] == 'released' and not released['automatic_retraining']
    assert store.release_dataset('r', draft['release_id'], 'Reviewed release for evaluation.', **auth)['idempotent']
    assert store.export_dataset('r', draft['release_id'], **auth)['sha256']
    revoked = {**auth, 'scopes': auth['scopes'] - {'document:doc:0:read'}}
    with pytest.raises(ReviewTargetError, match='document read'):
        store.export_dataset('r', draft['release_id'], **revoked)


def test_related_document_grouping_reuses_prior_split_and_evaluates_paired_predictions():
    store, tasks, auth = reviewed()
    # A previous release assigned this document to the held-out group.
    store.conn.execute("INSERT INTO review_dataset_split_guards VALUES ('r','document:doc:0','test','prior-release')")
    # The curator discovers that two observations belong to the same study.
    import json
    for task in tasks:
        row = store.conn.execute('SELECT task_json FROM review_inbox_tasks WHERE task_id=?', [task['task_id']]).fetchone()
        content = json.loads(row[0]); content['related_groups'] = ['same-study']
        store.conn.execute('UPDATE review_inbox_tasks SET task_json=? WHERE task_id=?', [json.dumps(content), task['task_id']])
    draft = store.build_dataset('r', [t['task_id'] for t in tasks], **auth)
    assert {row['split'] for row in draft['rows']} == {'test'}
    assert len({row['group_id'] for row in draft['rows']}) == 1
    store.release_dataset('r', draft['release_id'], 'Hold-out release.', **auth)
    before = {t['task_id']: {'decision': 'rejected'} for t in tasks}
    after = {t['task_id']: {'decision': 'accepted'} for t in tasks}
    evaluation = store.evaluate_predictions('r', draft['release_id'], before, after, **auth)
    assert evaluation['before_errors'] == 2 and evaluation['after_errors'] == 0
    assert evaluation['self_reported_review_effort_ms'] == 2000 and evaluation['limitations']
    store.conn.execute("UPDATE review_dataset_split_guards SET split='train' WHERE group_token='document:doc:1'")
    with pytest.raises(ReviewTargetError, match='bridge prior dataset splits'):
        store.build_dataset('r', [t['task_id'] for t in tasks], **auth)


def test_disputed_and_machine_labels_stay_out_of_training_rows():
    original, cross, tasks, scopes = setup(2)
    store = ReviewDatasetStore(original.conn)
    scopes.add(DATASET_SCOPE)
    auth = {'principal_id': 'coordinator', 'scopes': scopes}
    for i, task in enumerate(tasks):
        store.assign('r', task['task_id'], ['alice', 'bob'], **auth)
        submit(store, task, scopes, 'alice', 'accepted')
        if i == 0:
            submit(store, task, scopes, 'bob', 'rejected')
            store.resolve('r', task['task_id'], 'Adjudicated.', adjudicated_label={'decision': 'accepted'}, **auth)
        else:
            store.submit('r', task['task_id'], task['target_revision_hash'], {'decision': 'accepted'}, 'Machine suggestion.', 0, 'machine', principal_id='bob', scopes=scopes)
            store.resolve('r', task['task_id'], 'Agreement with machine label.', **auth)
    draft = store.build_dataset('r', [t['task_id'] for t in tasks], **auth)
    assert draft['rows'] == [] and {v['reason'] for v in draft['excluded']} == {'disputed', 'machine_annotation'}
    with pytest.raises(ReviewTargetError, match='eligible independent consensus'):
        store.release_dataset('r', draft['release_id'], 'Not eligible.', **auth)
