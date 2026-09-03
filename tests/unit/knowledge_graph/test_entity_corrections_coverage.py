"""Coverage tests for src/knowledge_graph/entity_corrections.py.

Targets the REMAINING uncovered lines that the existing
tests/knowledge_graph/test_entity_corrections.py does not exercise:

  * line 267   -- approving a non-MERGE correction for a missing entity (KeyError)
  * lines 308/310 -- merge with a missing target / missing source node
  * line 319   -- merge property carry-over (setdefault, no conflict)
  * lines 322-350 -- triple rewriting in ``_apply_merge`` (rewrite, self-loop
                     skip, ontology-violation skip, provenance/triple deletion,
                     source-node removal)

Everything runs against the in-memory KnowledgeGraphStore from kg_updater; no
external services are used.
"""
from __future__ import annotations

import pytest

# Guard the foundation model / store imports (all pure-python, but be safe).
pytest.importorskip("src.knowledge_graph.entity_corrections")

from src.knowledge_graph.foundation import EntityType, Node  # noqa: E402
from src.knowledge_graph.foundation.model import Provenance, RelationType, Triple  # noqa: E402


@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Isolate the correction store and KG store per test."""
    import src.knowledge_graph.entity_corrections as ec
    import src.knowledge_graph.kg_updater as ku
    monkeypatch.setenv("NOESIS_DB_PATH", str(tmp_path / "kg.duckdb"))

    ec._correction_store = None
    ku._store = None
    ku._resolver = None
    ku._events.clear()
    yield
    if ku._store is not None and hasattr(ku._store, "close"):
        ku._store.close()
    ec._correction_store = None
    ku._store = None
    ku._resolver = None
    ku._events.clear()


def _store():
    from src.knowledge_graph.kg_updater import _shared_store

    return _shared_store()


def _submit(entity_id, correction_type, payload):
    from src.knowledge_graph.entity_corrections import CorrectionType, get_correction_store

    return get_correction_store().submit(
        entity_id=entity_id,
        correction_type=CorrectionType(correction_type),
        payload=payload,
        reason="test",
        submitted_by="user-1",
    )


def _approve(correction_id):
    from src.knowledge_graph.entity_corrections import get_correction_store

    return get_correction_store().approve(correction_id, reviewed_by="admin")


# ---------------------------------------------------------------------------
# Non-merge correction against a missing entity -> KeyError (line 267)
# ---------------------------------------------------------------------------

class TestMissingEntity:
    def test_approve_rename_for_absent_entity_raises_keyerror(self):
        # No node is seeded, so approval must fail with KeyError.
        c = _submit("does-not-exist", "rename", {"new_name": "Nope"})
        with pytest.raises(KeyError, match="not found in the knowledge graph"):
            _approve(c.correction_id)
        # The correction stays PENDING because approval blew up before status flip.
        from src.knowledge_graph.entity_corrections import CorrectionStatus, get_correction_store

        stored = get_correction_store().get(c.correction_id)
        assert stored.status == CorrectionStatus.PENDING

    def test_approve_add_property_for_absent_entity_raises_keyerror(self):
        c = _submit("ghost-id", "add_property", {"key": "role", "value": "CEO"})
        with pytest.raises(KeyError):
            _approve(c.correction_id)


# ---------------------------------------------------------------------------
# Merge error paths (lines 308, 310)
# ---------------------------------------------------------------------------

class TestMergeErrors:
    def test_merge_missing_target_raises(self):
        store = _store()
        source = Node(type=EntityType.CONCEPT, name="Source Concept")
        store.add_node(source)
        # target_id references a node that was never added.
        c = _submit("no-such-target", "merge", {"merge_from": source.node_id})
        with pytest.raises(KeyError, match="Merge target"):
            _approve(c.correction_id)

    def test_merge_missing_source_raises(self):
        store = _store()
        target = Node(type=EntityType.CONCEPT, name="Target Concept")
        store.add_node(target)
        c = _submit(target.node_id, "merge", {"merge_from": "no-such-source"})
        with pytest.raises(KeyError, match="Merge source"):
            _approve(c.correction_id)


# ---------------------------------------------------------------------------
# Full merge with triples (lines 313-353)
# ---------------------------------------------------------------------------

def _prov():
    return Provenance(source_doc="doc-1", confidence=0.9)


class TestMergeWithTriples:
    def test_merge_rewrites_triples_and_carries_properties(self):
        store = _store()
        # Three concepts; PART_OF is valid Concept->Concept.
        target = Node(type=EntityType.CONCEPT, name="Machine Learning")
        source = Node(
            type=EntityType.CONCEPT,
            name="ML",
            aliases=["Statistical Learning"],
            properties={"field": "AI", "wiki": "ml"},
        )
        neighbour = Node(type=EntityType.CONCEPT, name="Deep Learning")
        for n in (target, source, neighbour):
            store.add_node(n)

        # target already has one of the source's properties -> must NOT be
        # overwritten (setdefault keeps target's value), and 'field' is carried.
        target.properties["wiki"] = "target-wiki"

        # source --PART_OF--> neighbour  (subject == source)
        store.add_triple(
            Triple(
                subject=source.node_id,
                predicate=RelationType.PART_OF,
                object=neighbour.node_id,
                provenance=_prov(),
                properties={"weight": 1},
            )
        )
        # neighbour --PART_OF--> source  (object == source)
        store.add_triple(
            Triple(
                subject=neighbour.node_id,
                predicate=RelationType.PART_OF,
                object=source.node_id,
                provenance=_prov(),
            )
        )

        assert store.node_count == 3
        triples_before = store.triple_count
        assert triples_before == 2

        c = _submit(target.node_id, "merge", {"merge_from": source.node_id})
        _approve(c.correction_id)

        # Source node removed (line 353).
        assert store.get_node(source.node_id) is None
        assert store.node_count == 2

        # Source's alias + name folded into target (dedup).
        assert "Statistical Learning" in target.aliases
        assert "ML" in target.aliases

        # Property carry-over: 'field' added (line 319), 'wiki' NOT overwritten.
        assert target.properties["field"] == "AI"
        assert target.properties["wiki"] == "target-wiki"

        # Triples now reference target instead of source.
        remaining = list(store._triples.values())
        assert all(source.node_id not in (t.subject, t.object) for t in remaining)
        # target <-> neighbour edges both rewritten and preserved.
        pairs = {(t.subject, t.object) for t in remaining}
        assert (target.node_id, neighbour.node_id) in pairs
        assert (neighbour.node_id, target.node_id) in pairs

    def test_merge_skips_self_loop_created_by_rewrite(self):
        store = _store()
        target = Node(type=EntityType.CONCEPT, name="Alpha")
        source = Node(type=EntityType.CONCEPT, name="Beta")
        store.add_node(target)
        store.add_node(source)

        # target --PART_OF--> source ; after rewriting source->target this would
        # become target--PART_OF-->target (a self-loop) which must be skipped.
        store.add_triple(
            Triple(
                subject=target.node_id,
                predicate=RelationType.PART_OF,
                object=source.node_id,
                provenance=_prov(),
            )
        )
        assert store.triple_count == 1

        c = _submit(target.node_id, "merge", {"merge_from": source.node_id})
        _approve(c.correction_id)

        # The single triple was deleted (referenced source) and NOT re-added as a
        # self-loop -> zero triples remain.
        assert store.triple_count == 0
        assert store.get_node(source.node_id) is None

    def test_merge_skips_triple_that_violates_ontology_after_rewrite(self):
        """A rewritten triple that breaks the ontology is swallowed (lines 347-350)."""
        store = _store()
        # Document --CITES--> Document is valid. We merge a Document source into a
        # Concept target; the rewritten CITES(Concept->Document) is invalid and
        # add_triple raises OntologyViolation which _apply_merge catches & skips.
        target = Node(type=EntityType.CONCEPT, name="TargetConcept")
        source = Node(type=EntityType.DOCUMENT, name="SourceDoc")
        other_doc = Node(type=EntityType.DOCUMENT, name="OtherDoc")
        for n in (target, source, other_doc):
            store.add_node(n)

        # source(Document) --CITES--> other_doc(Document)  (valid to insert)
        store.add_triple(
            Triple(
                subject=source.node_id,
                predicate=RelationType.CITES,
                object=other_doc.node_id,
                provenance=_prov(),
            )
        )
        assert store.triple_count == 1

        c = _submit(target.node_id, "merge", {"merge_from": source.node_id})
        # Approval succeeds; the invalid rewritten triple is skipped, not raised.
        _approve(c.correction_id)

        # Source gone, and the ontology-violating rewrite was dropped.
        assert store.get_node(source.node_id) is None
        remaining = list(store._triples.values())
        assert all(source.node_id not in (t.subject, t.object) for t in remaining)
        # CITES from a Concept is invalid, so it was NOT re-added.
        assert store.triple_count == 0
