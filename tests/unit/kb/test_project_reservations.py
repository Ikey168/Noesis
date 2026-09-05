from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import duckdb
import pytest

from src.kb.research_projects import ResearchProjectStore, ResearchProjectError

AUTH = {'principal_id': 'alice', 'scopes': {'knowledge:projects:read', 'knowledge:projects:write', 'namespace:r:write'}}


def create(conn):
    store = ResearchProjectStore(conn)
    project = store.create('r', 'p', questions=['Research question'], success_criteria=['Evidence coverage'],
        scope={'namespaces': ['r'], 'domains': []}, budget={'requests': 10, 'tokens': 100}, **AUTH)
    return store, project


def test_reservations_bound_direct_spending_and_settle_once_after_pause():
    store, project = create(duckdb.connect())
    identity = project['project_id']
    store.reserve_budget('r', identity, 'action', {'requests': 8}, **AUTH)
    assert store.reserve_budget('r', identity, 'action', {'requests': 8}, **AUTH)['idempotent']
    assert store.inspect_budget('r', identity, **AUTH)['available']['requests'] == 2
    with pytest.raises(ResearchProjectError, match='budget'):
        store.reserve_budget('r', identity, 'other', {'requests': 3}, **AUTH)
    state = store.inspect('r', identity, **AUTH)
    with pytest.raises(ResearchProjectError, match='budget'):
        store.record_expenditure('r', identity, 'bypass', {'requests': 3}, state['revision'], **AUTH)
    store.revise('r', identity, state['revision'], status='paused', **AUTH)
    with pytest.raises(ResearchProjectError, match='reserved ceiling'):
        store.settle_budget('r', identity, 'action', {'requests': 9}, **AUTH)
    store.settle_budget('r', identity, 'action', {'requests': 6}, **AUTH)
    assert store.settle_budget('r', identity, 'action', {'requests': 6}, **AUTH)['idempotent']
    budget = store.inspect_budget('r', identity, **AUTH)
    assert budget['spent']['requests'] == 6 and budget['reserved']['requests'] == 0
    assert budget['available']['requests'] == 4
    assert store.conn.execute('SELECT count(*) FROM research_project_expenditures').fetchone()[0] == 1


def test_two_concurrent_actions_cannot_reserve_the_same_balance(tmp_path):
    path = str(tmp_path/'budget.duckdb')
    root = duckdb.connect(path)
    store, project = create(root)
    barrier = Barrier(2)
    def action(key):
        conn = duckdb.connect(path)
        local = ResearchProjectStore(conn, initialize=False)
        original = local._reserved
        def synchronized(identity):
            result = original(identity)
            barrier.wait(timeout=5)
            return result
        local._reserved = synchronized
        try:
            return local.reserve_budget('r', project['project_id'], key, {'requests': 8}, **AUTH)['status']
        except ResearchProjectError as exc:
            return exc.code
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(action, ['first', 'second']))
    assert sorted(results) == ['held', 'revision_conflict']
    assert store.inspect_budget('r', project['project_id'], **AUTH)['reserved']['requests'] == 8
