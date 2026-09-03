"""Coverage tests for src/knowledge_graph/enhanced_entity_extractor.py.

Targets the remaining reachable lines the existing extractor suite leaves out:

  * initialize_nlp_components() -- both the optimized-pipeline branch, the
    NER-only branch, and the failure/re-raise branch (lines 294-316)
  * extract_relationships() exception handling (lines 453-459)
  * _deduplicate_entities() alias-merge branch (lines 588-589)
  * _extract_relationships_by_context() indicator matching (lines 707-746)
  * _validate_relationship_types() valid / unknown / mismatch branches
    (lines 790-813)

The optional NLP imports always expose explicit availability flags and bound
placeholders, so the branch behavior is stable in both lean CI and full model
environments.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

import src.knowledge_graph.enhanced_entity_extractor as eee  # noqa: E402
from src.knowledge_graph.enhanced_entity_extractor import (  # noqa: E402
    AdvancedEntityExtractor,
    EnhancedEntity,
    create_advanced_entity_extractor,
)


def _run(coro):
    return asyncio.run(coro)


def _entity(text, label, start=0, end=0, confidence=0.9):
    return EnhancedEntity(
        text=text, label=label, start=start, end=end or len(text), confidence=confidence
    )


# ---------------------------------------------------------------------------
# Optional-dependency contract
# ---------------------------------------------------------------------------

def test_optional_nlp_symbols_are_always_bound():
    assert isinstance(eee.OPTIMIZED_NLP_AVAILABLE, bool)
    assert hasattr(eee, "NERProcessor")


# ---------------------------------------------------------------------------
# initialize_nlp_components
# ---------------------------------------------------------------------------

class TestInitializeNlpComponents:
    def test_ner_only_branch(self, monkeypatch):
        # Inject the (buggy-missing) flag as False so the optimized branch is
        # skipped and only the NER processor is constructed.
        monkeypatch.setattr(eee, "OPTIMIZED_NLP_AVAILABLE", False, raising=False)
        fake_ner_cls = MagicMock(return_value="ner-instance")
        monkeypatch.setattr(eee, "NERProcessor", fake_ner_cls)

        extractor = AdvancedEntityExtractor({"confidence_threshold": 0.5})
        _run(extractor.initialize_nlp_components())

        fake_ner_cls.assert_called_once_with(confidence_threshold=0.5)
        assert extractor.ner_processor == "ner-instance"
        assert extractor.nlp_pipeline is None

    def test_optimized_pipeline_branch(self, monkeypatch):
        # Inject flag True AND provide NLPConfig + IntegratedNLPProcessor so the
        # optimized-pipeline construction path runs.
        monkeypatch.setattr(eee, "OPTIMIZED_NLP_AVAILABLE", True, raising=False)
        fake_config = MagicMock(return_value="cfg")
        fake_integrated = MagicMock(return_value="pipeline-instance")
        fake_ner_cls = MagicMock(return_value="ner-instance")
        monkeypatch.setattr(eee, "NLPConfig", fake_config, raising=False)
        monkeypatch.setattr(eee, "IntegratedNLPProcessor", fake_integrated)
        monkeypatch.setattr(eee, "NERProcessor", fake_ner_cls)

        extractor = AdvancedEntityExtractor()
        _run(extractor.initialize_nlp_components())

        fake_config.assert_called_once()
        fake_integrated.assert_called_once_with("cfg")
        assert extractor.nlp_pipeline == "pipeline-instance"
        assert extractor.ner_processor == "ner-instance"

    def test_failure_is_logged_and_reraised(self, monkeypatch):
        monkeypatch.setattr(eee, "OPTIMIZED_NLP_AVAILABLE", False, raising=False)
        monkeypatch.setattr(
            eee, "NERProcessor", MagicMock(side_effect=RuntimeError("no model"))
        )
        extractor = AdvancedEntityExtractor()
        with pytest.raises(RuntimeError, match="no model"):
            _run(extractor.initialize_nlp_components())


# ---------------------------------------------------------------------------
# extract_relationships exception path
# ---------------------------------------------------------------------------

class TestExtractRelationshipsErrorPath:
    def test_returns_empty_list_on_exception(self):
        extractor = create_advanced_entity_extractor()
        entities = [_entity("Alice", "PERSON"), _entity("Acme", "ORGANIZATION")]

        # Force the first internal step to blow up so the except branch runs.
        async def boom(*args, **kwargs):
            raise RuntimeError("pattern failure")

        with patch.object(
            extractor, "_extract_relationships_by_patterns", side_effect=boom
        ):
            result = _run(extractor.extract_relationships(entities, "text", "a1"))
        assert result == []


# ---------------------------------------------------------------------------
# _deduplicate_entities alias merge
# ---------------------------------------------------------------------------

class TestDeduplicateAliasMerge:
    def test_different_text_same_normalized_form_becomes_alias(self):
        extractor = create_advanced_entity_extractor()
        # Two ORGANIZATION mentions that normalize to the same form ("Apple")
        # but have different surface text -> the second's text becomes an alias.
        e1 = EnhancedEntity(text="Apple Inc.", label="ORGANIZATION", start=0, end=10, confidence=0.8)
        e2 = EnhancedEntity(text="Apple Corp.", label="ORGANIZATION", start=20, end=30, confidence=0.95)
        assert e1.normalized_form == e2.normalized_form  # both "Apple"

        merged = extractor._deduplicate_entities([e1, e2])
        assert len(merged) == 1
        survivor = merged[0]
        assert survivor.mention_count == 2
        assert survivor.confidence == 0.95  # max confidence kept
        # The differing surface form was recorded as an alias.
        assert "Apple Corp." in survivor.aliases

    def test_property_merge_from_duplicate(self):
        extractor = create_advanced_entity_extractor()
        e1 = EnhancedEntity(text="Apple Inc.", label="ORGANIZATION", start=0, end=10)
        e1.properties = {"industry": "tech"}
        e2 = EnhancedEntity(text="Apple Inc.", label="ORGANIZATION", start=20, end=30)
        e2.properties = {"headquarters": "Cupertino"}
        merged = extractor._deduplicate_entities([e1, e2])
        assert len(merged) == 1
        assert merged[0].properties.get("industry") == "tech"
        assert merged[0].properties.get("headquarters") == "Cupertino"


# ---------------------------------------------------------------------------
# _extract_relationships_by_context
# ---------------------------------------------------------------------------

class TestContextRelationships:
    def test_builds_relationship_when_indicator_present(self):
        extractor = create_advanced_entity_extractor()
        text = "Alice works for Acme as an employee of the company"
        # Position the entities inside the (single) sentence.
        alice = _entity("Alice", "PERSON", start=0, end=5, confidence=0.9)
        acme = _entity("Acme", "ORGANIZATION", start=16, end=20, confidence=0.9)

        rels = _run(
            extractor._extract_relationships_by_context([alice, acme], text, "art-1")
        )
        # "works"/"employee" indicators -> a WORKS_FOR relationship (valid
        # PERSON/ORGANIZATION pair).
        assert any(r.relation_type == "WORKS_FOR" for r in rels)
        works = next(r for r in rels if r.relation_type == "WORKS_FOR")
        assert works.article_id == "art-1"
        assert works.confidence > 0

    def test_no_relationship_without_two_entities(self):
        extractor = create_advanced_entity_extractor()
        text = "Alice works alone."
        alice = _entity("Alice", "PERSON", start=0, end=5)
        rels = _run(
            extractor._extract_relationships_by_context([alice], text, "art-2")
        )
        assert rels == []


# ---------------------------------------------------------------------------
# _validate_relationship_types
# ---------------------------------------------------------------------------

class TestValidateRelationshipTypes:
    def test_valid_person_org_works_for(self):
        extractor = create_advanced_entity_extractor()
        p = _entity("Alice", "PERSON")
        o = _entity("Acme", "ORGANIZATION")
        assert extractor._validate_relationship_types(p, o, "WORKS_FOR") is True

    def test_valid_reversed_order(self):
        extractor = create_advanced_entity_extractor()
        p = _entity("Alice", "PERSON")
        o = _entity("Acme", "ORGANIZATION")
        # Reversed (ORG, PERSON) should still validate for WORKS_FOR.
        assert extractor._validate_relationship_types(o, p, "WORKS_FOR") is True

    def test_unknown_relation_type_is_allowed(self):
        extractor = create_advanced_entity_extractor()
        p = _entity("Alice", "PERSON")
        o = _entity("Bob", "PERSON")
        assert extractor._validate_relationship_types(p, o, "SOMETHING_NEW") is True

    def test_invalid_combination_is_rejected(self):
        extractor = create_advanced_entity_extractor()
        # WORKS_FOR only valid for PERSON/ORGANIZATION; two technologies -> False.
        t1 = _entity("Python", "TECHNOLOGY")
        t2 = _entity("Rust", "TECHNOLOGY")
        assert extractor._validate_relationship_types(t1, t2, "WORKS_FOR") is False
