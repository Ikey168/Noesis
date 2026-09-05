"""Fixed-label diagnostics; regression metrics do not imply task readiness."""
from collections import Counter
import math


def prf(truth,prediction,labels):
    classes={}
    for label in labels:
        tp=sum(label in a and label in b for a,b in zip(truth,prediction))
        fp=sum(label not in a and label in b for a,b in zip(truth,prediction))
        fn=sum(label in a and label not in b for a,b in zip(truth,prediction))
        p=tp/(tp+fp) if tp+fp else 0; r=tp/(tp+fn) if tp+fn else 0
        classes[label]={'precision':p,'recall':r,'f1':2*p*r/(p+r) if p+r else 0,'support':tp+fn}
    return {'per_class':classes,'macro_f1':sum(v['f1'] for v in classes.values())/len(labels),
        'exact_accuracy':sum(a==b for a,b in zip(truth,prediction))/len(truth),'n':len(truth)}


def diagnose(rows,labels,*,task,threshold=.45,share_floor=.3,legacy=False,dominant_only=False):
    if not rows or task not in {'stance','frames'}:
        raise ValueError('nonempty supported diagnostic task required')
    if any(len(row['scores'])!=len(labels) or any(not math.isfinite(v) or not 0<=v<=1 for v in row['scores']) for row in rows):
        raise ValueError('finite per-label scores required')
    truths=[]; predictions=[]; confidence=[]; brier=[]; confusion=Counter()
    for row in rows:
        scores=dict(zip(labels,row['scores'])); truth=set(row['labels'])
        if task=='stance' and not truth<=set(labels): raise ValueError('unknown frozen stance label')
        best=max(labels,key=lambda label:scores[label]); total=sum(scores.values())
        if task=='stance':
            if len(truth)!=1: raise ValueError('stance needs one label')
            normalized={key:score/total if total else 1/len(labels) for key,score in scores.items()}
            abstain=scores[best] < (.4 if legacy else threshold) or normalized[best]<share_floor
            prediction={'neutral'} if abstain and legacy else set() if abstain else {best}
            conf=.5 if abstain and legacy else normalized[best]
            brier.append(sum((normalized[label]-(label in truth))**2 for label in labels))
            confusion[(next(iter(truth)),next(iter(prediction),'unsupported'))]+=1
        else:
            prediction={label for label,score in scores.items() if score>=(.35 if legacy and label in {'political','humanitarian'} else threshold)}
            if not prediction and legacy: prediction={'other'}
            if dominant_only and prediction:
                prediction={max(prediction,key=lambda label:scores[label])}
            conf=max(scores.values()); brier.append(sum((scores[label]-(label in truth))**2 for label in labels)/len(labels))
        truths.append(truth); predictions.append(prediction); confidence.append(conf)
    result=prf(truths,predictions,labels)
    result['out_of_ontology_labels']=dict(Counter(label for truth in truths for label in truth-set(labels)))
    result['out_of_ontology_rows']=sum(bool(truth-set(labels)) for truth in truths)
    if task=='frames':
        result['per_label_calibration']={}
        for label_index,label in enumerate(labels):
            buckets=[]
            for index in range(10):
                selected=[i for i,row in enumerate(rows) if min(9,int(row['scores'][label_index]*10))==index]
                if selected:
                    buckets.append({'lower':index/10,'n':len(selected),
                        'mean_score':sum(rows[i]['scores'][label_index] for i in selected)/len(selected),
                        'observed_fraction':sum(label in truths[i] for i in selected)/len(selected)})
            result['per_label_calibration'][label]={'brier':sum((row['scores'][label_index]-(label in truth))**2 for row,truth in zip(rows,truths))/len(rows),
                'ece':sum(b['n']*abs(b['mean_score']-b['observed_fraction']) for b in buckets)/len(rows),'bins':buckets}
    accepted=[i for i,p in enumerate(predictions) if p]
    bins=[]
    for index in range(10):
        selected=[i for i in accepted if min(9,int(confidence[i]*10))==index]
        if selected:
            bins.append({'lower':index/10,'n':len(selected),'confidence':sum(confidence[i] for i in selected)/len(selected),
                'accuracy':sum(truths[i]==predictions[i] for i in selected)/len(selected)})
    result.update(coverage=len(accepted)/len(rows),unsupported=len(rows)-len(accepted),
        selective_accuracy=sum(truths[i]==predictions[i] for i in accepted)/len(accepted) if accepted else None,
        brier=sum(brier)/len(rows),calibration_bins=bins,
        ece=sum(b['n']*abs(b['confidence']-b['accuracy']) for b in bins)/len(accepted) if accepted else None,
        calibration_basis='normalized stance scores' if task=='stance' else 'max frame score versus exact label-set accuracy (descriptive only)',
        confusions=[{'expected':a,'predicted':b,'n':n} for (a,b),n in sorted(confusion.items())],
        by_source={},by_domain={})
    for field,target in [('source_type','by_source'),('domain','by_domain')]:
        for group in sorted({row.get(field,'unknown') for row in rows}):
            indices=[i for i,row in enumerate(rows) if row.get(field,'unknown')==group]
            result[target][group]=prf([truths[i] for i in indices],[predictions[i] for i in indices],labels)
    result['readiness_targets']={'macro_f1':.7 if task=='stance' else .65,'minimum_class_recall':.7 if task=='stance' else .5,'coverage':.8}
    targets=result['readiness_targets']
    result['absolute_metric_targets_met']=result['macro_f1']>=targets['macro_f1'] and result['coverage']>=targets['coverage'] and all(v['recall']>=targets['minimum_class_recall'] for v in result['per_class'].values())
    result['task_ready']=False
    result['readiness_blocker']='Independent EX-05 human validation and domain readiness review are still required'
    return result
