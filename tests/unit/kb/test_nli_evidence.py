import re
from types import SimpleNamespace
import pytest
from src.kb.nli import NLIResult,TransformersNLI
from src.kb.nli_evidence import classify_evidence


class Tokenizer:
    def num_special_tokens_to_add(self,pair): return 3
    def __call__(self,text,other=None,**kwargs):
        offsets=[(m.start(),m.end()) for m in re.finditer(r'\S+',text)]
        return {'input_ids':list(range(len(offsets)+(len(other.split())+3 if other is not None else 0))),
            'offset_mapping':offsets}


class Backend:
    _tokenizer=Tokenizer(); prediction_mode='fixture'
    calls=[]
    def classify(self,premise,hypothesis):
        self.calls.append(premise)
        return NLIResult('contradiction' if 'DENIED' in premise else 'entailment' if 'SUPPORTED' in premise else 'neutral',.9,'fixture')


def test_full_span_tail_conflict_and_no_work_when_window_budget_insufficient():
    backend=Backend(); backend.calls=[]
    premise='Background. '*1100+'SUPPORTED'
    result=classify_evidence(backend,premise,'Claim')
    assert result['label']=='entailment' and result['coverage_complete']
    assert result['windows'][-1]['end']==len(premise)
    assert all(len((premise[w['start']:w['end']]).split())+4<=512 for w in result['windows'])
    conflict=classify_evidence(backend,'DENIED '+premise,'Claim')
    assert conflict['status']=='conflicting_evidence' and conflict['confidence']==0
    backend.calls=[]
    with pytest.raises(ValueError,match='max_windows'):
        classify_evidence(backend,premise,'Claim',max_windows=1)
    assert backend.calls==[]


def test_direct_pair_fails_before_model_on_overlength_or_unavailable(monkeypatch):
    backend=object.__new__(TransformersNLI)
    backend._tokenizer=lambda *a,**k:{'input_ids':SimpleNamespace(shape=(1,513))}
    with pytest.raises(ValueError,match='512'):
        backend._bounded_inputs(['long'],['claim'])
    from src.argument_mining import model_registry
    monkeypatch.setattr(model_registry,'cached_model_path',lambda name:None)
    with pytest.raises(RuntimeError,match='cache'):
        TransformersNLI()
