"""Canonical commits must invalidate active derivatives before maintenance catches up."""
import duckdb
import pytest
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.derived_revisions import DerivedRevisionStore,logical_identity


@pytest.mark.parametrize('lifecycle',['active','retracted','deleted'])
def test_correction_or_withdrawal_hides_stale_claim_and_all_projections(tmp_path,lifecycle):
    conn=duckdb.connect(str(tmp_path/'lifecycle.duckdb'))
    documents=DocumentRevisionStore(conn); derived=DerivedRevisionStore(conn,fixture_mode=True)
    first=documents.observe({'document_id':'paper','source_id':'journal','content':'Original evidence'})
    content={'statement':'Original finding'}; identity=logical_identity('claim',content)
    derived.apply_generation('science',1,[{'object_type':'claim','content':content,'document_id':'paper',
        'source_revision_id':first['revision_id'],'producer':{'name':'fixture','version':'1'}}],
        [{'document_id':'paper','revision_id':first['revision_id'],'change_kind':'added'}])
    derived.publish_generation('science',1)
    assert derived.revision('science',identity)
    assert derived.projection('science','lexical')
    documents.observe({'document_id':'paper','source_id':'journal','content':'Corrected or withdrawn evidence',
        'metadata':{'lifecycle':lifecycle}})
    assert derived.revision('science',identity) is None
    for projection in ('lexical','vector','graph','summary'):
        assert not derived.projection('science',projection)
    historical=derived.revision('science',identity,generation=1)
    assert historical['support'][0]['source_revision_id']==first['revision_id']
    assert derived.replay('science',1,1)['verified']
    conn.close()


def test_live_snapshot_pin_hold_release_atomic_reclamation_and_replay(tmp_path,monkeypatch):
    from src.kb.knowledge_retention import KnowledgeRetentionStore,KnowledgeRetentionError
    from src.kb.research_snapshots import ResearchSnapshotStore
    from src.ingestion.revisions import RevisionError
    conn=duckdb.connect(str(tmp_path/'retention.duckdb')); clock=[1000]
    documents=DocumentRevisionStore(conn); derived=DerivedRevisionStore(conn,fixture_mode=True)
    source=documents.observe({'document_id':'paper','source_id':'journal','content':'Historical text to reclaim'})
    derived.apply_generation('science',1,[{'object_type':'claim','content':{'statement':'Finding'},'document_id':'paper',
        'source_revision_id':source['revision_id'],'producer':{'name':'fixture','version':'1'}}],
        [{'document_id':'paper','revision_id':source['revision_id'],'change_kind':'added'}]);derived.publish_generation('science',1)
    snapshots=ResearchSnapshotStore(conn,now=lambda:clock[0])
    session=snapshots.begin({'namespaces':['science']},principal_id='owner',scopes={'operator'},ttl_ms=1000)
    retention=KnowledgeRetentionStore(conn,now=lambda:clock[0]);auth={'principal_id':'owner','scopes':{'operator'}}
    retention.register_policy('science','p',1,{'minimum_age_ms':0},**auth)
    retention.register_object('science','source-history','document','p',1,
        {'managed_storage':{'kind':'document_revision_payload','document_id':'paper','revision_id':source['revision_id']}},created_at_ms=0,**auth)
    assert 'current_active_source' in retention.explain('science','source-history',scopes={'operator'})['reason_codes']
    documents.observe({'document_id':'paper','source_id':'journal','content':'Withdrawal','metadata':{'lifecycle':'retracted'}})
    assert not derived.projection('science','lexical')
    hold=retention.place_hold('science','source-history','review',**auth)
    assert 'active_snapshot_pin' in retention.explain('science','source-history',scopes={'operator'})['reason_codes']
    retention.release_hold('science',hold['hold_id'],**auth)
    clock[0]=2001
    plan=retention.plan_gc('science',['source-history'],**auth)
    assert plan['eligible']==['source-history']
    original=retention._audit
    def interrupted(*args,**kwargs):
        if args[1]=='execute_gc': raise RuntimeError('injected publication failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(retention,'_audit',interrupted)
    with pytest.raises(RuntimeError,match='publication'):
        retention.execute_gc('science',plan,**auth)
    assert documents.revision('paper',revision=source['revision'])['payload']['content']=='Historical text to reclaim'
    monkeypatch.setattr(retention,'_audit',original)
    completed=retention.execute_gc('science',plan,**auth)
    assert completed['processed']==1 and retention.execute_gc('science',plan,**auth)['idempotent']
    with pytest.raises(RevisionError,match='reclaimed'):
        documents.revision('paper',revision=source['revision'])
    assert conn.execute('SELECT payload_hash FROM document_payload_reclamations').fetchone()[0]==source['payload_hash']
    assert derived.replay('science',1,1)['verified']
    assert conn.execute("SELECT count(*) FROM retention_audit WHERE operation='execute_gc'").fetchone()[0]==1


def test_held_dependent_blocks_requested_dependency_and_forged_plan():
    from tests.unit.kb.test_knowledge_retention import setup_store,add
    from src.kb.knowledge_retention import KnowledgeRetentionError
    store,_=setup_store();auth={'principal_id':'a','scopes':{'operator'}}
    add(store,'dependency');add(store,'held',dependencies=['dependency'])
    store.place_hold('research','held','hold',**auth)
    plan=store.plan_gc('research',['held','dependency'],**auth)
    assert not plan['eligible'] and 'dependency' in plan['blocked']
    forged={**plan,'eligible':['dependency']}
    with pytest.raises(KnowledgeRetentionError,match='changed'):
        store.execute_gc('research',forged,**auth)
