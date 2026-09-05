"""Compare frozen stance/frame configurations with real pinned local NLI scores."""
import argparse
import hashlib
import json
import platform
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from src.argument_mining.model_diagnostics import diagnose


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--out',required=True);parser.add_argument('--cache',required=True)
    args=parser.parse_args()
    import torch
    import pyarrow.parquet as pq
    from src.kb.nli import TransformersNLI
    from src.argument_mining.models import StanceClassifier
    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.model_registry import resolved_pins
    torch.set_num_threads(2)
    backend=TransformersNLI(); cachepath=Path(args.cache)
    cached=json.loads(cachepath.read_text()) if cachepath.exists() else {}
    identity=hashlib.sha256(json.dumps({'pins':resolved_pins(),'nli_code':hashlib.sha256((ROOT/'src/kb/nli.py').read_bytes()).hexdigest()},sort_keys=True).encode()).hexdigest()
    if cached.get('identity')!=identity: cached={'identity':identity,'scores':{}}
    cache_entries_at_start=len(cached['scores'])
    results={}; hashes={}; timings={}; model_calls=0
    for task,name,templates in [('stance','stance',StanceClassifier.NLI_TEMPLATES),('frames','frames',FrameClassifier.NLI_TEMPLATES)]:
        path=ROOT/'data/argument_mining'/f'{name}.parquet';hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
        inputs=[row for row in pq.read_table(path).to_pylist() if row['split']=='test']
        rows=[];start=time.monotonic()
        for index,row in enumerate(inputs):
            text=row['text'];topic=row.get('topic','the issue')
            pairs=[(text,template.format(topic=topic)) for template in templates.values()]
            key=hashlib.sha256(json.dumps(pairs).encode()).hexdigest()
            if key not in cached['scores']:
                cached['scores'][key]=backend.entailment_scores(pairs,batch_size=8);model_calls+=1
            labels=[row['stance']] if task=='stance' else json.loads(row['frames']) if isinstance(row['frames'],str) else list(row['frames'])
            rows.append({'labels':labels,'scores':cached['scores'][key],'source_type':row['source_type'],'domain':row.get('domain','unknown')})
            if index%64==0:
                cachepath.write_text(json.dumps(cached,sort_keys=True));print(f'{task}: {index+1}/{len(inputs)}',flush=True)
        cachepath.write_text(json.dumps(cached,sort_keys=True));timings[task]=time.monotonic()-start
        results[task]={'baseline':diagnose(rows,list(templates),task=task,legacy=True,dominant_only=task=='frames')}
        if task=='frames':
            results[task]['existing_thresholds_multilabel']=diagnose(rows,list(templates),task=task,legacy=True)
        for threshold in [.35,.45,.55]:
            results[task][f'unsupported_threshold_{threshold}']=diagnose(rows,list(templates),task=task,threshold=threshold,share_floor=.4)
    report={'contract':'noesis-mining-config-experiment-v1','dataset_hashes':hashes,'split':'test','label_origin':'existing benchmark; not independently collected EX-05 human labels',
        'model_pins':resolved_pins(),'runtime_identity':identity,'python':platform.python_version(),'threads':2,
        'elapsed_seconds':timings,'uncached_model_calls':model_calls,'cache_reused':bool(model_calls<sum(v['baseline']['n'] for v in results.values())),
        'cache_entries_at_start':cache_entries_at_start,
        'evaluation_code_sha256':hashlib.sha256((ROOT/'src/argument_mining/model_diagnostics.py').read_bytes()).hexdigest(),
        'external_inference_usd_micros':0,'local_compute_cost':'not priced','results':results,'selected_replacement':None,
        'limitations':['Fixed threshold candidates were not fitted to this test split. No production model or threshold was changed.',
            'Readiness targets are explicit provisional engineering criteria, separate from historical regression gates.',
            'Source type is available; absent domain annotations remain unknown.',
            'The claim detector is outside this experiment. Human validation remains required.']}
    Path(args.out).write_text(json.dumps(report,indent=2,sort_keys=True)+'\n')

if __name__=='__main__':main()
