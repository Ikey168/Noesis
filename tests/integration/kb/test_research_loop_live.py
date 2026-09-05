"""Opt-in public Europe PMC acquisition and real-model project cycle."""
import json
import os
from pathlib import Path

import duckdb
import pytest

from src.kb.research_loops import ResearchLoopStore
from src.kb.source_planner import SourcePlannerStore
from src.kb.research_gaps import ResearchGapStore
from src.ingestion.source_packs import SourcePackStore
from src.ingestion.source_pack_runtime import SourcePackRuntime
from tests.unit.kb.test_research_gaps import _policy, _observe

pytestmark=pytest.mark.skipif(os.environ.get('NOESIS_LIVE_RESEARCH')!='1',reason='opt-in public API and cached real-model execution')


def test_live_two_domain_project_cycle(tmp_path):
    conn=duckdb.connect(str(tmp_path/'research.duckdb'))
    store=ResearchLoopStore(conn)
    auth={'principal_id':'local-live-validation','scopes':{'operator'}}
    manifest=json.loads(Path('config/source_packs/scientific.json').read_text())
    SourcePackStore(conn).install(manifest,principal_id=auth['principal_id'],enable=True)
    SourcePackRuntime(conn).accept_license(manifest['pack_id'],'europe-pmc',principal_id=auth['principal_id'],redistribution=False)
    project=store.create('project-live','two-domain',questions=['What evidence connects economic recession and illicit drug use?',
        'What evidence concerns chloroplast development and photosynthesis?'],success_criteria=['One matching sourced abstract in each declared domain'],
        scope={'namespaces':['economic','scientific'],'domains':['economic','scientific']},budget={'requests':20},**auth)
    planner=SourcePlannerStore(conn); gaps=ResearchGapStore(conn)
    bindings=[]
    for index,(ns,pmid) in enumerate([('economic','37409756'),('scientific','42605531')]):
        _policy(gaps,namespace=ns); _observe(gaps,'question:'+ns,[],namespace=ns)
        gaps.discover(ns,principal_id=auth['principal_id'],scopes={'knowledge:gaps:write'})
        gaps.prioritize(ns,budget=10,principal_id=auth['principal_id'],scopes={'knowledge:gaps:write'})
        task=gaps.tasks(ns,scopes={'knowledge:gaps:read'})['items'][0]
        planner.register_capability(ns,'europe-pmc','live-v1',coverage={'domains':[ns],'evidence_classes':['literature']},
            authority={'score':0.8,'basis':'publication metadata and abstract'},access={'license_id':'europe-pmc-terms','terms_accepted':True,'redistribution':False},
            latency={'p95_ms':15000},cost={'per_query':0},rate_limits={'requests_per_minute':6},query_forms=['identifier'],
            connector={'kind':'source-pack','pack_id':manifest['pack_id'],'source_id':'europe-pmc'},dependency_group='europe-pmc',
            principal_id=auth['principal_id'],scopes={'knowledge:source-planner:write'})
        objective=planner.create_objective(ns,project['questions'][index],[{'question':project['questions'][index],'query_form':'identifier',
            'parameters':{'query':f'EXT_ID:{pmid} AND SRC:MED'}}],['literature'],
            {'domain':ns,'max_pages':1,'max_results':1,'retries':0,'redistribute':False,'min_independence':1},
            principal_id=auth['principal_id'],scopes={'knowledge:source-planner:write'})
        plan=planner.preview(ns,objective['objective_id'],at_ms=store.now(),persist=True,principal_id=auth['principal_id'],scopes={'knowledge:source-planner:write'})
        assert plan['feasible'],plan
        bindings.append({'gap_namespace':ns,'gap_task_id':task['task_id'],'plan_namespace':ns,'plan_id':plan['plan_id'],'question_index':index,
            'domain':ns,'cost_ceiling':{'requests':2},'minimum_semantic_score':0.2})
    loop=store.create_loop('project-live',project['project_id'],'run',bindings,
        {'max_iterations':2,'max_results':4,'max_retries':1,'timeout_ms':180000,'independent_sources_per_domain':1},**auth)
    result=store.execute_loop('project-live',loop['loop_id'],**auth)
    evidence={'status':result['status'],'state':result['state'],'runtime':result['runtime'],
        'validation_kind':'live public API and real local models; selected-publication integration check',
        'independent_human_quality_judgments':False,'source_urls':['https://europepmc.org/article/MED/37409756','https://europepmc.org/article/MED/42605531'],
        'budget':store.inspect_budget('project-live',project['project_id'],**auth)}
    target=os.environ.get('NOESIS_RESEARCH_LOOP_EVIDENCE_PATH')
    if target:
        Path(target).write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n')
    assert result['status']=='completed',result['state']
    assert result['state']['completed_iterations']==2
    before=conn.execute('SELECT count(*) FROM source_pack_runs').fetchone()[0]
    assert store.execute_loop('project-live',loop['loop_id'],**auth)['status']=='completed'
    assert conn.execute('SELECT count(*) FROM source_pack_runs').fetchone()[0]==before
