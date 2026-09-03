"""
Tests for the user-driven entity corrections system — Issue #44.

Covers:
- Submission (payload validation, version tracking)
- Admin approval (applies change to the live KG store)
- Admin rejection (no KG change)
- All correction types: rename, add_alias, remove_alias, add_property,
  remove_property, merge
- Concurrent submissions are safe
- Double-approve / double-reject raise ValueError
"""
from __future__ import annotations

import threading
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state(tmp_path, monkeypatch):
    """Reset correction store and KG store before each test."""
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


def _seed_entity(name: str = "Tim Cook", entity_type: str = "Person") -> str:
    """Add an entity to the live KG store and return its node_id."""
    from src.knowledge_graph.kg_updater import _shared_store
    from src.knowledge_graph.foundation import Node, EntityType
    store = _shared_store()
    node = Node(type=EntityType(entity_type), name=name)
    store.add_node(node)
    return node.node_id


def _submit(entity_id: str, correction_type: str, payload: Dict[str, Any], reason: str = "fix") -> Any:
    from src.knowledge_graph.entity_corrections import (
        CorrectionType, get_correction_store,
    )
    return get_correction_store().submit(
        entity_id=entity_id,
        correction_type=CorrectionType(correction_type),
        payload=payload,
        reason=reason,
        submitted_by="user-42",
    )


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

class TestSubmit:
    def test_returns_correction_with_pending_status(self):
        from src.knowledge_graph.entity_corrections import CorrectionStatus
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "Timothy Cook"})
        assert c.status == CorrectionStatus.PENDING
        assert c.entity_id == entity_id
        assert c.correction_type.value == "rename"

    def test_versions_are_monotonic_per_entity(self):
        entity_id = _seed_entity()
        c1 = _submit(entity_id, "rename", {"new_name": "Name One"})
        c2 = _submit(entity_id, "rename", {"new_name": "Name Two"})
        assert c2.version == c1.version + 1

    def test_versions_are_independent_per_entity(self):
        e1 = _seed_entity("Alice", "Person")
        e2 = _seed_entity("Google", "Organization")
        c1 = _submit(e1, "rename", {"new_name": "Alice Smith"})
        c2 = _submit(e2, "rename", {"new_name": "Alphabet Inc"})
        assert c1.version == 1
        assert c2.version == 1

    def test_missing_payload_raises(self):
        entity_id = _seed_entity()
        with pytest.raises(ValueError, match="requires payload keys"):
            _submit(entity_id, "rename", {})

    def test_correction_id_is_unique(self):
        entity_id = _seed_entity()
        ids = {_submit(entity_id, "rename", {"new_name": f"Name{i}"}).correction_id for i in range(5)}
        assert len(ids) == 5

    def test_to_dict_is_serialisable(self):
        import json
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "X"})
        serialised = c.to_dict()
        json.dumps(serialised)  # must not raise
        assert serialised["status"] == "pending"


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

class TestApprove:
    def test_status_changes_to_approved(self):
        from src.knowledge_graph.entity_corrections import CorrectionStatus, get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "Tim Cook Jr"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin-1")
        assert c.status == CorrectionStatus.APPROVED

    def test_reviewer_and_timestamp_recorded(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "Tim Cook Jr"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin-1", review_note="confirmed")
        assert c.reviewed_by == "admin-1"
        assert c.review_note == "confirmed"
        assert c.reviewed_at is not None

    def test_double_approve_raises(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "X"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin-1")
        with pytest.raises(ValueError, match="already approved"):
            get_correction_store().approve(c.correction_id, reviewed_by="admin-2")

    def test_approve_unknown_correction_raises(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        with pytest.raises(KeyError):
            get_correction_store().approve("no-such-id", reviewed_by="admin")


# ---------------------------------------------------------------------------
# Rejection
# ---------------------------------------------------------------------------

class TestReject:
    def test_status_changes_to_rejected(self):
        from src.knowledge_graph.entity_corrections import CorrectionStatus, get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "Wrong Name"})
        get_correction_store().reject(c.correction_id, reviewed_by="admin-1", review_note="not correct")
        assert c.status == CorrectionStatus.REJECTED
        assert c.review_note == "not correct"

    def test_double_reject_raises(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "X"})
        get_correction_store().reject(c.correction_id, reviewed_by="admin-1")
        with pytest.raises(ValueError, match="already rejected"):
            get_correction_store().reject(c.correction_id, reviewed_by="admin-2")


# ---------------------------------------------------------------------------
# Correction type: rename
# ---------------------------------------------------------------------------

