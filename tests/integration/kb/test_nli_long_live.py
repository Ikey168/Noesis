"""Opt-in real-model full-span behavior, not human entailment validation."""
import hashlib
import json
import os
from pathlib import Path
import pytest


@pytest.mark.skipif(os.environ.get('NOESIS_LIVE_NLI')!='1',reason='requires explicitly enabled cached NLI model')
def test_real_nli_complete_long_span():
    import torch
    from src.kb.nli import TransformersNLI
    torch.set_num_threads(2)
    backend=TransformersNLI()
    premise='These records describe routine accounting procedures. '*180+'The final observation reports that the trial was cancelled.'
    hypothesis='The trial was cancelled.'
    with pytest.raises(ValueError,match='512'):
        backend.classify(premise,hypothesis)
    result=backend.classify_evidence(premise,hypothesis)
    assert result['coverage_complete'] and len(result['windows'])>1
    assert result['windows'][-1]['end']==len(premise)
    assert all(v['prediction_mode'].startswith('zero-shot:') for v in result['windows'])
    evidence={'kind':'real-model behavior on generated stress text','human_quality_validation':False,
        'premise_sha256':hashlib.sha256(premise.encode()).hexdigest(),'premise_characters':len(premise),
        'result':result}
    if os.environ.get('NOESIS_NLI_EVIDENCE_PATH'):
        Path(os.environ['NOESIS_NLI_EVIDENCE_PATH']).write_text(json.dumps(evidence,indent=2,sort_keys=True)+'\n')
