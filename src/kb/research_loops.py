"""Persistent bounded project cycles over pinned gap and acquisition recipes."""
import concurrent.futures
import json
import threading
import time

from src.kb.research_projects import ResearchProjectStore, ResearchProjectError, _cost, _hash, _json, _links
from src.kb.research_loop_runtime import ProductionResearchRuntime, ResearchLoopRuntimeError, runtime_identity

EXECUTE_SCOPE = 'knowledge:projects:execute'
_DDL = '''
CREATE TABLE IF NOT EXISTS research_loops(
 loop_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,project_id TEXT NOT NULL,definition_json TEXT NOT NULL,
 status TEXT NOT NULL,cancel_requested BOOLEAN NOT NULL,state_json TEXT NOT NULL,deadline_ms BIGINT);
CREATE TABLE IF NOT EXISTS research_loop_actions(
 loop_id TEXT NOT NULL,ordinal BIGINT NOT NULL,attempts BIGINT NOT NULL,status TEXT NOT NULL,result_json TEXT,
 PRIMARY KEY(loop_id,ordinal));
CREATE TABLE IF NOT EXISTS research_loop_acquisitions(action_key TEXT NOT NULL,run_id TEXT NOT NULL,receipt_json TEXT NOT NULL,PRIMARY KEY(action_key,run_id));
CREATE TABLE IF NOT EXISTS research_loop_generations(action_key TEXT PRIMARY KEY,namespace TEXT NOT NULL,generation BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS research_loop_requests(action_key TEXT NOT NULL,step_id TEXT NOT NULL,request_json TEXT NOT NULL,PRIMARY KEY(action_key,step_id));
'''
_WORKERS = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='noesis-project-loop')
_SLOTS = threading.BoundedSemaphore(2)
_FUTURES = {}
_LOCK = threading.Lock()


def _limits(value):
    bounds = {'max_iterations': (1,20), 'max_results': (1,1000), 'max_retries': (0,3), 'timeout_ms': (100,3600000), 'independent_sources_per_domain': (1,10)}
    if not isinstance(value, dict) or set(value)!=set(bounds) or any(type(value[k]) is not int or not low<=value[k]<=high for k,(low,high) in bounds.items()):
        raise ResearchLoopRuntimeError('invalid_limits', 'explicit bounded iterations, results, retries, time and source coverage required')
    return dict(value)


