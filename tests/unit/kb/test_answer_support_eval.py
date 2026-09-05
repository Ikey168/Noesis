import pytest
from src.kb.answer_support_eval import evaluate_support,KINDS


def test_semantic_errors_stay_separate_from_refusal_and_fixture_provenance():
    cases=[{'id':str(i),'kind':kind,'source_revision_id':'revision:fixture','locator':{'start':0,'end':8},
        'judgment':{'relevant':True,'entailment':'contradiction','should_refuse':True}} for i,kind in enumerate(sorted(KINDS))]
    predictions={c['id']:{'entailment':'entailment','relevant':None,'refused':True} for c in cases}
    with pytest.raises(ValueError,match='human'):
        evaluate_support(cases,predictions,label_origin='fixture')
    result=evaluate_support(cases,predictions,label_origin='fixture',allow_fixture=True)
    assert result['metrics']['support_correct']==0 and result['metrics']['refusal_correct']==1
    assert result['metrics']['relevance_unavailable']==1 and not result['audit_complete']
    assert all(c['structural_evaluation'] is None for c in result['cases'])
