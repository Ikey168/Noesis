import hashlib
import json
import pytest
from src.kb.retrieval_eval import evaluate_retrieval,MODES


def test_frozen_ir_metrics_domains_missing_runs_and_fixture_honesty(tmp_path):
    def member(name,value):
        raw=json.dumps(value).encode(); (tmp_path/name).write_bytes(raw)
        return {'path':name,'sha256':hashlib.sha256(raw).hexdigest()}
    qrels={'q':{'relevant':1,'irrelevant':0},'empty':{'missed':1}}
    runs={mode:{**member(mode+'.json',{'q':{'results':[{'id':'irrelevant','score':2},{'id':'relevant','score':1}],
        'latency_ms':10,'usd_micros':0,'status':'complete'},'empty':{'results':[],'latency_ms':30,'status':'unavailable'}}),
        'configuration':{'fixture':True}} for mode in MODES}
    manifest={'contract':'noesis-retrieval-eval-v1','label_origin':'fixture','queries':[{'id':'q','domain':'science','text':'query'},
        {'id':'empty','domain':'economics','text':'missing'}],'cutoffs':[10,50],'qrels':member('qrels.json',qrels),'runs':runs}
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match='human'):
        evaluate_retrieval(path)
    result=evaluate_retrieval(path,allow_fixture=True)
    lexical=result['runs']['lexical']
    assert lexical['metrics']['RR@50']==.25 and lexical['metrics']['R@50']==.5
    assert lexical['domains']['science']['RR@50']==.5
    assert lexical['partial_queries']==['empty'] and lexical['usd_micros'] is None
    assert not result['human_provenance_independently_verified']
    (tmp_path/'lexical.json').write_text('{}')
    with pytest.raises(ValueError,match='changed'):
        evaluate_retrieval(path,allow_fixture=True)
