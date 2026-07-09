"""
Unit tests for the quantitative claim extractor (A3), including a precision
spot-check over a labeled sample spanning all six source types.
"""

from __future__ import annotations

from services.ingest.common.document_model import Document
from src.argument_mining.quantities import QuantAssertion, QuantityExtractor


def _one(text: str):
    out = QuantityExtractor().extract(text)
    return out[0] if out else None


def test_extracts_direction_value_unit_period_geography():
    a = _one("Unemployment in Germany rose to 3.4% in 2024.")
    assert a.direction == "rose"
    assert a.value == 3.4
    assert a.unit == "percent"
    assert a.period == "2024"
    assert a.geography == "DE"
    assert "Unemployment" in a.subject
    assert a.confidence > 0.8


def test_year_used_as_period_is_not_a_value():
    a = _one("US crime has doubled since 2020.")
    assert a.direction == "rose"
    assert a.value is None  # 2020 is a date, not a measured value
    assert a.period == "2020"
    assert a.geography == "US"
    assert a.metadata["relative_factor"] == 2.0


def test_scale_words_multiply():
    a = _one("GDP fell by $200 billion last quarter.")
    assert a.direction == "fell"
    assert a.value == 200_000_000_000.0
    assert a.unit == "usd"


def test_range_period():
    a = _one("German exports climbed 5.2% between 2019 and 2023.")
    assert a.period == "2019..2023"
    assert a.value == 5.2
    assert a.geography == "DE"


def test_quarter_period_normalized():
    a = _one("Retail sales rose 1.2% in Q3 2023.")
    assert a.period == "2023-Q3"


def test_exceeds_without_value_still_extracts():
    a = _one("Renewables now exceed a third of generation.")
    assert a.direction == "exceeds"
    assert a.value is None


def test_non_quantitative_sentence_yields_nothing():
    assert QuantityExtractor().extract("The committee met on Tuesday to discuss policy.") == []
    assert QuantityExtractor().extract("") == []
    assert QuantityExtractor().extract("   ") == []


def test_extract_document_over_sentences():
    doc = Document(
        document_id="d1",
        source_type="news",
        language="en",
        ingested_at=0,
        content="The mayor spoke today. Unemployment rose to 5% in 2023. Everyone applauded.",
    )
    assertions = QuantityExtractor().extract_document(doc)
    assert len(assertions) == 1
    assert assertions[0].direction == "rose"


def test_to_dict_shape():
    a = _one("Inflation exceeded 8 percent in 2022.")
    d = a.to_dict()
    assert set(d) == {
        "text", "subject", "direction", "value", "unit",
        "period", "geography", "confidence", "metadata",
    }


# --- Labeled precision spot-check across the six source types --------------
# Each item: (source_type, sentence, expected direction or None if not a
# quantitative claim). This is the A3 exit-criterion sample.
LABELED = [
    ("news", "Unemployment in France fell to 7.1% in 2023.", "fell"),
    ("news", "The minister announced a new committee on Tuesday.", None),
    ("blog", "In my view, remote work has increased productivity by 12% since 2020.", "rose"),
    ("blog", "I really enjoyed the conference this year.", None),
    ("paper", "Model accuracy exceeded 90% on the held-out test set.", "exceeds"),
    ("paper", "We describe the experimental setup in Section 3.", None),
    ("transcript", "So basically emissions dropped 4 percent last year.", "fell"),
    ("transcript", "And then we talked about the weather for a while.", None),
    ("book", "Between 1990 and 2000 the population grew by 15 million.", "rose"),
    ("book", "The protagonist wandered through the ancient city.", None),
    ("note", "China GDP surpassed $17 trillion in 2021.", "exceeds"),
    ("note", "Remember to email the team about the offsite.", None),
]


def test_precision_and_recall_over_labeled_sample():
    ex = QuantityExtractor()
    correct = 0
    quant_total = 0
    false_positives = 0
    for source_type, sentence, expected in LABELED:
        doc = Document(
            document_id="x", source_type=source_type, language="en",
            ingested_at=0, content=sentence,
        )
        got = ex.extract_document(doc)
        if expected is None:
            # A non-quantitative sentence must not yield an assertion.
            if got:
                false_positives += 1
        else:
            quant_total += 1
            if got and got[0].direction == expected:
                correct += 1
    # Every source type's quantitative sentence is detected with the right
    # direction, and no non-quantitative sentence produces a false positive.
    assert false_positives == 0
    assert correct == quant_total == 6


def test_all_six_source_types_supported():
    ex = QuantityExtractor()
    for st in ("news", "blog", "paper", "transcript", "book", "note"):
        doc = Document(
            document_id="x", source_type=st, language="en",
            ingested_at=0, content="Prices rose 3% in 2024.",
        )
        got = ex.extract_document(doc)
        assert got and got[0].direction == "rose", st
