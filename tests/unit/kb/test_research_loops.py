"""Behavior fixtures for loop accounting/replay; not live research validation."""
import duckdb
import pytest

from src.kb.research_loops import ResearchLoopStore
from src.kb.research_loop_runtime import ResearchLoopRuntimeError
from src.kb.research_gaps import ResearchGapStore
from src.kb.source_planner import SourcePlannerStore
from tests.unit.kb.test_research_gaps import _policy, _observe
from tests.unit.kb.test_source_planner import _capability, _objective

AUTH={'principal_id':'alice','scopes':{'operator'}}


def setup(conn=None):
    conn=conn or duckdb.connect()
    store=ResearchLoopStore(conn)
    project=store.create('projects','p',questions=['What changed economically?','What changed scientifically?'],success_criteria=['Two-domain evidence'],
        scope={'namespaces':['economic','scientific'],'domains':['economic','scientific']},budget={'requests':20},**AUTH)
    gaps=ResearchGapStore(conn); planner=SourcePlannerStore(conn)
    bindings=[]
    for index,ns in enumerate(['economic','scientific']):
        _policy(gaps,namespace=ns)
        _observe(gaps,'claim:'+ns,[],namespace=ns)
        gaps.discover(ns,principal_id='alice',scopes={'knowledge:gaps:write'})
        gaps.prioritize(ns,budget=10,principal_id='alice',scopes={'knowledge:gaps:write'})
        task=gaps.tasks(ns,scopes={'knowledge:gaps:read'})['items'][0]
        _capability(planner,'source:'+ns,namespace=ns,cost=0)
        objective=_objective(planner,namespace=ns,constraints={'max_pages':1,'max_results':10,'retries':0,'redistribute':False})
        plan=planner.preview(ns,objective['objective_id'],scopes={'knowledge:source-planner:write'},at_ms=100,persist=True,principal_id='alice')
        bindings.append({'gap_namespace':ns,'gap_task_id':task['task_id'],'plan_namespace':ns,'plan_id':plan['plan_id'],
            'question_index':index,'domain':ns,'cost_ceiling':{'requests':2},'minimum_semantic_score':0.3})
    limits={'max_iterations':2,'max_results':10,'max_retries':1,'timeout_ms':30000,'independent_sources_per_domain':1}
    loop=store.create_loop('projects',project['project_id'],'cycle',bindings,limits,**AUTH)
    return store,project,loop,bindings,limits


class Runtime:
    calls=[]
    def __init__(self,conn,**kwargs):
        self.cancelled=kwargs['cancelled']; self.conn=conn
    def acquire(self,action):
        self.calls.append(('acquire',action['domain']))
        from src.ingestion.revisions import DocumentRevisionStore
        revision=DocumentRevisionStore(self.conn).observe({'document_id':action['domain'],'source_id':action['domain'],'content':'Behavior fixture '+action['domain']})
        return {'documents':[{'document_id':action['domain'],'revision_id':revision['revision_id']}],'live_acquisition':True,'execution_mode':'production'}
    def derive(self,action,acquired):
        self.calls.append(('derive',action['domain']))
        return {'generation':1,'claim_count':1,'documents':acquired['documents'],'execution_mode':'production'}
    def query(self,action,derived):
        self.calls.append(('query',action['domain']))
        return {'independent_groups':[action['domain']],'group_evidence':derived['documents'],'coverage_complete':True,'execution_mode':'production'}


def test_two_domain_cycle_budget_and_idempotent_replay():
    store,project,loop,bindings,limits=setup()
    Runtime.calls=[]
    assert store.create_loop('projects',project['project_id'],'cycle',bindings,limits,**AUTH)['loop_id']==loop['loop_id']
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)
    assert result['status']=='completed',result['state']
    assert result['state']['coverage']=={'economic':['economic'],'scientific':['scientific']}
    assert result['state']['completed_iterations']==2 and result['state']['results']==2
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['spent']['requests']==4
    assert store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)['status']=='completed'
    assert len(Runtime.calls)==6


def test_no_progress_and_fixture_completion_are_not_success():
    store,project,loop,bindings,limits=setup()
    class Empty(Runtime):
        def query(self,action,derived):
            return {'independent_groups':[],'group_evidence':[],'coverage_complete':True,'execution_mode':'production'}
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Empty,**AUTH)
    assert result['status']=='stopped' and result['state']['stop_reason']=='no_new_independent_evidence'
    other=store.create_loop('projects',project['project_id'],'fixture',bindings,limits,**AUTH)
    class Fixture(Runtime):
        def acquire(self,action):
            return {'documents':[],'live_acquisition':False,'execution_mode':'fixture'}
    result=store.execute_loop('projects',other['loop_id'],runtime_factory=Fixture,**AUTH)
    assert result['status']=='blocked' and result['state']['stop_reason']=='fixture_completion_rejected'


def test_committed_recipe_recovery_does_not_repeat_or_double_charge(monkeypatch):
    store,project,loop,bindings,limits=setup()
    Runtime.calls=[]
    original=store._save
    def fail_publication(identity,state,status):
        if state['completed_iterations']==1 and status=='running':
            raise RuntimeError('publication interrupted')
        return original(identity,state,status)
    monkeypatch.setattr(store,'_save',fail_publication)
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)
    assert result['status']=='blocked' and result['state']['completed_iterations']==0
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['spent']['requests']==2
    monkeypatch.setattr(store,'_save',original)
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)
    assert result['status']=='completed',result['state']
    assert len(Runtime.calls)==6
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['spent']['requests']==4


