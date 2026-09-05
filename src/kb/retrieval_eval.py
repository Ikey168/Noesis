"""Frozen-judgment retrieval comparison, separate from structural query checks."""
import hashlib
import json
import math
from pathlib import Path

MODES = {'lexical', 'semantic', 'fusion', 'reranked'}


def evaluate_retrieval(manifest_path, *, allow_fixture=False):
    path=Path(manifest_path)
    def read(name, expected=None):
        target=(path.parent/name).resolve()
        if not target.is_relative_to(path.parent.resolve()) or target.stat().st_size>16*1024*1024:
            raise ValueError('evaluation input must be a bounded local manifest member')
        raw=target.read_bytes()
        if expected and hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError('frozen evaluation member changed')
        return json.loads(raw)
    manifest=read(path.name)
    if manifest.get('contract')!='noesis-retrieval-eval-v1':
        raise ValueError('unsupported evaluation contract')
    origin=manifest.get('label_origin')
    if origin!='human' and not (allow_fixture and origin=='fixture'):
        raise ValueError('independent human judgments required')
    if origin=='human' and (len(set(manifest.get('reviewers',[])))<2 or not manifest.get('adjudication_record')):
        raise ValueError('human provenance and adjudication record required')
    queries=manifest['queries']; cutoffs=manifest['cutoffs']
    if not 1<=len(queries)<=1000 or len({q['id'] for q in queries})!=len(queries) or any(not q.get('domain') or not q.get('text') for q in queries):
        raise ValueError('unique bounded queries with domains required')
    if not cutoffs or len(cutoffs)>20 or any(type(k) is not int or not 1<=k<=1000 for k in cutoffs) or max(cutoffs)<=20:
        raise ValueError('bounded cutoffs must include a value above 20')
    if set(manifest['runs'])!=MODES:
        raise ValueError('compare lexical, semantic, fusion and reranked runs')
    import ir_measures as ir
    qrels=read(manifest['qrels']['path'],manifest['qrels']['sha256'])
    if set(qrels)!={q['id'] for q in queries} or any(not rows or len(rows)>10000 or any(type(v) is not int or not 0<=v<=3 for v in rows.values()) for rows in qrels.values()):
        raise ValueError('every frozen query needs bounded graded judgments')
    measures=[measure@k for k in cutoffs for measure in (ir.R,ir.nDCG,ir.RR,ir.Judged)]
    results={}
    for mode,member in sorted(manifest['runs'].items()):
        run=read(member['path'],member['sha256'])
        if set(run)!=set(qrels):
            raise ValueError('missing query outcomes cannot silently improve means')
        scores={}; latencies=[]; costs=[]; partial=[]
        for qid,outcome in run.items():
            rows=outcome['results']; latency=outcome['latency_ms']; cost=outcome.get('usd_micros')
            if len(rows)>1000 or len({r['id'] for r in rows})!=len(rows) or any(not math.isfinite(r['score']) for r in rows):
                raise ValueError('bounded unique finite scored results required')
            if not isinstance(latency,(float,int)) or not math.isfinite(latency) or latency<0 or cost is not None and (type(cost) is not int or cost<0):
                raise ValueError('invalid observed latency/cost')
            if outcome.get('status') not in {'complete','partial','unavailable'}:
                raise ValueError('explicit source outcome required')
            if outcome['status']!='complete':
                partial.append(qid)
            scores[qid]={r['id']:float(r['score']) for r in rows}; latencies.append(latency); costs.append(cost)
        per_query={q['id']:{} for q in queries}
        # ir-measures 0.4.3 Judged divides by zero for an explicit empty
        # ranking. Omitted rankings receive the evaluator's zero metrics.
        for metric in ir.iter_calc(measures,qrels,{q: rows for q,rows in scores.items() if rows}):
            per_query[metric.query_id][str(metric.measure)]=metric.value
        average=lambda ids:{str(m):sum(per_query[q][str(m)] for q in ids)/len(ids) for m in measures}
        ordered=sorted(latencies)
        results[mode]={'metrics':average(list(qrels)),'per_query':per_query,
            'domains':{domain:average([q['id'] for q in queries if q['domain']==domain]) for domain in sorted({q['domain'] for q in queries})},
            'latency_ms':{'p50':ordered[math.ceil(len(ordered)*.5)-1],'p95':ordered[math.ceil(len(ordered)*.95)-1]},
            'usd_micros':sum(costs) if all(v is not None for v in costs) else None,
            'partial_queries':sorted(partial),'configuration':member['configuration']}
    return {'contract':'noesis-retrieval-eval-result-v1','manifest_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'label_origin':origin,'human_provenance_independently_verified':False,'runs':results,
        'limitations':['Human provenance is supplied metadata; an independent audit is still required.',
            'Recall is relative to the frozen judgments; unjudged candidates are exposed by Judged@k.',
            'No task-readiness claim follows from fixture metrics or a partial provider run.']}
