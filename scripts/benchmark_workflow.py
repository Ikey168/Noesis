"""Bounded real-model workflow workload with separately reported fault fixtures."""
import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import platform
import resource
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))

MANIFEST={'version':1,'batch_document_counts':[2,8,16],'domains':['economic','scientific'],
    'query_concurrency':2,'queries_per_worker':8,'query_results':25,'query_timeout_ms':2000,
    'torch_threads':2,'fault_requests':10,'fault_deadline_ms':40,
    'targets':{'query_p95_ms':2000,'warm_documents_per_second':1,'peak_rss_mib':4096,'fault_return_ms':250,'max_provider_workers':8}}


def percentile(values,p):return sorted(values)[max(0,math.ceil(len(values)*p)-1)]


def fault_checks():
    from src.kb.unified_query import UnifiedQueryEngine,QueryCatalog,StaticQueryAdapter,capability_definition
    release=threading.Event();entered=[];latencies=[];codes=[]
    class Slow:
        def describe(self):return capability_definition('slow','fixture',namespaces=['workload'],surfaces=['lexical'],object_types=['document'])
        def query(self,child,*,scopes):
            entered.append(1);release.wait(timeout=5);return {'items':[]}
    request={'query':'test','scope':{'namespaces':['workload']},'surfaces':['lexical'],'budgets':{'timeout_ms':40,'max_retries':3}}
    try:
        for _ in range(MANIFEST['fault_requests']):
            start=time.monotonic();result=UnifiedQueryEngine(QueryCatalog([Slow()])).execute(request,scopes={'operator'})
            latencies.append((time.monotonic()-start)*1000);codes.extend(v['error']['code'] for v in result['failures'])
        admitted=len(entered)
    finally:release.set()
    cancelled=threading.Event();cancelled.set()
    class Failed(Slow):
        def query(self,child,*,scopes):
            from src.kb.unified_query import UnifiedQueryError
            raise UnifiedQueryError('source_unavailable','controlled failure')
    engine=UnifiedQueryEngine(QueryCatalog([Failed()]))
    cancel=engine.execute(request,scopes={'operator'},cancelled=cancelled.is_set)
    partial=engine.execute(request,scopes={'operator'})
    return {'kind':'controlled failure fixtures; not provider throughput','latency_ms':latencies,'failure_codes':codes,
        'admitted_noncooperative_calls':admitted,'cancellation_status':cancel['status'],'partial_status':partial['status'],
        'budget_and_backpressure_passed':max(latencies)<250 and admitted<=8 and 'source_busy' in codes}


