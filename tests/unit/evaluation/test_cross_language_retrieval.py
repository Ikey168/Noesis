import copy

import pytest

from scripts.evaluate_cross_language_retrieval import add_cross_language


def rows():
    return [
        {
            "id": f"xnli:{language}:test:{i}",
            "split": "test",
            "language": language,
            "labels": ["entailment"],
            "group_id": str(i),
            "premise": f"{language} premise {i}",
            "hypothesis": f"{language} hypothesis {i}",
            "label_origin": "independent-human",
            "source": "facebook/xnli",
        }
        for i in range(8)
        for language in ("de", "en")
    ]


def test_cross_language_derivation_preserves_translations_and_support_targets():
    source = rows()
    result = add_cross_language(
        {"documents": [], "queries": []}, source, lambda _: True
    )
    assert len(result["documents"]) == len(result["queries"]) == 8
    assert all(r["text"].startswith("en premise") for r in result["documents"])
    assert all(r["text"].startswith("de hypothesis") for r in result["queries"])
    assert all(r["language_pair"] == "de-en" for r in result["queries"])
    assert set(result["queries"][0]["judgments"]) == {result["documents"][0]["id"]}
    assert (
        "not a separately annotated retrieval judgment"
        in result["queries"][0]["label_origin"]
    )
    assert source == rows()


def test_model_labels_or_misaligned_translations_cannot_be_relabelled_human():
    source = rows()
    source[1]["label_origin"] = "model"
    with pytest.raises(ValueError, match="human label provenance"):
        add_cross_language({"documents": [], "queries": []}, source, lambda _: True)
    source = copy.deepcopy(rows())
    source[1]["labels"] = ["contradiction"]
    with pytest.raises(ValueError, match="aligned human translations"):
        add_cross_language({"documents": [], "queries": []}, source, lambda _: True)
