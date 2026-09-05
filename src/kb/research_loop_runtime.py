"""Production adapters for project recipes; fixture adapters are never selected."""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import time

from src.kb.research_projects import _hash

EMBEDDING_REVISION = '1110a243fdf4706b3f48f1d95db1a4f5529b4d41'


class ResearchLoopRuntimeError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def runtime_identity():
    from src.argument_mining.model_registry import resolved_pins
    root = Path(__file__).resolve().parents[2]
    files = ['src/kb/research_loop_runtime.py', 'src/kb/research_loops.py', 'src/kb/research_recipes.py', 'src/kb/workflows.py', 'src/kb/derived_revisions.py',
             'src/ingestion/source_pack_runtime.py', 'src/ingestion/europepmc_api.py', 'src/kb/source_planner.py', 'services/embeddings/provider.py']
    libraries={}
    for name in ('torch','transformers','sentence-transformers'):
        try:
            libraries[name]=importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            libraries[name]=None
    return {'implementation': _hash({name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in files}), 'libraries':libraries,
            'models': resolved_pins(), 'embedding_provider': 'local', 'embedding_model': 'all-MiniLM-L6-v2', 'embedding_revision': EMBEDDING_REVISION}


class ProductionResearchRuntime:
    def __init__(self, conn, *, principal_id, scopes, deadline_ms, cancelled, loop_id, action_index):
        self.conn, self.principal, self.scopes = conn, principal_id, scopes
        self.deadline, self.cancelled = deadline_ms, cancelled
        self.key = f'{loop_id}:{action_index}'
        self.provider = None
        from src.argument_mining.model_registry import cached_model_path
        if cached_model_path('claim') is None:
            raise ResearchLoopRuntimeError('provider_unavailable', 'pinned claim model is absent from the local cache')

    def check(self):
        if self.cancelled():
            raise ResearchLoopRuntimeError('cancelled', 'research loop cancelled')
        if int(time.time()*1000) >= self.deadline:
            raise ResearchLoopRuntimeError('deadline_exhausted', 'persistent research deadline exhausted')

    def _secret(self, name):
        if not name.startswith('NOESIS_') or 'operator' not in self.scopes and f'credential:{name}:use' not in self.scopes:
            return None
        from src.config.env import resolve_env
        return resolve_env(name)

    def acquire(self, action):
        from src.kb.source_planner import SourcePlannerStore
        from src.ingestion.source_pack_runtime import SourcePackRuntime
        self.check()
        planner = SourcePlannerStore(self.conn)
        plan = planner.plan(action['plan_namespace'], action['plan_id'], scopes=self.scopes)
        if plan['plan_hash'] != action['plan_hash']:
            raise ResearchLoopRuntimeError('plan_changed', 'pinned acquisition plan changed')
        runtime = SourcePackRuntime(self.conn)
        def runner(capability, step, checkpoint):
            self.check()
            connector = capability['connector']
            if connector.get('kind') != 'source-pack':
                raise ResearchLoopRuntimeError('connector_unavailable', 'research loops require a registered source-pack connector')
            if len(step['queries']) != 1:
                raise ResearchLoopRuntimeError('unsupported_plan', 'bind one query per source-plan step to preserve complete query coverage')
            query = step['queries'][0]
            request = {'pack_id': connector['pack_id'], 'run_key': self.key+':'+step['step_id'], 'operation': query['query_form'],
                'source_ids': [connector.get('source_id', capability['source_id'])],
                'parameters': query.get('parameters') or {'query': query['question']}, 'redistribute': False, 'network': 'live',
                'max_pages': plan['constraints']['max_pages'], 'max_results': min(action['result_limit'], plan['constraints']['max_results']),
                'timeout_ms': max(1, min(plan['constraints']['timeout_ms'], self.deadline-int(time.time()*1000))), 'retries': 0}
            prior_request = self.conn.execute('SELECT request_json FROM research_loop_requests WHERE action_key=? AND step_id=?', [self.key, step['step_id']]).fetchone()
            if prior_request:
                request = json.loads(prior_request[0])
            else:
                self.conn.execute('INSERT INTO research_loop_requests VALUES (?,?,?)', [self.key, step['step_id'], json.dumps(request)])
            output = runtime.run(request, principal_id=self.principal, adapters=None, secret_resolver=self._secret, cancelled=self.cancelled)
            self.conn.execute('INSERT OR REPLACE INTO research_loop_acquisitions VALUES (?,?,?)', [self.key, output['run_id'], json.dumps(output)])
            return {'status': 'completed' if output.get('status')=='complete' else 'failed', 'cost': step['projected_cost'],
                'counts': {'sources': len(output.get('sources', []))}, 'error': {'code': 'acquisition_incomplete', 'message': str(output.get('status'))}}
        receipt = planner.execute(action['plan_namespace'], action['plan_id'], self.key, runner=runner,
            principal_id=self.principal, scopes=self.scopes, cancelled=self.cancelled)
        if receipt['status'] != 'completed':
            raise ResearchLoopRuntimeError('acquisition_incomplete', 'source acquisition was incomplete; fixture substitution is disabled')
        run_ids = [row[0] for row in self.conn.execute('SELECT run_id FROM research_loop_acquisitions WHERE action_key=? ORDER BY run_id', [self.key]).fetchall()]
        rows = self.conn.execute('''SELECT document_id,revision_id FROM document_revision_records WHERE run_id IN (SELECT unnest(?))
            AND committed_watermark IS NOT NULL ORDER BY document_id,revision LIMIT ?''', [run_ids, action['result_limit']+1]).fetchall()
        if len(rows)>action['result_limit']:
            raise ResearchLoopRuntimeError('result_budget_exceeded', 'acquisition exceeded the remaining loop result bound')
        return {'source_plan_receipt': receipt, 'source_run_ids': run_ids, 'documents': [{'document_id': row[0], 'revision_id': row[1]} for row in rows],
                'execution_mode': 'production', 'live_acquisition': True}

    def derive(self, action, acquired):
        from src.kb.workflows import WorkflowStore, reference_manifest, production_handlers
        from src.kb.derived_revisions import DerivedRevisionStore, maintenance_observations
        self.check()
        documents, changes = [], []
        for ref in acquired['documents']:
            row = self.conn.execute('SELECT payload_json,lifecycle FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL', [ref['document_id'], ref['revision_id']]).fetchone()
            if not row:
                raise ResearchLoopRuntimeError('input_unavailable', 'a pinned source revision is unavailable')
            changes.append({'document_id':ref['document_id'],'change_kind':'corrected' if row[1]=='active' else row[1]})
            if row[1]!='active':
                continue
            doc = json.loads(row[0]); doc['_revision_id'] = ref['revision_id']
            if len(doc.get('content','')) > 100000:
                raise ResearchLoopRuntimeError('document_budget_exceeded', 'research-loop documents are bounded to 100000 characters')
            documents.append(doc)
        if not acquired['documents']:
            return {'documents': [], 'generation': None, 'claim_count': 0, 'execution_mode': 'production'}
        manifest = reference_manifest(action['plan_namespace'])
        manifest['workflow_id'] = 'project-research-production-v1'
        manifest['domains'] = [action['domain']]
        manifest['stages'] = manifest['stages'][:3]
        # Production extraction uses ordinary source text and registered model pins.
        workflows = WorkflowStore(self.conn)
        result = workflows.execute(manifest, production_handlers(self.conn, principal_id=self.principal), {'documents': documents}, run_key=self.key,
            cancelled=lambda: self.cancelled() or int(time.time()*1000)>=self.deadline) if documents else {'run_id':None,'state':{'extraction':{'outputs':[]}}}
        self.check()
        from services.embeddings.provider import get_embedding_provider
        self.provider = get_embedding_provider(provider='local', model_name='all-MiniLM-L6-v2', revision=EMBEDDING_REVISION, local_files_only=True)
        derived = DerivedRevisionStore(self.conn, embedding_provider=self.provider)
        row = self.conn.execute('SELECT generation FROM research_loop_generations WHERE action_key=?', [self.key]).fetchone()
        if row:
            generation = row[0]
        else:
            generation = self.conn.execute('SELECT coalesce(max(generation),0)+1 FROM derived_object_generations WHERE namespace=?', [action['plan_namespace']]).fetchone()[0]
            self.conn.execute('INSERT INTO research_loop_generations VALUES (?,?,?)', [self.key, action['plan_namespace'], generation])
        observations = maintenance_observations(documents, result['state']['extraction'])
        modes={out.get('output',{}).get('value',{}).get('prediction_mode','unknown') for out in result['state']['extraction']['outputs'] if out['status']=='produced'}
        if any(not mode.startswith(('pretrained:','checkpoint:')) for mode in modes):
            raise ResearchLoopRuntimeError('provider_unavailable', 'claim extraction used an unconfigured fallback mode')
        derived.apply_generation(action['plan_namespace'], generation, observations, changes)
        self.check()
        derived.publish_generation(action['plan_namespace'], generation)
        claims = sum(out['status']=='produced' and out.get('output',{}).get('output_type')=='claim' for out in result['state']['extraction']['outputs'])
        return {'documents': acquired['documents'], 'generation': generation, 'workflow_run_id': result['run_id'], 'claim_count': claims,
                'execution_mode': 'production', 'semantic_model': self.provider.name()}

    def query(self, action, derived):
        self.check()
        if derived['generation'] is None:
            return {'results': [], 'coverage_complete': True, 'independent_groups': [], 'group_evidence': [], 'generation': None, 'execution_mode': 'production'}
        from services.embeddings.provider import get_embedding_provider
        from src.kb.derived_revisions import DerivedRevisionStore
        provider = self.provider or get_embedding_provider(provider='local', model_name='all-MiniLM-L6-v2', revision=EMBEDDING_REVISION, local_files_only=True)
        results = DerivedRevisionStore(self.conn, embedding_provider=provider).semantic_search(action['plan_namespace'], action['question'], scopes=self.scopes, limit=min(action['result_limit'], 100))
        self.check()
        selected = {ref['revision_id'] for ref in derived['documents']}
        results = [{**result, 'citations':[c for c in result['citations'] if c['revision_id'] in selected]}
            for result in results if any(c['revision_id'] in selected for c in result['citations'])]
        groups, group_evidence = set(), []
        for result in results:
            if result['score'] < action['minimum_semantic_score']:
                continue
            for citation in result['citations']:
                if citation['revision_id'] not in selected:
                    continue
                row = self.conn.execute('SELECT source_id,content_hash FROM document_revision_records WHERE revision_id=? AND committed_watermark IS NOT NULL', [citation['revision_id']]).fetchone()
                if row:
                    groups.add((row[0] or 'unknown', row[1]))
                    group_evidence.append({**citation,'source_id':row[0] or 'unknown','content_hash':row[1], 'namespace':action['plan_namespace']})
        # Source identities and shared-content hashes are proxies, not proof of
        # editorial independence. Unknown publishers never satisfy coverage.
        seen_content, independent = set(), set()
        for source, content in sorted(groups):
            if source!='unknown' and content not in seen_content:
                independent.add(source)
                seen_content.add(content)
        independent = sorted(independent)
        from src.kb.research_gaps import ResearchGapStore
        gaps=ResearchGapStore(self.conn,initialize=False)
        supports=[]
        for result in results:
            if result['score']<action['minimum_semantic_score']:
                continue
            for citation in result['citations']:
                row=self.conn.execute('SELECT source_id FROM document_revision_records WHERE revision_id=? AND committed_watermark IS NOT NULL',[citation['revision_id']]).fetchone()
                supports.append({'evidence_id':citation['revision_id'],'source_id':row[0] or 'unknown','accessible':True,'current':True,
                    'independence_group':row[0] or 'unknown','primary':False,'method_adequate':False,'stance':'unknown'})
        gap=action['gap']
        observed=gaps.observe(action['gap_namespace'],gap['object_kind'],gap['object_id'],gap['dimension'],coverage_known=True,
            supports=supports,signals={'research_loop_action':self.key,'semantic_matches':len(results),'substantive_support_verified':False},
            principal_id=self.principal,scopes=self.scopes,generation=derived['generation'],
            provenance={'project_question_revision':action['question_revision'],'selected_gap_revision':gap['gap_revision_id']})
        reassessment=gaps.discover(action['gap_namespace'],principal_id=self.principal,scopes=self.scopes,object_kind=gap['object_kind'],limit=100)
        return {'results': results, 'coverage_complete': True, 'independent_groups': independent, 'generation': derived['generation'],
                'group_evidence':group_evidence,
                'gap_observation':observed,'gap_reassessment':reassessment,
                'execution_mode': 'production', 'substantive_support_verified': False, 'independence_basis': 'declared source identities among matching committed revisions'}