def benchmark():
    import duckdb,torch
    from services.embeddings.provider import get_embedding_provider
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.revisions import DocumentRevisionStore
    from src.kb.derived_revisions import DerivedRevisionStore,maintenance_observations
    from src.kb.workflows import WorkflowStore,WorkflowError,production_handlers,reference_manifest
    from src.kb.unified_query import UnifiedQueryEngine,QueryCatalog,MaintainedSemanticQueryAdapter
    from src.argument_mining.model_registry import resolved_pins
    torch.set_num_threads(2)
    corpus=json.loads((ROOT/'tests/fixtures/workflow_real_text/corpus.json').read_text())
    for doc in corpus:assert hashlib.sha256(doc['content'].encode()).hexdigest()==doc['metadata']['pinned_text_sha256']
    provider=get_embedding_provider(provider='local',model_name='all-MiniLM-L6-v2',revision='1110a243fdf4706b3f48f1d95db1a4f5529b4d41',local_files_only=True)
    batches=[];query_times=[];query_failures=[];last_input=None;last_run=None;started=time.monotonic()
    with tempfile.TemporaryDirectory(prefix='noesis-workload-') as tmp:
        path=Path(tmp)/'warehouse.duckdb';conn=duckdb.connect(str(path));conn.execute('SET threads=2')
        manifest=reference_manifest('workload');manifest['domains']=MANIFEST['domains']
        workflows=WorkflowStore(conn);handlers=production_handlers(conn)
        def queries():
            local=duckdb.connect(str(path));engine=UnifiedQueryEngine(QueryCatalog([MaintainedSemanticQueryAdapter(local,'workload',embedding_provider=provider)]))
            results=[]
            try:
                for index in range(MANIFEST['queries_per_worker']):
                    start=time.monotonic()
                    result=engine.execute({'query':['employment inflation','infrared distant galaxies'][index%2],
                        'scope':{'namespaces':['workload']},'surfaces':['semantic'],
                        'budgets':{'max_results':25,'timeout_ms':2000}},scopes={'operator'})
                    results.append(((time.monotonic()-start)*1000,result['status'],result.get('failures',[])))
            finally:local.close()
            return results
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for batch,count in enumerate(MANIFEST['batch_document_counts']):
                futures=[pool.submit(queries) for _ in range(2)] if batch else []
                documents=[];begin=time.monotonic()
                for index in range(count):
                    doc=json.loads(json.dumps(corpus[index%len(corpus)]));doc['document_id']=f'workload-{batch}-{index}'
                    DocumentStore(conn).upsert([doc]);doc['_revision_id']=DocumentRevisionStore(conn).revision(doc['document_id'])['revision_id'];documents.append(doc)
                inputs={'documents':documents};key=f'batch-{batch}'
                # First batch proves durable stage recovery under actual extraction.
                if batch==0:
                    try:workflows.execute(manifest,handlers,inputs,run_key=key,fail_after=2)
                    except WorkflowError:pass
                result=workflows.execute(manifest,handlers,inputs,run_key=key)
                derived=DerivedRevisionStore(conn,embedding_provider=provider)
                receipt=derived.apply_generation('workload',batch+1,maintenance_observations(documents,result['state']['extraction']),
                    [{'document_id':doc['document_id'],'revision_id':doc['_revision_id'],'change_kind':'added'} for doc in documents])
                derived.publish_generation('workload',batch+1)
                seconds=time.monotonic()-begin
                batches.append({'documents':count,'elapsed_seconds':seconds,'documents_per_second':count/seconds,
                    'publication_lag_ms':seconds*1000,'generation':batch+1,'claims':sum(v['status']=='produced' for v in result['state']['extraction']['outputs']),
                    'subscription_events':result['state']['report']['subscription_events'],'export_verified':result['state']['report']['verified']})
                for future in futures:
                    for latency,status,failures in future.result(timeout=30):
                        query_times.append(latency)
                        if status!='complete':query_failures.append({'status':status,'failures':failures})
                last_input,last_run=inputs,result
        replayfile=Path(tmp)/'replay.json';replayfile.write_text(json.dumps({'manifest':manifest,'inputs':last_input,'run_key':key}))
        conn.close()
        reopened=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--replay',str(path),str(replayfile)],capture_output=True,text=True,timeout=30,check=True)
        replay=json.loads(reopened.stdout)
        assert replay['run_id']==last_run['run_id'] and replay['status']=='completed'
    faults=fault_checks();peak=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    warm=sum(v['documents'] for v in batches[1:])/sum(v['elapsed_seconds'] for v in batches[1:])
    return {'contract':'noesis-workflow-workload-v1','manifest':MANIFEST,'manifest_sha256':hashlib.sha256(json.dumps(MANIFEST,sort_keys=True).encode()).hexdigest(),
        'kind':'bounded local-model workload on duplicated pinned public text; not live-source throughput or human quality',
        'model_pins':resolved_pins(),'embedding_revision':'1110a243fdf4706b3f48f1d95db1a4f5529b4d41',
        'environment':{'python':platform.python_version(),'platform':platform.platform(),'logical_cpus':os.cpu_count(),'cpu':next((line.split(':',1)[1].strip() for line in Path('/proc/cpuinfo').read_text().splitlines() if line.startswith('model name')),'unknown')},
        'source_text_sha256':[v['metadata']['pinned_text_sha256'] for v in corpus],
        'source_lengths':[len(v['content']) for v in corpus],'batches':batches,'queries':len(query_times),
        'query_latency_ms':{'p50':percentile(query_times,.5),'p95':percentile(query_times,.95),'max':max(query_times)},
        'query_failures':query_failures,'peak_rss_mib':peak,'warm_documents_per_second':warm,
        'cost':{'external_inference_usd_micros':0,'local_compute_pricing':None,'token_usage':'not metered'},
        'process_restart_replay':replay,'fault_fixtures':faults,'elapsed_seconds':time.monotonic()-started,
        'targets_passed':not query_failures and percentile(query_times,.95)<2000 and warm>=1 and peak<4096 and faults['budget_and_backpressure_passed'],
        'limitations':['Small repeated corpus exercises plumbing/cache behavior, not production-scale capacity.',
            'Publication lag is measured per bounded batch; no background queue is modeled.',
            'Fault fixtures are reported separately from actual extraction and embedding timings.']}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--out');parser.add_argument('--replay',nargs=2)
    args=parser.parse_args()
    if args.replay:
        import duckdb
        from src.kb.workflows import WorkflowStore,production_handlers
        value=json.loads(Path(args.replay[1]).read_text())
        with duckdb.connect(args.replay[0]) as conn:
            before=conn.execute('SELECT count(*) FROM knowledge_workflow_receipts').fetchone()[0]
            result=WorkflowStore(conn).execute(value['manifest'],production_handlers(conn),value['inputs'],run_key=value['run_key'])
            after=conn.execute('SELECT count(*) FROM knowledge_workflow_receipts').fetchone()[0]
            print(json.dumps({'run_id':result['run_id'],'status':result['status'],'receipt_count_unchanged':before==after}))
    else:
        if not args.out:parser.error('--out required')
        result=benchmark();Path(args.out).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
        print(json.dumps({'targets_passed':result['targets_passed'],'query_latency_ms':result['query_latency_ms'],'peak_rss_mib':result['peak_rss_mib']}))
