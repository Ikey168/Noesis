"""Bounded complete-source NLI windows with exact character locators."""
from dataclasses import asdict


def classify_evidence(backend,premise,hypothesis,*,overlap_tokens=32,max_windows=64):
    if not isinstance(premise,str) or not isinstance(hypothesis,str) or len(premise)>262144 or len(hypothesis)>262144:
        raise ValueError('bounded premise and hypothesis strings required')
    if type(max_windows) is not int or not 1<=max_windows<=64 or type(overlap_tokens) is not int or not 0<=overlap_tokens<=128:
        raise ValueError('invalid NLI window limits')
    tokenizer=backend._tokenizer
    hypothesis_size=len(tokenizer(hypothesis,add_special_tokens=False)['input_ids'])
    budget=512-hypothesis_size-tokenizer.num_special_tokens_to_add(pair=True)
    if budget<=overlap_tokens:
        raise ValueError('hypothesis leaves no bounded premise window')
    encoded=tokenizer(premise,add_special_tokens=False,return_offsets_mapping=True,truncation=False)
    offsets=[tuple(v) for v in encoded['offset_mapping'] if v[1]>v[0]]
    if not offsets:
        return {'status':'insufficient_evidence','label':'neutral','confidence':0.0,'windows':[],
            'coverage_complete':not premise.strip(),'prediction_mode':backend.prediction_mode}
    planned=[]; cursor=0
    while cursor<len(offsets):
        end=min(cursor+budget,len(offsets))
        # A substring can tokenize differently at its boundary. Validate the
        # actual pair and shrink before inference, preserving forward progress.
        start_char=0 if cursor==0 else offsets[cursor][0]
        while end>cursor:
            end_char=len(premise) if end==len(offsets) else offsets[end-1][1]
            ids=tokenizer(premise[start_char:end_char],hypothesis,truncation=False)['input_ids']
            if len(ids)<=512:
                break
            end-=1
        if end<=cursor or end-cursor<=overlap_tokens and end<len(offsets):
            raise ValueError('tokenizer cannot form a progressing bounded window')
        planned.append((start_char,end_char))
        if len(planned)>max_windows:
            raise ValueError('complete evidence exceeds max_windows; no partial inference published')
        if end==len(offsets):
            break
        cursor=end-overlap_tokens
    windows=[{'start':start,'end':end,**asdict(backend.classify(premise[start:end],hypothesis))} for start,end in planned]
    labels={v['label'] for v in windows}
    conflict={'entailment','contradiction'}<=labels
    label='neutral' if conflict else 'contradiction' if 'contradiction' in labels else 'entailment' if 'entailment' in labels else 'neutral'
    confidence=0.0 if conflict else max(v['confidence'] for v in windows if v['label']==label)
    return {'status':'conflicting_evidence' if conflict else 'assessed','label':label,'confidence':confidence,
        'windows':windows,'coverage_complete':True,'prediction_mode':backend.prediction_mode,
        'aggregation':'any decisive window; opposing decisive windows abstain',
        'limitations':['Window aggregation does not establish cross-window compositional entailment or calibrated confidence.']}
