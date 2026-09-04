from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.cross_language import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    CrossLanguageError,
    CrossLanguageStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def test_original_identity_unknown_unicode_code_switch_and_immutability():
    store = CrossLanguageStore(duckdb.connect(":memory:"), now=lambda: 100)
    result = store.record_text(
        "research",
        "document",
        "d1",
        "Cafe\u0301 — привет",
        language="und",
        script="Zyyy",
        code_switches=[{"start": 7, "end": 13, "language": "ru"}],
        metadata={"source": "s1", "generation": 2},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    assert result["original_text"] == "Cafe\u0301 — привет"
    assert result["normalized_text"] == "Café — привет"
    assert result["language"] == "und" and result["code_switches"]
    assert store.record_text(
        "research",
        "document",
        "d1",
        "Cafe\u0301 — привет",
        language="und",
        script="Zyyy",
        principal_id="p",
        scopes={WRITE_SCOPE},
    )["idempotent"]
    with pytest.raises(CrossLanguageError, match="cannot be overwritten"):
        store.record_text(
            "research",
            "document",
            "d1",
            "different",
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
    validate("noesis-language-text-v1.json", result)


def test_alias_transliterations_homonyms_historical_and_review():
    store = CrossLanguageStore(duckdb.connect(":memory:"), now=lambda: 100)
    a = store.record_alias(
        "political",
        "entity:kyiv",
        "Kiev",
        "en",
        "Latn",
        transliteration_system="historical-English",
        confidence=0.7,
        alternatives=["Kyiv"],
        evidence=[{"source_id": "s1"}],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    b = store.record_alias(
        "political",
        "entity:kyiv",
        "Kyiv",
        "uk",
        "Latn",
        transliteration_system="BGN-PCGN",
        confidence=0.9,
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    assert a["alias_id"] != b["alias_id"]
    reviewed = store.review_alias(
        "political",
        a["alias_id"],
        "ambiguous",
        "r1",
        rationale="historical spelling may be politically loaded",
        principal_id="r1",
        scopes={REVIEW_SCOPE},
    )
    assert reviewed["status"] == "ambiguous"
    validate("noesis-multilingual-alias-v1.json", reviewed)


def test_claim_alignment_preserves_wording_and_false_equivalence_analysis():
    store = CrossLanguageStore(duckdb.connect(":memory:"), now=lambda: 100)
    source = store.record_text(
        "osint",
        "claim",
        "c1",
        "No troops may enter.",
        language="en",
        script="Latn",
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    target = store.record_text(
        "osint",
        "claim",
        "c2",
        "Войска могут войти.",
        language="ru",
        script="Cyrl",
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    aligned = store.align_claims(
        "osint",
        "c1",
        "c2",
        "divergent",
        source["text_id"],
        target["text_id"],
        confidence=0.98,
        analysis={
            "negation": "reversed",
            "modality": "changed",
            "numeric_format": "none",
            "idiom": "none",
        },
        evidence=[{"locator": "p1"}],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    assert aligned["source_text"]["original_text"].startswith("No")
    assert aligned["target_text"]["original_text"].startswith("Войска")
    assert aligned["relation"] == "divergent"
    validate("noesis-cross-language-claim-alignment-v1.json", aligned)


def test_translation_versions_disagreement_partial_passage_and_namespace():
    store = CrossLanguageStore(duckdb.connect(":memory:"), now=lambda: 100)
    source = store.record_text(
        "scientific",
        "passage",
        "p1",
        "Die Probe ist klein.",
        language="de",
        script="Latn",
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    first = store.record_translation(
        "scientific",
        source["text_id"],
        "en",
        "The sample is small.",
        {"kind": "model", "name": "local-model", "version": "1"},
        version=1,
        passage={"start": 0, "end": 20},
        confidence=0.8,
        alternatives=["The specimen is small."],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    second = store.record_translation(
        "scientific",
        source["text_id"],
        "en",
        "The sample size is small.",
        {"kind": "model", "name": "local-model", "version": "2"},
        version=2,
        confidence=0.9,
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    disputed = store.review_translation(
        "scientific",
        first["translation_id"],
        "disputed",
        "human:1",
        rationale="Probe is context-dependent",
        principal_id="human:1",
        scopes={REVIEW_SCOPE},
    )
    assert (
        first["translation_id"] != second["translation_id"]
        and disputed["status"] == "disputed"
    )
    assert disputed["source_original_text"] == "Die Probe ist klein."
    with pytest.raises(CrossLanguageError, match="not found"):
        store.get_text("other", source["text_id"], scopes={READ_SCOPE})
    validate("noesis-translation-record-v1.json", disputed)


def test_fair_multilingual_search_authorization_and_six_domain_fixtures():
    store = CrossLanguageStore(duckdb.connect(":memory:"), now=lambda: 100)
    domains = ("research", "political", "economic", "osint", "technical", "scientific")
    for namespace in domains:
        store.record_text(
            namespace,
            "document",
            "en",
            "energy report",
            language="en",
            script="Latn",
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
        store.record_text(
            namespace,
            "document",
            "de",
            "energy Bericht",
            language="de",
            script="Latn",
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
    result = store.search("political", "energy", limit=2, scopes={READ_SCOPE})
    assert {item["language"] for item in result["results"]} == {"de", "en"}
    assert result["ranking"]["translation_results_preserve_original"]
    with pytest.raises(CrossLanguageError, match="scope"):
        store.search("political", "energy", scopes=set())
    assert (
        store.conn.execute("SELECT count(*) FROM cross_language_audit").fetchone()[0]
        == 12
    )
    validate("noesis-multilingual-search-v1.json", result)
