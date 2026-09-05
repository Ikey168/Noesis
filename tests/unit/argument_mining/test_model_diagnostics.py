import pytest
from src.argument_mining.model_diagnostics import diagnose


def test_stance_abstention_confusions_and_absolute_readiness():
    rows=[{'labels':['critical'],'scores':[.2,.3,.25,.25],'source_type':'paper'}]
    result=diagnose(rows,['supportive','critical','neutral','ambiguous'],task='stance',threshold=.4)
    assert result['unsupported']==1 and result['coverage']==0 and result['selective_accuracy'] is None
    assert result['confusions']==[{'expected':'critical','predicted':'unsupported','n':1}]
    assert not result['task_ready'] and not result['absolute_metric_targets_met']
    assert result['by_domain']['unknown']['n']==1


def test_frames_multilabel_false_negatives_and_uncertain_is_not_other():
    rows=[{'labels':['economic','scientific'],'scores':[.6,.3,.1],'source_type':'paper'}]
    result=diagnose(rows,['economic','scientific','other'],task='frames',threshold=.45)
    assert result['per_class']['scientific']['recall']==0 and result['exact_accuracy']==0
    uncertain=diagnose(rows,['economic','scientific','other'],task='frames',threshold=.9)
    assert uncertain['unsupported']==1 and uncertain['per_class']['other']['precision']==0
    rows[0]['scores'][0]=float('nan')
    with pytest.raises(ValueError,match='finite'):
        diagnose(rows,['economic','scientific','other'],task='frames')
