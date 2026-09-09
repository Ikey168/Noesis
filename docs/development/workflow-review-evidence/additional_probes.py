import json, tempfile
from pathlib import Path
from unittest.mock import patch
import duckdb

def run(name,fn):
    try: print(json.dumps({'probe':name,'result':fn()},default=str),flush=True)
    except Exception as exc: print(json.dumps({'probe':name,'error':type(exc).__name__+': '+str(exc)}),flush=True)

def mining_failure():
    from src.ingestion.document_store import DocumentStore
    from src.database.local_warehouse_seed import ensure_schema
    from src.ingestion.argument_mining import mine_unprocessed_documents
    c=duckdb.connect(':memory:')
    DocumentStore(c).upsert([{'document_id':'d','source_type':'news','language':'en','ingested_at':1,'content':'A revised claim about the policy.'}])
    ensure_schema(c)
    c.execute("INSERT INTO argument_claims(claim_id,claim_text,document_id,source_type) VALUES ('c','Prior valid claim','d','news')")
    c.execute("INSERT INTO claim_evidence(evidence_id,claim_id,evidence_document_id,evidence_source_type,relation) VALUES ('e','c','d','news','supports')")
    with patch('src.argument_mining.evidence.run_pipeline',side_effect=RuntimeError('injected model failure')):
        result=mine_unprocessed_documents(c,limit=1)
    return {'status':result['status'],'failed':result['failed'],'claims_remaining':c.execute('SELECT count(*) FROM argument_claims').fetchone()[0],'evidence_remaining':c.execute('SELECT count(*) FROM claim_evidence').fetchone()[0]}

def coverage_replay():
    from src.kb.subscriptions import SubscriptionStore
    c=duckdb.connect(':memory:');s=SubscriptionStore(c)
    scopes={'knowledge:subscriptions:read','knowledge:subscriptions:write','namespace:research:read'}
    spec={'namespace':'research','domain':'scientific','query':{'operation':'search','text':'replication'},'filters':{},'cadence':{'trigger':'watermark'},'delivery':{'kind':'poll'}}
    item=s.create(spec,'probe',principal_id='p',scopes=scopes)
    s.commit_watermark('research',1)
    s.evaluate(item['subscription_id'],1,{'items':[{'id':'a'}],'coverage':{'complete':True}},principal_id='p',scopes=scopes)
    replay=s.evaluate(item['subscription_id'],1,{'items':[{'id':'a'}],'coverage':{'complete':False}},principal_id='p',scopes=scopes)
    return {'changed_coverage_replay_status':replay['status'],'stored_coverage':json.loads(c.execute('SELECT coverage_json FROM knowledge_subscription_snapshots').fetchone()[0])}

def package_closure():
    from src.kb.research_packages import ResearchPackageStore,WRITE_SCOPE,_hash
    s=ResearchPackageStore(duckdb.connect(':memory:'))
    manifest={'format_version':'1.0','question':'What changed?','plan':{'recipe_id':'r1'},'snapshot':{'generation':7},'evidence':['root'],'transformations':[{'tool':'compare'}],'findings':[{'text':'changed'}],'limitations':[],'policies':{'view':'public'},'compatibility':{'minimum_noesis':'1'},'extensions':{}}
    m=s.create_manifest('research',manifest,principal_id='p',scopes={WRITE_SCOPE})
    s.register_component('research','claim','root',{'text':'A'},dependencies=['source'],principal_id='p',scopes={WRITE_SCOPE})
    s.register_component('research','document','source',{'text':'Evidence'},principal_id='p',scopes={WRITE_SCOPE})
    pkg=s.build('research',m['package_id'],['root'],principal_id='p',scopes={WRITE_SCOPE})
    pkg['members']=[x for x in pkg['members'] if x['component_id']!='source']
    excluded={'content_hash','status','canonical_bytes_b64','byte_length','reproducible','signature','envelope'}
    pkg['content_hash']=_hash({k:v for k,v in pkg.items() if k not in excluded})
    return {'remaining_members':[x['component_id'] for x in pkg['members']],'root_dependencies':pkg['members'][0]['dependencies'],'verification':s.verify(pkg)}

def archive_io():
    from src.kb.knowledge_retention import KnowledgeRetentionStore,EXECUTE_SCOPE
    s=KnowledgeRetentionStore(duckdb.connect(':memory:'))
    checkpoint=s.checkpoint('research',1,1,[{'id':'a'}],schema_version='1',principal_id='p',scopes={EXECUTE_SCOPE})
    with tempfile.TemporaryDirectory() as folder:
        target=Path(folder)/'archive.json'
        archive=s.archive('research',checkpoint['checkpoint_id'],{'driver':'local','location':str(target)},principal_id='p',scopes={EXECUTE_SCOPE})
        restore=s.restore('research',archive['archive_id'],principal_id='p',scopes={EXECUTE_SCOPE})
        return {'archive_status':archive['status'],'archive_file_exists':target.exists(),'restore_status':restore['status'],'restored_atomically':restore['restored_atomically']}

for name,fn in [('mining_failure',mining_failure),('subscription_coverage',coverage_replay),('package_closure',package_closure),('archive_io',archive_io)]:run(name,fn)

def membership_revision():
    from src.kb import load_registry
    from src.kb.membership import run_membership_pass
    from src.ingestion.document_store import DocumentStore
    with tempfile.TemporaryDirectory() as folder:
        path=Path(folder)/'domains.yml'
        path.write_text('version: 1\ndomains:\n  - name: economics\n    backing: corpus-view\n    embedding_model: test\n    keywords: [inflation, rates]\n')
        registry=load_registry(path); c=duckdb.connect(':memory:');s=DocumentStore(c)
        doc={'document_id':'d','source_type':'note','language':'en','ingested_at':1,'content':'Inflation and rates rose.'}
        s.upsert([doc]);first=run_membership_pass(c,registry)
        s.upsert([{**doc,'content':'This article is about gardening and cats.','ingested_at':2}])
        second=run_membership_pass(c,registry)
        return {'first_pass':first,'revised_pass':second,'remaining_domains':c.execute('SELECT domain FROM document_domains').fetchall()}

def checkpoint_truncation():
    from src.kb.knowledge_retention import KnowledgeRetentionStore,EXECUTE_SCOPE
    s=KnowledgeRetentionStore(duckdb.connect(':memory:'))
    result=s.checkpoint('research',1,3,[{'id':i} for i in range(3)],schema_version='1',limit=2,principal_id='p',scopes={EXECUTE_SCOPE})
    return {'input_records':3,'limit':2,'result':result}

run('membership_revision',membership_revision)
run('checkpoint_truncation',checkpoint_truncation)
