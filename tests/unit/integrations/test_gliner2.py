import copy
from types import SimpleNamespace

import pytest

from src.integrations.common import IntegrationError
from src.integrations.entities import GLiNER2Extractor


def extractor(monkeypatch, output):
    monkeypatch.setattr("src.integrations.common.version", lambda _: "2.0.0")
    adapter = GLiNER2Extractor.__new__(GLiNER2Extractor)
    adapter.threshold = 0.5
    adapter.max_chars = 8000
    adapter.max_tokens = 512
    calls = []

    def extract(text, schema, **kwargs):
        calls.append((text, schema, kwargs))
        return copy.deepcopy(output)

    adapter.model = SimpleNamespace(
        extract_entities=extract,
        processor=SimpleNamespace(
            tokenizer=SimpleNamespace(encode=lambda text, **_: text.split())
        ),
    )
    return adapter, calls


def test_original_unicode_overlapping_mentions_and_replay(monkeypatch):
    text = "🌍  Universität für Berlin\nBerlin"
    outer = "Universität für Berlin"
    start = text.index(outer)
    occurrences = [text.index("Berlin"), text.rindex("Berlin")]
    raw = {
        "entities": {
            "ORGANIZATION": [
                {
                    "text": outer,
                    "start": start,
                    "end": start + len(outer),
                    "confidence": 0.8,
                },
                *[
                    {"text": "Berlin", "start": p, "end": p + 6, "confidence": 0.6}
                    for p in occurrences
                ],
            ]
        }
    }
    adapter, calls = extractor(monkeypatch, raw)
    kwargs = {"language": "de", "article_id": "article:1", "revision_id": "revision:1"}
    run = adapter.extract(text, **kwargs)
    assert len(run["entities"]) == 3
    assert all(
        text[e["start_position"] : e["end_position"]] == e["text"]
        for e in run["entities"]
    )
    assert run == adapter.extract(text, **kwargs)
    assert calls[0][0] == text
    assert calls[0][2]["overlap_policy"] == "allow"
    assert adapter.extract(text, **{**kwargs, "revision_id": "revision:2"}) != run
    assert (
        run["receipt"]["request"]["confidence_semantics"] == "uncalibrated_model_score"
    )


@pytest.mark.parametrize(
    "override,code",
    [
        ({"language": "fr"}, "unsupported_language"),
        ({"labels": ["PERSON"]}, "unsupported_label"),
        ({"labels": ["AUTHORITY", "AUTHORITY"]}, "unsupported_label"),
        ({"revision_id": None}, "missing_source"),
        ({"text": "x" * 8001}, "input_limit"),
        ({"text": "word " * 500}, "token_limit"),
    ],
)
def test_fail_before_model(monkeypatch, override, code):
    adapter, calls = extractor(monkeypatch, {"entities": {}})
    kwargs = {"text": "Berlin", "language": "de", "article_id": "a", "revision_id": "r"}
    with pytest.raises(IntegrationError) as error:
        adapter.extract(**{**kwargs, **override})
    assert error.value.code == code
    assert not calls


@pytest.mark.parametrize(
    "item",
    [
        {"text": "Berlin", "start": 1, "end": 7, "confidence": 0.9},
        {"text": "Berlin", "start": 0, "end": 6, "confidence": float("nan")},
        {"text": "Berlin", "start": True, "end": 6, "confidence": 0.9},
    ],
)
def test_reject_invalid_source_spans_and_scores(monkeypatch, item):
    adapter, _ = extractor(monkeypatch, {"entities": {"AUTHORITY": [item]}})
    with pytest.raises(IntegrationError):
        adapter.extract("Berlin", language="de", article_id="a", revision_id="r")


def test_ner_surface_uses_original_source_and_provenance(monkeypatch):
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    from src.nlp.ner_processor import NERProcessor

    adapter, calls = extractor(
        monkeypatch,
        {
            "entities": {
                "AUTHORITY": [
                    {"text": "Amt", "start": 3, "end": 6, "confidence": 0.8},
                ]
            }
        },
    )
    monkeypatch.setattr(
        "src.integrations.entities.GLiNER2Extractor", lambda **_: adapter
    )
    ner = NERProcessor(backend="gliner2", language="de")
    result = ner.extract_entities("🌍  Amt", "article", revision_id="r1")
    assert calls[0][0] == "🌍  Amt"
    assert result[0]["start_position"] == 3
    assert result[0]["provenance"]["revision_id"] == "r1"
    assert ner.stats["total_entities_extracted"] == 1
