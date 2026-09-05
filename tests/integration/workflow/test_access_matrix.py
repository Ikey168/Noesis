"""Cross-surface current-access matrix; test data deliberately contains a sentinel."""
import json
import duckdb
import pytest
from src.kb.subscriptions import SubscriptionStore,SubscriptionError
from src.kb.research_snapshots import ResearchSnapshotStore,ResearchSnapshotError
from src.kb.derived_revisions import DerivedRevisionStore
from src.kb.portable_namespaces import PortableNamespaceStore,PortableNamespaceError
from src.kb.research_packages import ResearchPackageStore
from src.kb.unified_query import QueryCatalog,StaticQueryAdapter,UnifiedQueryEngine
from tests.unit.kb.test_subscriptions import definition,WRITE

SECRET='CLASSIFIED-MATRIX-SENTINEL'


def test_owner_source_scope_revocation_across_snapshot_watch_replay_and_delivery(tmp_path):
    conn=duckdb.connect(str(tmp_path/'matrix.duckdb'))
    derived=DerivedRevisionStore(conn,fixture_mode=True)
    derived.apply_generation('research',1,[],[]);derived.publish_generation('research',1)
    snapshots=ResearchSnapshotStore(conn)
    full=WRITE|{'knowledge:snapshot:read','knowledge:snapshot:write','source:private:read','knowledge:read'}
    auth={'principal_id':'alice','scopes':full}
    snapshot=snapshots.begin({'namespaces':['research']},**auth)
    watches=SubscriptionStore(conn)
    payload=definition(delivery={'kind':'queue','destination_ref':'local:test'})
    watch=watches.create(payload,'original',**auth);sid=watch['subscription_id']
    watches.commit_watermark('research',1)
    watches.evaluate(sid,1,{'items':[{'id':'classified','text':SECRET}]},**auth)
    page=watches.poll(sid,**auth)
    assert SECRET in json.dumps(page)
    revoked={**auth,'scopes':full-{'source:private:read'}}
    for operation in [lambda:snapshots.inspect(snapshot['token'],**revoked),
        lambda:snapshots.bind_query(snapshot['token'],{'query':'q','scope':{'namespaces':['research']}},**revoked),
        lambda:watches.inspect(sid,**revoked),lambda:watches.poll(sid,cursor=page['cursor'],**revoked),
        lambda:watches.create(payload,'original',**revoked)]:
        with pytest.raises((SubscriptionError,ResearchSnapshotError)) as error:operation()
        assert SECRET not in str(error.value)
    assert watches.list(**revoked)==[] and watches.pending_deliveries(**revoked)==[]
    assert watches.list(principal_id='alice',scopes={'knowledge:subscriptions:read'})==[]
    with pytest.raises(SubscriptionError):watches.poll(sid,principal_id='bob',scopes=full)
    # Dropping source scopes never restores old authorization through a token.
    assert snapshots.inspect(snapshot['token'],**auth)['session_id']==snapshot['session_id']
    conn.close()


def test_query_classification_filters_before_counts_and_namespace_export_denial(tmp_path):
    conn=duckdb.connect(str(tmp_path/'query.duckdb'))
    from src.kb.access_views import AccessViewStore
    from tests.unit.kb.test_access_views import rules
    policy=AccessViewStore(conn)
    policy.register_policy('research','p',1,rules(allowed_principals=['alice']),principal_id='admin',scopes={'operator'})
    for identity,classification,text in [('visible','public','Visible'),('hidden','secret',SECRET)]:
        policy.register_object('research','claim',identity,classification,'p',1,{'text':text},principal_id='admin',scopes={'operator'})
    filtered=policy.filter_query('research',[{'object_type':'claim','object_id':'hidden','score':100},
        {'object_type':'claim','object_id':'visible','score':1}],principal_id='alice',purpose='research',scopes={'knowledge:views:read'},limit=1)
    assert filtered['visible_count']==1 and filtered['next_offset'] is None and SECRET not in json.dumps(filtered)
    catalog=QueryCatalog([StaticQueryAdapter('public',[{'id':'public','text':'Visible'}],domains=['scientific']),
        StaticQueryAdapter('private',[{'id':'private','text':SECRET}],domains=['scientific'],required_scopes=['source:private:read'])])
    result=UnifiedQueryEngine(catalog).execute({'query':'text','scope':{'domains':['scientific']},'surfaces':['lexical']},scopes={'knowledge:read'})
    assert SECRET not in json.dumps(result) and len(result['items'])==1
    portable=PortableNamespaceStore(conn)
    portable.put_component('research','document','private',{'text':SECRET},sensitivity='restricted')
    with pytest.raises(PortableNamespaceError):portable.export('research',scopes={'knowledge:namespace:export'})
    # Namespace export is a privileged whole-namespace operation; its explicit
    # redaction policy is distinct from recipient-specific AccessView policies.
    exported=portable.export('research',scopes={'knowledge:namespace:export','namespace:research:read'},redaction={'sensitivities':['restricted']})
    assert SECRET not in json.dumps(exported)
    conn.close()


def test_package_closure_does_not_walk_inaccessible_dependencies():
    conn=duckdb.connect();store=ResearchPackageStore(conn)
    auth={'principal_id':'alice','scopes':{'knowledge:packages:write'}}
    # Use published scope constants so the test exercises actual API guards.
    from src.kb.research_packages import WRITE_SCOPE,READ_SCOPE
    auth['scopes']={WRITE_SCOPE}
    store.register_component('research','document','blocked',{'text':SECRET},access_status='inaccessible',dependencies=['hidden-descendant'],**auth)
    store.register_component('research','document','hidden-descendant',{'text':SECRET},**auth)
    closure=store.closure('research',['blocked'],scopes={READ_SCOPE})
    assert closure['members']==[] and SECRET not in json.dumps(closure)
    assert 'hidden-descendant' not in json.dumps(closure)