def test_cancellation_resume_deadline_and_unavailable_provider():
    store,project,loop,bindings,limits=setup()
    store.cancel_loop('projects',loop['loop_id'],**AUTH)
    assert store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)['status']=='cancelled'
    store.resume_loop('projects',loop['loop_id'],**AUTH)
    class Missing(Runtime):
        def acquire(self,action):
            raise ResearchLoopRuntimeError('provider_unavailable','configured provider unavailable')
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Missing,**AUTH)
    assert result['status']=='blocked'
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['reserved']['requests']==2
    store.conn.execute('UPDATE research_loops SET deadline_ms=1 WHERE loop_id=?',[loop['loop_id']])
    with pytest.raises(ResearchLoopRuntimeError,match='deadline'):
        store.resume_loop('projects',loop['loop_id'],**AUTH)
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Runtime,**AUTH)
    assert result['status']=='stopped' and result['state']['stop_reason']=='deadline_exhausted'


def test_coverage_drops_retracted_prior_source_before_later_domain_completion():
    store,project,loop,bindings,limits=setup()
    class Retraction(Runtime):
        def acquire(self,action):
            result=super().acquire(action)
            if action['domain']=='scientific':
                from src.ingestion.revisions import DocumentRevisionStore
                DocumentRevisionStore(self.conn).observe({'document_id':'economic','source_id':'economic','content':'Withdrawn behavior fixture','metadata':{'lifecycle':'retracted'}})
            return result
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Retraction,**AUTH)
    assert result['status']=='stopped'
    assert result['state']['coverage']['economic']==[]


def test_bounded_call_returns_at_deadline_while_provider_holds_one_worker_slot(tmp_path,monkeypatch):
    import time
    from src.kb.research_loop_runtime import ProductionResearchRuntime
    store,project,loop,bindings,limits=setup(duckdb.connect(str(tmp_path/'deadline.duckdb')))
    loop=store.create_loop('projects',project['project_id'],'short',bindings,{**limits,'timeout_ms':100},**AUTH)
    monkeypatch.setattr(ProductionResearchRuntime,'__init__',lambda self,*a,**k:None)
    def slow(self,action):
        time.sleep(0.5)
        raise ResearchLoopRuntimeError('provider_unavailable','test slow provider')
    monkeypatch.setattr(ProductionResearchRuntime,'acquire',slow)
    start=time.monotonic()
    result=store.run_bounded('projects',loop['loop_id'],wait_ms=1000,**AUTH)
    assert time.monotonic()-start<0.4,result
    assert result.get('deadline_exhausted') or result['state']['stop_reason']=='deadline_exhausted'
    # Keep the file open until the deliberately noncooperative test call returns.
    from src.kb.research_loops import _FUTURES
    for future in list(_FUTURES.values()):
        future.result(timeout=2)
    final=store.inspect_loop('projects',loop['loop_id'],**AUTH)
    assert final['state']['completed_iterations']==0 and final['state']['stop_reason']=='deadline_exhausted'


def test_project_cost_and_retry_limits_survive_resumed_attempts():
    store,project,loop,bindings,limits=setup()
    costly=[{**binding,'cost_ceiling':{'requests':12}} for binding in bindings]
    budgeted=store.create_loop('projects',project['project_id'],'bounded-cost',costly,limits,**AUTH)
    result=store.execute_loop('projects',budgeted['loop_id'],runtime_factory=Runtime,**AUTH)
    assert result['status']=='stopped' and result['state']['stop_reason']=='budget_exceeded'
    assert result['state']['completed_iterations']==1
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['spent']['requests']==12
    class Failed(Runtime):
        def acquire(self,action):
            raise ResearchLoopRuntimeError('provider_unavailable','No configured provider.')
    for _ in range(2):
        assert store.execute_loop('projects',loop['loop_id'],runtime_factory=Failed,**AUTH)['status']=='blocked'
    result=store.execute_loop('projects',loop['loop_id'],runtime_factory=Failed,**AUTH)
    assert result['status']=='stopped' and result['state']['stop_reason']=='retry_budget_exhausted'
    assert store.inspect_budget('projects',project['project_id'],**AUTH)['reserved']['requests']==2


def test_model_thread_budget_is_pinned_and_cannot_change_under_an_active_worker(monkeypatch):
    import sys
    from types import SimpleNamespace
    import src.kb.research_loop_runtime as runtime
    configured={'threads':99,'calls':0}
    def set_threads(value):
        configured['threads']=value;configured['calls']+=1
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(set_num_threads=set_threads,get_num_threads=lambda:configured['threads']))
    monkeypatch.setattr(runtime,'_MODEL_THREADS',None)
    monkeypatch.setenv('NOESIS_MODEL_THREADS','2')
    runtime.configure_model_threads();runtime.configure_model_threads()
    assert configured=={'threads':2,'calls':1}
    monkeypatch.setenv('NOESIS_MODEL_THREADS','4')
    with pytest.raises(ResearchLoopRuntimeError,match='restart'):
        runtime.configure_model_threads()
    monkeypatch.setenv('NOESIS_MODEL_THREADS','0')
    with pytest.raises(ResearchLoopRuntimeError,match='1 to 8'):
        runtime.model_threads()