class ResearchLoopStore(ResearchProjectStore):
    def __init__(self, conn, *, initialize=True, now=None):
        super().__init__(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)
            from src.kb.research_recipes import ResearchRecipeStore
            ResearchRecipeStore(conn)

    def create_loop(self, namespace, project_id, request_key, bindings, limits, *, principal_id, scopes):
        project = self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes)
        self._authorize(project, principal_id, scopes, write=True)
        limits = _limits(limits)
        if not isinstance(request_key, str) or not request_key or len(request_key)>1000 or not isinstance(bindings,list) or not 1<=len(bindings)<=limits['max_iterations']:
            raise ResearchLoopRuntimeError('invalid_loop', 'bounded request key and selected gap bindings required')
        from src.kb.source_planner import SourcePlannerStore
        from src.kb.research_recipes import ResearchRecipeStore
        actions = []
        for binding in bindings:
            fields = {'gap_namespace','gap_task_id','plan_namespace','plan_id','question_index','domain','cost_ceiling','minimum_semantic_score'}
            if not isinstance(binding,dict) or set(binding)!=fields or type(binding['question_index']) is not int or not 0<=binding['question_index']<len(project['questions']):
                raise ResearchLoopRuntimeError('invalid_binding', 'bind a selected gap task, acquisition plan, domain and project question index')
            for ns in (binding['gap_namespace'], binding['plan_namespace']):
                if ns not in {namespace,*project['scope']['namespaces']} or 'operator' not in scopes and f'namespace:{ns}:read' not in scopes:
                    raise ResearchLoopRuntimeError('unauthorized', 'current scoped namespace read access required')
            if project['scope']['domains'] and binding['domain'] not in project['scope']['domains']:
                raise ResearchLoopRuntimeError('scope_mismatch', 'action domain is outside the project')
            if type(binding['minimum_semantic_score']) not in {int,float} or not 0<=binding['minimum_semantic_score']<=1:
                raise ResearchLoopRuntimeError('invalid_score', 'minimum semantic score must be from zero to one')
            if 'operator' not in scopes and 'knowledge:gaps:read' not in scopes:
                raise ResearchLoopRuntimeError('unauthorized', 'gap read scope required')
            row = self.conn.execute('SELECT to_json(t) FROM research_gap_tasks t WHERE namespace=? AND task_id=?', [binding['gap_namespace'],binding['gap_task_id']]).fetchone()
            if not row:
                raise ResearchLoopRuntimeError('gap_unavailable', 'selected existing gap task is unavailable')
            from src.kb.research_gaps import ResearchGapStore
            gap=ResearchGapStore(self.conn,initialize=False).get(binding['gap_namespace'],json.loads(row[0])['gap_id'],scopes=scopes)
            if not gap or gap['status']!='open':
                raise ResearchLoopRuntimeError('gap_unavailable','selected gap must still be open')
            plan = SourcePlannerStore(self.conn, initialize=False).plan(binding['plan_namespace'],binding['plan_id'],scopes=scopes)
            if not plan['feasible'] or plan['constraints'].get('redistribute'):
                raise ResearchLoopRuntimeError('plan_unavailable', 'a feasible nonredistributing source plan is required')
            costs = _cost(binding['cost_ceiling'])
            steps = [*plan['steps'],*plan.get('fallback_steps',[])]
            if any(step['projected_cost']!=0 for step in steps):
                raise ResearchLoopRuntimeError('unsupported_cost_model','this runtime supports declared zero-cost source plans and local models; paid connectors require metered pricing guards')
            request_bound = len(steps)*plan['constraints']['max_pages']*(plan['constraints']['retries']+1)*(limits['max_retries']+1)
            if costs['requests']<request_bound:
                raise ResearchLoopRuntimeError('insufficient_reservation', 'request ceiling must cover pages and all declared retry/fallback attempts')
            actions.append({**binding, 'cost_ceiling': costs, 'gap_task_hash': _hash(json.loads(row[0])), 'plan_hash': plan['plan_hash'],
                'gap':{k:gap[k] for k in ('gap_id','gap_revision_id','object_kind','object_id','dimension')},
                'question': project['questions'][binding['question_index']], 'question_revision': project['question_revision'],
                'result_limit': limits['max_results']})
        identity = runtime_identity()
        core = {'project_id':project_id,'namespace':namespace,'owner':principal_id,'question_revision':project['question_revision'],
            'actions':actions,'limits':limits,'runtime':identity,'request_key':request_key}
        loop_id = 'research-loop:'+_hash([namespace,project_id,request_key])[:32]
        prior = self.conn.execute('SELECT definition_json FROM research_loops WHERE loop_id=?',[loop_id]).fetchone()
        if prior:
            old = json.loads(prior[0])
            if old['input_hash']!=_hash(core):
                raise ResearchLoopRuntimeError('idempotency_conflict', 'loop key already identifies different pinned inputs')
            return self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        recipes = ResearchRecipeStore(self.conn)
        for index, action in enumerate(actions):
            steps=[]
            for name, dependency in [('acquire',[]),('derive',['acquire']),('query',['derive'])]:
                steps.append({'id':name,'tool':'project-'+name,'tool_version':identity['implementation'],'depends_on':dependency,
                    'input_schema':'noesis-project-action-v1','output_schema':'noesis-project-action-result-v1','network':name=='acquire'})
            recipe=recipes.register({'recipe_id':loop_id+':'+str(index),'version':'1','namespace':namespace,'inputs':{},'steps':steps,
                'outputs':['query'],'compatibility':{'runtime':identity['implementation']},
                'limits':{'max_steps':3,'max_concurrency':1,'timeout_ms':limits['timeout_ms'],'max_output_bytes':5_000_000,'retries':0}},
                principal_id=principal_id,scopes=scopes,known_tools={'project-acquire','project-derive','project-query'})
            action['recipe_revision_id']=recipe['recipe_revision_id']
        definition={**core,'loop_id':loop_id,'input_hash':_hash({**core,'actions':[{k:v for k,v in a.items() if k!='recipe_revision_id'} for a in actions]})}
        self.conn.execute('BEGIN')
        try:
            current=self._state(namespace,project_id)
            self._authorize(current,principal_id,scopes,write=True)
            if current['question_revision']!=project['question_revision']:
                raise ResearchLoopRuntimeError('project_changed','project questions changed while compiling the loop')
            current['links']=_links([*current['links'],{'kind':'plan','id':loop_id,'namespace':namespace,'question_revision':project['question_revision']}])
            self._append(current,current['revision'])
            self.conn.execute("INSERT INTO research_loops VALUES (?,?,?,?,'ready',false,?,NULL)",[loop_id,namespace,project_id,_json(definition),_json({'completed_iterations':0,'results':0,'coverage':{},'stop_reason':None})])
            self.conn.execute('COMMIT')
        except Exception as exc:
            self._abort(exc)
        return self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)

    def inspect_loop(self,namespace,loop_id,*,principal_id,scopes):
        row=self.conn.execute('SELECT definition_json,status,cancel_requested,state_json,deadline_ms FROM research_loops WHERE namespace=? AND loop_id=?',[namespace,loop_id]).fetchone()
        if not row:
            raise ResearchLoopRuntimeError('loop_unavailable','research loop is unavailable')
        definition=json.loads(row[0])
        project=self.inspect(namespace,definition['project_id'],principal_id=principal_id,scopes=scopes)
        if definition['owner']!=principal_id and 'operator' not in scopes:
            raise ResearchLoopRuntimeError('unauthorized','loop owner required')
        for action in definition['actions']:
            if 'operator' not in scopes and any(f'namespace:{action[k]}:read' not in scopes for k in ('gap_namespace','plan_namespace')):
                raise ResearchLoopRuntimeError('unauthorized','current action namespace access required')
        return {**definition,'status':row[1],'cancel_requested':row[2],'state':json.loads(row[3]),'deadline_ms':row[4],
            'project_status':project['status'],'project_question_revision':project['question_revision']}

    def cancel_loop(self,namespace,loop_id,*,principal_id,scopes):
        loop=self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        self._authorize(self._state(namespace,loop['project_id']),principal_id,scopes,write=True)
        self.conn.execute('UPDATE research_loops SET cancel_requested=true WHERE loop_id=?',[loop_id])
        return {'loop_id':loop_id,'cancel_requested':True}

    def resume_loop(self,namespace,loop_id,*,principal_id,scopes):
        loop=self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        self._authorize(self._state(namespace,loop['project_id']),principal_id,scopes,write=True)
        if loop['project_status']!='active' or loop['project_question_revision']!=loop['question_revision']:
            raise ResearchLoopRuntimeError('project_changed','resume requires the same active project questions')
        if loop['deadline_ms'] is not None and self.now()>=loop['deadline_ms']:
            raise ResearchLoopRuntimeError('deadline_exhausted','the original run deadline cannot be extended on resume')
        if loop['status'] not in {'blocked','cancelled'}:
            raise ResearchLoopRuntimeError('not_resumable','only blocked or cancelled loops can be resumed')
        self.conn.execute("UPDATE research_loops SET status='ready',cancel_requested=false WHERE loop_id=?",[loop_id])
        return self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)

    def _save(self,loop_id,state,status):
        self.conn.execute('UPDATE research_loops SET status=?,state_json=? WHERE loop_id=?',[status,_json(state),loop_id])

    def _coverage(self,state,domain,query):
        evidence=state.setdefault('coverage_evidence',{})
        prior=state['coverage'].get(domain,[])
        evidence[domain]=list({_hash(v):v for v in [*evidence.get(domain,[]),*query['group_evidence']]}.values())
        for name,refs in evidence.items():
            current=[]
            for ref in refs:
                row=self.conn.execute('SELECT revision_id,lifecycle,source_id,content_hash FROM document_revision_records WHERE document_id=? AND committed_watermark IS NOT NULL ORDER BY revision DESC LIMIT 1',[ref['document_id']]).fetchone()
                if row and row[0]==ref['revision_id'] and row[1]=='active' and row[2] and row[2]!='unknown':
                    current.append((row[2],row[3]))
            used,groups=set(),set()
            for source,content in sorted(current):
                if content not in used:
                    groups.add(source); used.add(content)
            state['coverage'][name]=sorted(groups)
        return set(state['coverage'].get(domain,[]))-set(prior)

    def execute_loop(self,namespace,loop_id,*,principal_id,scopes,runtime_factory=ProductionResearchRuntime):
        loop=self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        if 'operator' not in scopes and EXECUTE_SCOPE not in scopes:
            raise ResearchLoopRuntimeError('unauthorized','project execution scope required')
        if loop['status'] in {'completed','stopped','cancelled'}:
            return loop
        if runtime_identity()!=loop['runtime']:
            raise ResearchLoopRuntimeError('runtime_changed','pinned runtime changed; register a new loop')
        deadline=loop['deadline_ms'] or self.now()+loop['limits']['timeout_ms']
        self.conn.execute("UPDATE research_loops SET deadline_ms=?,status='running' WHERE loop_id=?",[deadline,loop_id])
        state=loop['state']
        def cancelled():
            row=self.conn.execute('SELECT cancel_requested FROM research_loops WHERE loop_id=?',[loop_id]).fetchone()
            project=self._state(namespace,loop['project_id'])
            return bool(row[0]) or project['status']!='active' or project['question_revision']!=loop['question_revision']
        from src.kb.research_recipes import ResearchRecipeStore
        recipes=ResearchRecipeStore(self.conn)
        last_failure={}
        try:
            for index,action in enumerate(loop['actions']):
                if cancelled():
                    state['stop_reason']='cancelled_or_project_changed'; self._save(loop_id,state,'cancelled'); break
                if self.now()>=deadline:
                    state['stop_reason']='deadline_exhausted'; self._save(loop_id,state,'stopped'); break
                row=self.conn.execute('SELECT attempts,status,result_json FROM research_loop_actions WHERE loop_id=? AND ordinal=?',[loop_id,index]).fetchone()
                if row and row[1]=='completed':
                    continue
                if state['completed_iterations']>=loop['limits']['max_iterations'] or state['results']>=loop['limits']['max_results']:
                    state['stop_reason']='iteration_or_result_budget_exhausted'; self._save(loop_id,state,'stopped'); break
                reservation=f'{loop_id}:{index}'
                self.reserve_budget(namespace,loop['project_id'],reservation,action['cost_ceiling'],principal_id=principal_id,scopes=scopes)
                attempts=row[0] if row else 0
                completed_recipe=self.conn.execute("SELECT 1 FROM research_recipe_runs WHERE namespace=? AND recipe_revision_id=? AND run_key=? AND status='completed'",[namespace,action['recipe_revision_id'],reservation]).fetchone()
                if attempts>loop['limits']['max_retries'] and not completed_recipe:
                    state['stop_reason']='retry_budget_exhausted'; self._save(loop_id,state,'stopped'); break
                if not completed_recipe:
                    self.conn.execute("INSERT INTO research_loop_actions VALUES (?, ?,1,'running',NULL) ON CONFLICT(loop_id,ordinal) DO UPDATE SET attempts=research_loop_actions.attempts+1,status='running'",[loop_id,index])
                runtime=runtime_factory(self.conn,principal_id=principal_id,scopes=scopes,deadline_ms=deadline,cancelled=cancelled,loop_id=loop_id,action_index=index)
                bounded={**action,'result_limit':min(action['result_limit'],loop['limits']['max_results']-state['results'])}
                last_failure.clear()
                def invoke(method,*args):
                    try:
                        return method(*args)
                    except Exception as exc:
                        last_failure.update(code=getattr(exc,'code',type(exc).__name__),message=str(exc)[:500]); raise
                adapters={'project-acquire':lambda step,s: invoke(runtime.acquire,bounded),
                    'project-derive':lambda step,s: invoke(runtime.derive,bounded,s['steps']['acquire']),
                    'project-query':lambda step,s: invoke(runtime.query,bounded,s['steps']['derive'])}
                result=recipes.run(namespace,action['recipe_revision_id'],{},run_key=reservation,adapters=adapters,principal_id=principal_id,scopes=scopes,
                    network_allowed=True,granted_scopes=scopes,tool_versions={name:loop['runtime']['implementation'] for name in adapters},
                    cancelled=lambda:cancelled() or self.now()>=deadline)
                outputs=result['outputs']; query=outputs['query']; derived=outputs['derive']
                if any(v.get('execution_mode')!='production' for v in outputs.values()) or not outputs['acquire'].get('live_acquisition'):
                    raise ResearchLoopRuntimeError('fixture_completion_rejected','prepared fixture outputs cannot complete live research')
                if cancelled() or self.now()>=deadline:
                    raise ResearchLoopRuntimeError('deadline_or_cancellation','loop ended before coverage publication')
                gained=self._coverage(state,action['domain'],query)
                state['results']+=len(outputs['acquire']['documents']); state['completed_iterations']+=1
                # Charge the declared ceiling conservatively; this is bounded
                # accounting, not a claim to have measured provider billing.
                self.settle_budget(namespace,loop['project_id'],reservation,action['cost_ceiling'],principal_id=principal_id,scopes=scopes)
                state['accounting']='conservative_reserved_ceiling'; state['substantive_support_verified']=False
                state['last_recipe_run_id']=result['run_id']
                domains={a['domain'] for a in loop['actions']}
                adequate=query['coverage_complete'] and derived.get('claim_count',0)>0 and all(len(state['coverage'].get(d,[]))>=loop['limits']['independent_sources_per_domain'] for d in domains)
                state['stop_reason']='configured_coverage_met' if adequate else 'no_new_independent_evidence' if not gained else None
                self.conn.execute('BEGIN')
                try:
                    project=self._state(namespace,loop['project_id'])
                    project['links']=_links([*project['links'],{'kind':'run','id':result['run_id'],'namespace':namespace,'question_revision':loop['question_revision']}])
                    self._append(project,project['revision'])
                    self.conn.execute("UPDATE research_loop_actions SET status='completed',result_json=? WHERE loop_id=? AND ordinal=?",[_json(result),loop_id,index])
                    self._save(loop_id,state,'completed' if adequate else 'stopped' if not gained else 'running')
                    self.conn.execute('COMMIT')
                except Exception:
                    self.conn.execute('ROLLBACK'); raise
                if adequate or not gained:
                    break
            else:
                state['stop_reason']='selected_gap_actions_exhausted'; self._save(loop_id,state,'stopped')
        except Exception as exc:
            state=json.loads(self.conn.execute('SELECT state_json FROM research_loops WHERE loop_id=?',[loop_id]).fetchone()[0])
            state.update(stop_reason=last_failure.get('code',getattr(exc,'code',type(exc).__name__)),error=last_failure.get('message',str(exc)[:500]))
            if self.now()>=deadline:
                state['stop_reason']='deadline_exhausted'
            status='stopped' if 'deadline' in state['stop_reason'] or state['stop_reason']=='budget_exceeded' else 'cancelled' if state['stop_reason']=='cancelled' else 'blocked'
            self._save(loop_id,state,status)
        return self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)

    def run_bounded(self,namespace,loop_id,*,principal_id,scopes,wait_ms=1000):
        if type(wait_ms) is not int or not 0<=wait_ms<=60000:
            raise ResearchLoopRuntimeError('invalid_wait','wait_ms must be from zero to 60000')
        loop=self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        if 'operator' not in scopes and EXECUTE_SCOPE not in scopes:
            raise ResearchLoopRuntimeError('unauthorized','project execution scope required')
        if loop['status'] in {'completed','stopped','cancelled'}:
            return loop
        paths=self.conn.execute('PRAGMA database_list').fetchall()
        path=next((r[2] for r in paths if r[2]),None)
        if not path:
            raise ResearchLoopRuntimeError('persistent_database_required','bounded background research requires a file-backed warehouse')
        key=(path,loop_id)
        with _LOCK:
            for finished in [k for k,v in _FUTURES.items() if v.done()]:
                _FUTURES.pop(finished)
            future=_FUTURES.get(key)
            if future is None or future.done():
                if not _SLOTS.acquire(blocking=False):
                    return {**loop,'admission':'backpressure'}
                # Start the persisted budget at admission, including worker
                # initialization and runtime verification in the deadline.
                if loop['deadline_ms'] is None:
                    loop['deadline_ms']=self.now()+loop['limits']['timeout_ms']
                    try:
                        self.conn.execute('UPDATE research_loops SET deadline_ms=? WHERE loop_id=? AND deadline_ms IS NULL',[loop['deadline_ms'],loop_id])
                    except Exception:
                        _SLOTS.release()
                        raise
                def work():
                    import duckdb
                    try:
                        with duckdb.connect(path) as conn:
                            return ResearchLoopStore(conn,initialize=False).execute_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
                    finally:
                        _SLOTS.release()
                future=_WORKERS.submit(work); _FUTURES[key]=future
        try:
            remaining=max(0,((loop['deadline_ms'] or self.now()+loop['limits']['timeout_ms'])-self.now())/1000)
            future.result(timeout=min(wait_ms/1000,remaining))
        except concurrent.futures.TimeoutError:
            pass
        current=self.inspect_loop(namespace,loop_id,principal_id=principal_id,scopes=scopes)
        if not future.done() and current['deadline_ms'] is not None and self.now()>=current['deadline_ms']:
            self.conn.execute('UPDATE research_loops SET cancel_requested=true WHERE loop_id=?',[loop_id])
            return {**current,'deadline_exhausted':True,'cancel_requested':True,'worker_slot_retained_until_return':True}
        return current
