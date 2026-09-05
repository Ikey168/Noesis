"""Human-auditable factual support metrics independent of schema compliance."""
from collections import Counter
from src.kb.research_projects import _hash

KINDS={'unsupported_citation','contradiction','correction','incomplete_coverage','appropriate_refusal'}
LABELS={'entailment','contradiction','neutral','unavailable'}


def evaluate_support(cases,predictions,*,label_origin,allow_fixture=False):
    if label_origin!='human' and not (label_origin=='fixture' and allow_fixture):
        raise ValueError('human support judgments required')
    if not 1<=len(cases)<=1000 or len({c['id'] for c in cases})!=len(cases) or {c['kind'] for c in cases}!=KINDS:
        raise ValueError('unique bounded cases must cover all required failure/refusal kinds')
    if set(predictions)!={c['id'] for c in cases}:
        raise ValueError('every case needs an explicit prediction or unavailable result')
    audited=[]; metrics=Counter(); confusions=Counter(); rows=[]
    for case in cases:
        expected=case['judgment']; actual=predictions[case['id']]
        if expected['entailment'] not in LABELS or actual['entailment'] not in LABELS:
            raise ValueError('unknown entailment label')
        for value in (expected['relevant'],expected['should_refuse'],actual['refused']):
            if type(value) is not bool:
                raise ValueError('explicit relevance/refusal booleans required')
        if actual.get('relevant') is not None and type(actual['relevant']) is not bool:
            raise ValueError('relevance prediction must be boolean or unavailable')
        if not case.get('source_revision_id') or not isinstance(case.get('locator'),dict):
            raise ValueError('pinned source revision and locator required')
        if label_origin=='human' and not case.get('annotator_id'):
            raise ValueError('human annotation provenance required')
        audit=case.get('audit')
        if audit and audit.get('reviewer_id') and audit['reviewer_id']!=case.get('annotator_id') and audit.get('judgment_hash')==_hash(expected):
            audited.append(case['id'])
        confusions[(expected['entailment'],actual['entailment'])]+=1
        support=actual['entailment']==expected['entailment']
        relevance=actual.get('relevant')==expected['relevant']
        refusal=actual['refused']==expected['should_refuse']
        metrics['support_correct']+=support; metrics['relevance_correct']+=relevance; metrics['refusal_correct']+=refusal
        metrics['relevance_unavailable']+=actual.get('relevant') is None
        schema=None
        if actual.get('answer') is not None:
            from src.kb.answer_eval import evaluate_answer
            schema=evaluate_answer(actual['answer'])
        rows.append({'id':case['id'],'kind':case['kind'],'support_correct':support,'relevance_correct':relevance,
            'refusal_correct':refusal,'structural_evaluation':schema,'source_revision_id':case['source_revision_id'],
            'locator':case['locator'],'audit_present':case['id'] in audited})
    return {'contract':'noesis-answer-support-eval-v1','label_origin':label_origin,
        'input_hash':_hash([cases,predictions]),'n':len(cases),'metrics':{k:v/len(cases) for k,v in metrics.items()},
        'confusions':[{'expected':a,'predicted':b,'n':n} for (a,b),n in sorted(confusions.items())],
        'human_audit_subset':audited,'audit_complete':label_origin=='human' and bool(audited),
        'audit_identity_independently_verified':False,'cases':rows,
        'limitations':['Structural compliance is scored separately and does not establish relevance or entailment.',
            'The supplied human audit provenance requires independent verification.']}
