import json
from pathlib import Path
import duckdb
import pytest
from src.kb.warehouse_upgrade import upgrade_workflow_warehouse,WarehouseUpgradeError

FIXTURE=Path(__file__).resolve().parents[2]/'fixtures/workflow_upgrade/reviewed-base.json'


def restore(conn):
    fixture=json.loads(FIXTURE.read_text())
    for sql in fixture['sequence_sql']:conn.execute(sql)
    for table in fixture['tables']:
        conn.execute(table['ddl'])
        if table['rows']:
            columns=','.join('"'+c+'"' for c in table['columns'])
            conn.executemany('INSERT INTO "'+table['name']+'" ('+columns+') VALUES ('+','.join('?' for _ in table['columns'])+')',table['rows'])
    return fixture


def test_reviewed_storage_upgrade_rolls_back_interruption_and_preserves_public_state(tmp_path):
    path=tmp_path/'legacy.duckdb';conn=duckdb.connect(str(path));fixture=restore(conn)
    assert not conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='kb_membership_scans' AND column_name='input_hash'").fetchone()
    receipts=conn.execute('SELECT receipt_id,output_hash FROM knowledge_workflow_receipts ORDER BY receipt_id').fetchall()
    with pytest.raises(WarehouseUpgradeError,match='interruption'):
        upgrade_workflow_warehouse(conn,fail_after=4)
    assert not conn.execute("SELECT 1 FROM information_schema.columns WHERE table_name='kb_membership_scans' AND column_name='input_hash'").fetchone()
    assert conn.execute('SELECT receipt_id,output_hash FROM knowledge_workflow_receipts ORDER BY receipt_id').fetchall()==receipts
    result=upgrade_workflow_warehouse(conn);assert result['status']=='complete'
    conn.close();conn=duckdb.connect(str(path))
    assert upgrade_workflow_warehouse(conn)['status']=='complete'
    from src.ingestion.revisions import DocumentRevisionStore
    source=DocumentRevisionStore(conn).revision('legacy-document')
    assert source['payload']['content']=='Original migration behavior fixture.'
    assert conn.execute("SELECT method FROM document_domains WHERE document_id='legacy-document'").fetchone()==('source',)
    from src.kb.subscriptions import SubscriptionStore
    scopes={'knowledge:subscriptions:read','knowledge:subscriptions:write','namespace:research:read'}
    store=SubscriptionStore(conn);sid=fixture['subscription_id']
    assert store.inspect(sid,principal_id='alice',scopes=scopes)['last_watermark']==1
    assert store.poll(sid,principal_id='alice',scopes=scopes)['events'][0]['after']['text']=='Fixture'
    assert store.evaluate(sid,1,{'items':[{'id':'legacy-document','text':'Fixture'}]},principal_id='alice',scopes=scopes)['status']=='replayed'
    from src.kb.research_snapshots import ResearchSnapshotStore
    assert ResearchSnapshotStore(conn,now=lambda:200).inspect(fixture['snapshot_token'],principal_id='alice',
        scopes={'knowledge:snapshot:read','knowledge:read'})['vector']['namespaces']['research']['derived_generation']==1
    from src.kb.research_packages import ResearchPackageStore,READ_SCOPE
    assert ResearchPackageStore(conn).verify(fixture['package'])['valid']
    assert ResearchPackageStore(conn).inspect(fixture['package'],scopes={READ_SCOPE})
    from src.kb.workflows import WorkflowStore,STAGE_ORDER
    workflow=WorkflowStore(conn);assert len(workflow.inspect(fixture['workflow_run_id'])['receipts'])==2
    handlers={stage:lambda context,state:{**state,context.stage:True,'coverage':{'complete':True}} for stage in STAGE_ORDER}
    completed=workflow.execute(fixture['workflow'],handlers,{'seed':1},run_key='legacy-run',now_ms=300)
    assert completed['status']=='completed'
    conn.close()


def test_future_storage_and_subscription_versions_rejected_before_mutation():
    conn=duckdb.connect();conn.execute('CREATE TABLE workflow_storage_contract(version INTEGER,upgraded_at_ms BIGINT)')
    conn.execute('INSERT INTO workflow_storage_contract VALUES (999,1)')
    with pytest.raises(WarehouseUpgradeError,match='future'):upgrade_workflow_warehouse(conn)
    assert conn.execute('SELECT count(*) FROM information_schema.tables').fetchone()[0]==1
    conn.execute('DROP TABLE workflow_storage_contract')
    conn.execute('CREATE TABLE noesis_schema_migrations(component TEXT,version INTEGER,applied_at_ms BIGINT)')
    conn.execute("INSERT INTO noesis_schema_migrations VALUES ('knowledge-subscriptions',999,1)")
    from src.kb.subscriptions import SubscriptionError
    with pytest.raises(SubscriptionError,match='newer'):upgrade_workflow_warehouse(conn)
    assert conn.execute('SELECT count(*) FROM information_schema.tables').fetchone()[0]==1
