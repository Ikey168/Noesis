import json,time,traceback
import duckdb
def run(name, fn):
    try: print(json.dumps({'probe':name,'result':fn()},default=str),flush=True)
    except Exception as e: print(json.dumps({'probe':name,'error':type(e).__name__+': '+str(e)}),flush=True)
def doc(i,content=None,**kw):
    return {'document_id':i,'source_type':'note','language':'en','ingested_at':1000,'content':content,'title':i,'url':'https://example.org/'+i,'source_id':'source-'+i,**kw}
def dedup():
    from src.ingestion.document_store import DocumentStore
    c=duckdb.connect(':memory:'); s=DocumentStore(c)
    a=s.upsert([doc('pdf-a',content_ref='https://example.org/a.pdf'),doc('pdf-b',content_ref='https://example.org/b.pdf')])
    b=s.upsert([doc('pub-a','Identical syndicated text'),doc('pub-b','Identical syndicated text')])
    result={'content_ref_only':{'inserted':a.inserted,'duplicate':a.duplicate,'invalid':a.invalid},'separate_sources_same_text':{'inserted':b.inserted,'duplicate':b.duplicate}}
    c.close(); return result
def stale():
    from src.ingestion.document_store import DocumentStore
    from src.ingestion.enrich import enrich_documents
    from src.ingestion.embed import embed_documents
    from src.ingestion.embedding_store import EmbeddingStore
    c=duckdb.connect(':memory:');s=DocumentStore(c)
    calls=[]
    class Provider:
        def __init__(self,name):self.model=name
        def embed_texts(self, texts):calls.extend(texts); return [[float(len(x)),1.] for x in texts]
        def name(self):return self.model
        def dim(self):return 2
    s.upsert([doc('revised','The result is good.')])
    e1=enrich_documents(c,analyzer=lambda d:{'sentiment_score':1.,'sentiment_label':'positive','topics':['old']})
    v1=embed_documents(c,Provider('model-a'))
    s.upsert([doc('revised','Correction: the result is bad and has been withdrawn.',ingested_at=2000)])
    e2=enrich_documents(c,analyzer=lambda d:{'sentiment_score':-1.,'sentiment_label':'negative','topics':['new']})
    v2=embed_documents(c,Provider('model-b'))
    result={'first_enriched':e1,'after_revision_enriched':e2,'first_embedded':v1,'after_revision_or_model_change_embedded':v2,'stored_embedding':EmbeddingStore(c).get('revised'),'stored_sentiment':c.execute('SELECT sentiment_label FROM document_enrichments').fetchone()[0]}
    c.close();return result
def entities():
    from src.knowledge_graph.foundation.resolution import EntityResolver
    from src.knowledge_graph.foundation.ontology import EntityType
    r=EntityResolver()
    a=r.resolve(EntityType.PERSON,'John Smith')
    b=r.resolve(EntityType.PERSON,'Jane Smith')
    ambiguous=r.resolve(EntityType.PERSON,'Smith')
    r2=EntityResolver()
    b2=r2.resolve(EntityType.PERSON,'Jane Smith');a2=r2.resolve(EntityType.PERSON,'John Smith')
    other=r2.resolve(EntityType.PERSON,'Smith')
    return {'distinct_full_names':a.node_id!=b.node_id,'surname_first_order':ambiguous.name,'surname_reversed_order':other.name}
def deadline():
    from src.kb.unified_query import StaticQueryAdapter,QueryCatalog,UnifiedQueryEngine
    class Slow(StaticQueryAdapter):
        def query(self,request,*,scopes):time.sleep(.25); return super().query(request,scopes=scopes)
    engine=UnifiedQueryEngine(QueryCatalog([Slow('slow',[],domains=['test'])]))
    request={'query':'test','scope':{'domains':['test']},'surfaces':['lexical'],'budgets':{'timeout_ms':20,'max_results':10}}
    start=time.monotonic();result=engine.execute(request,scopes={'knowledge:read'})
    return {'budget_ms':20,'wall_ms':round((time.monotonic()-start)*1000),'failures':result.get('failures')}
def watermark():
    from src.kb.workflows import WorkflowStore,reference_manifest,STAGE_ORDER
    c=duckdb.connect(':memory:')
    class FailOnce(WorkflowStore):
        failed=False
        def _commit_watermark(self,*a,**kw):
            if not self.failed:self.failed=True;raise RuntimeError('injected crash before watermark')
            return super()._commit_watermark(*a,**kw)
    store=FailOnce(c); manifest=reference_manifest('probe')
    handlers={name:lambda ctx,state:dict(state) for name in STAGE_ORDER}
    try: store.execute(manifest,handlers,{'documents':[]},run_key='crash')
    except RuntimeError: pass
    run_id=c.execute('SELECT run_id FROM knowledge_workflow_runs').fetchone()[0]
    first=store.inspect(run_id)
    second=store.execute(manifest,handlers,{'documents':[]},run_key='crash')
    result={'first_status':first['status'],'resumed_status':second['status'],'resumed_watermark':second['watermark'],'completed_stages':[x['stage'] for x in second['receipts']]}
    c.close();return result
def chunks():
    import importlib.util,sys
    spec=importlib.util.spec_from_file_location('noesis_probe_chunking','services/rag/chunking.py')
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    TextChunker,ChunkConfig,SplitStrategy=module.TextChunker,module.ChunkConfig,module.SplitStrategy
    text='Repeated introduction with the same first fifty characters. First finding.\n\nRepeated introduction with the same first fifty characters. Second finding.'
    chunker=TextChunker(ChunkConfig(max_chars=85,overlap_chars=0,min_chunk_chars=1,preserve_sentences=False,split_on=SplitStrategy.PARAGRAPH))
    chunks=chunker.chunk_text(text)
    return [{'start':x.start_offset,'end':x.end_offset,'matches_original':text[x.start_offset:x.end_offset]==x.text,'text':x.text} for x in chunks]
for name,fn in [('document_identity',dedup),('stale_derivatives',stale),('ambiguous_entities',entities),('query_deadline',deadline),('watermark_crash',watermark),('chunk_offsets',chunks)]:run(name,fn)