class TestRename:
    def test_renames_entity_in_kg(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        entity_id = _seed_entity("Jeff Bezos")
        _submit(entity_id, "rename", {"new_name": "Jeffrey Preston Bezos"})
        c = get_correction_store().list_corrections(entity_id=entity_id)[0]
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        node = _shared_store().get_node(entity_id)
        assert node.name == "Jeffrey Preston Bezos"


# ---------------------------------------------------------------------------
# Correction type: add_alias / remove_alias
# ---------------------------------------------------------------------------

class TestAliases:
    def test_add_alias(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        entity_id = _seed_entity("Apple")
        c = _submit(entity_id, "add_alias", {"alias": "Apple Inc"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        node = _shared_store().get_node(entity_id)
        assert "Apple Inc" in node.aliases

    def test_remove_alias(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        from src.knowledge_graph.foundation import Node, EntityType
        # Seed node with an alias
        store = _shared_store()
        node = Node(type=EntityType.ORGANIZATION, name="Apple", aliases=["Apple Inc"])
        store.add_node(node)
        entity_id = node.node_id
        c = _submit(entity_id, "remove_alias", {"alias": "Apple Inc"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        assert "Apple Inc" not in store.get_node(entity_id).aliases

    def test_add_duplicate_alias_is_idempotent(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        entity_id = _seed_entity("Tesla")
        c1 = _submit(entity_id, "add_alias", {"alias": "Tesla Inc"})
        c2 = _submit(entity_id, "add_alias", {"alias": "Tesla Inc"})
        get_correction_store().approve(c1.correction_id, reviewed_by="admin")
        get_correction_store().approve(c2.correction_id, reviewed_by="admin")
        node = _shared_store().get_node(entity_id)
        assert node.aliases.count("Tesla Inc") == 1


# ---------------------------------------------------------------------------
# Correction type: add_property / remove_property
# ---------------------------------------------------------------------------

class TestProperties:
    def test_add_property(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        entity_id = _seed_entity("Elon Musk")
        c = _submit(entity_id, "add_property", {"key": "role", "value": "CEO"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        node = _shared_store().get_node(entity_id)
        assert node.properties.get("role") == "CEO"

    def test_remove_property(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        from src.knowledge_graph.foundation import Node, EntityType
        store = _shared_store()
        node = Node(type=EntityType.PERSON, name="Elon Musk", properties={"role": "CEO"})
        store.add_node(node)
        entity_id = node.node_id
        c = _submit(entity_id, "remove_property", {"key": "role"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        assert "role" not in store.get_node(entity_id).properties

    def test_remove_nonexistent_property_is_safe(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "remove_property", {"key": "does_not_exist"})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")


# ---------------------------------------------------------------------------
# Correction type: merge
# ---------------------------------------------------------------------------

class TestMerge:
    def test_merge_copies_aliases_to_target(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        from src.knowledge_graph.foundation import Node, EntityType
        store = _shared_store()
        target = Node(type=EntityType.ORGANIZATION, name="Google")
        source = Node(type=EntityType.ORGANIZATION, name="Alphabet", aliases=["Alphabet Inc"])
        store.add_node(target)
        store.add_node(source)
        c = _submit(target.node_id, "merge", {"merge_from": source.node_id})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        t = store.get_node(target.node_id)
        assert "Alphabet" in t.aliases or "Alphabet Inc" in t.aliases

    def test_merge_removes_source_node(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        from src.knowledge_graph.kg_updater import _shared_store
        from src.knowledge_graph.foundation import Node, EntityType
        store = _shared_store()
        target = Node(type=EntityType.ORGANIZATION, name="Google")
        source = Node(type=EntityType.ORGANIZATION, name="Alphabet")
        store.add_node(target)
        store.add_node(source)
        source_id = source.node_id
        c = _submit(target.node_id, "merge", {"merge_from": source_id})
        get_correction_store().approve(c.correction_id, reviewed_by="admin")
        assert store.get_node(source_id) is None


# ---------------------------------------------------------------------------
# List / query
# ---------------------------------------------------------------------------

class TestList:
    def test_list_by_status(self):
        from src.knowledge_graph.entity_corrections import CorrectionStatus, get_correction_store
        e1 = _seed_entity("Alice", "Person")
        e2 = _seed_entity("Bob", "Person")
        c1 = _submit(e1, "rename", {"new_name": "Alice Smith"})
        _submit(e2, "rename", {"new_name": "Robert"})
        get_correction_store().approve(c1.correction_id, reviewed_by="admin")
        pending = get_correction_store().list_corrections(status=CorrectionStatus.PENDING)
        approved = get_correction_store().list_corrections(status=CorrectionStatus.APPROVED)
        assert len(pending) == 1
        assert len(approved) == 1

    def test_list_by_entity(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        e1 = _seed_entity("Alice", "Person")
        e2 = _seed_entity("Bob", "Person")
        _submit(e1, "rename", {"new_name": "Alice Smith"})
        _submit(e2, "rename", {"new_name": "Robert"})
        results = get_correction_store().list_corrections(entity_id=e1)
        assert all(c.entity_id == e1 for c in results)
        assert len(results) == 1

    def test_get_by_id(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        c = _submit(entity_id, "rename", {"new_name": "X"})
        found = get_correction_store().get(c.correction_id)
        assert found is not None
        assert found.correction_id == c.correction_id

    def test_get_unknown_returns_none(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        assert get_correction_store().get("no-such-id") is None


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_submissions_are_safe(self):
        from src.knowledge_graph.entity_corrections import get_correction_store
        entity_id = _seed_entity()
        errors = []

        def _run(i):
            try:
                _submit(entity_id, "rename", {"new_name": f"Name {i}"})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        store = get_correction_store()
        corrections = store.list_corrections(entity_id=entity_id, limit=100)
        assert len(corrections) == 20
        versions = sorted(c.version for c in corrections)
        assert versions == list(range(1, 21))
