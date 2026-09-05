"""Regenerate synthetic reviewed-schema fixtures; execute from the repository root."""
import subprocess,types,sys,json,hashlib
from pathlib import Path
import duckdb
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
base='0bf70327'
origins={}
def old(name,path):
    raw=subprocess.check_output(['git','show',base+':'+path]);origins[path]=hashlib.sha256(raw).hexdigest()
    module=types.ModuleType(name);module.__file__=str(Path(path).resolve());sys.modules[name]=module
    exec(compile(raw,module.__file__,'exec'),module.__dict__)
    return module
conn=duckdb.connect()
docs=old('old_documents','src/ingestion/document_store.py').DocumentStore(conn)
docs.upsert([{'document_id':'legacy-document','source_type':'paper','language':'en','ingested_at':100,'source_id':'journal','url':'https://example.org/fixture','title':'Legacy fixture','content':'Original migration behavior fixture.'}])
from src.kb.derived_revisions import DerivedRevisionStore
store=DerivedRevisionStore(conn,fixture_mode=True);store.apply_generation('research',1,[],[]);store.publish_generation('research',1)
membership=old('old_membership','src/kb/membership.py');membership.ensure_membership_schema(conn)
conn.execute("INSERT INTO document_domains VALUES ('legacy-document','scientific',1,'source','legacy-run',100)")
conn.execute("INSERT INTO kb_membership_scans VALUES ('legacy-document','scientific',false,'legacy-run',100)")
subscriptions=old('old_subscriptions','src/kb/subscriptions.py').SubscriptionStore(conn)
auth={'principal_id':'alice','scopes':{'knowledge:subscriptions:read','knowledge:subscriptions:write','namespace:research:read'}}
sub=subscriptions.create({'namespace':'research','domain':'scientific','query':{'operation':'search','text':'fixture'},'delivery':{'kind':'poll'}},'legacy-key',**auth)
subscriptions.commit_watermark('research',1);subscriptions.evaluate(sub['subscription_id'],1,{'items':[{'id':'legacy-document','text':'Fixture'}]},**auth)
snapshots=old('old_snapshots','src/kb/research_snapshots.py').ResearchSnapshotStore(conn,now=lambda:100)
snapshot=snapshots.begin({'namespaces':['research']},principal_id='alice',scopes={'knowledge:snapshot:read','knowledge:snapshot:write','knowledge:read'},ttl_ms=5000)
packages=old('old_packages','src/kb/research_packages.py')
pkgstore=packages.ResearchPackageStore(conn,now=lambda:100)
from tests.unit.kb.test_research_packages import manifest
manifest_value=pkgstore.create_manifest('research',manifest(),principal_id='alice',scopes={packages.WRITE_SCOPE})
pkgstore.register_component('research','document','legacy-document',{'text':'Fixture'},principal_id='alice',scopes={packages.WRITE_SCOPE})
package=pkgstore.build('research',manifest_value['package_id'],['legacy-document'],principal_id='alice',scopes={packages.WRITE_SCOPE})
workflows=old('old_workflows','src/kb/workflows.py');workflow=workflows.reference_manifest('legacy')
handlers={stage:lambda context,state:{**state,context.stage:True,'coverage':{'complete':True}} for stage in workflows.STAGE_ORDER}
try:workflows.WorkflowStore(conn).execute(workflow,handlers,{'seed':1},run_key='legacy-run',fail_after=2,now_ms=100)
except workflows.WorkflowError:pass
# DDL and rows come from the old implementations, with synthetic inputs only.
tables=conn.execute('SELECT table_name,sql FROM duckdb_tables() WHERE NOT internal ORDER BY table_name').fetchall()
fixture={'origin_commit':base,'origin_modules':origins,'synthetic':True,'sequence_sql':[r[0] for r in conn.execute('SELECT sql FROM duckdb_sequences()').fetchall()],
    'tables':[{'name':name,'ddl':ddl,'columns':[r[0] for r in conn.execute('SELECT * FROM "'+name+'" LIMIT 0').description],
        'rows':conn.execute('SELECT * FROM "'+name+'"').fetchall()} for name,ddl in tables],
    'subscription_id':sub['subscription_id'],'snapshot_token':snapshot['token'],'package':package,'workflow':workflow,
    'workflow_run_id':conn.execute('SELECT run_id FROM knowledge_workflow_runs').fetchone()[0]}
Path('tests/fixtures/workflow_upgrade').mkdir(parents=True,exist_ok=True)
Path('tests/fixtures/workflow_upgrade/reviewed-base.json').write_text(json.dumps(fixture,sort_keys=True,indent=2)+'\n')
print(len(tables),'tables; fixture bytes',Path('tests/fixtures/workflow_upgrade/reviewed-base.json').stat().st_size)
