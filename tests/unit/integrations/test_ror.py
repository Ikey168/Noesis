import copy
import json
from pathlib import Path

from src.ingestion.ror import RORClient
from src.knowledge_graph.foundation.store import KnowledgeGraphStore


def test_berlin_registry_replay_retains_names_status_and_typed_relationships():
    native = json.loads(Path("tests/fixtures/integrations/ror-native.json").read_text())
    current = copy.deepcopy(native)
    client = RORClient(transport=lambda **_: {"content": json.dumps(current)})
    store = KnowledgeGraphStore()
    first = client.enrich(native["id"], store)
    second = client.enrich(native["id"], store)
    assert first.node_id == second.node_id
    assert len(second.properties["ror_history"]) == 1
    assert second.properties["ror_record"]["relationships"] == native["relationships"]
    assert any("Universität" in name for name in second.aliases)
    current["status"] = "inactive"
    current["names"].append(
        {"lang": "de", "types": ["alias"], "value": "Historischer Berliner Name"}
    )
    changed = client.enrich(native["id"], store)
    assert changed.properties["ror_record"]["status"] == "inactive"
    assert len(changed.properties["ror_history"]) == 2
    assert "Historischer Berliner Name" in changed.aliases
    current["id"] = "https://ror.org/046ak2485"
    distinct = client.enrich(current["id"], store)
    assert distinct.node_id != first.node_id


def test_name_search_remains_candidates_including_inactive_records():
    native = json.loads(Path("tests/fixtures/integrations/ror-native.json").read_text())
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {
            "content": json.dumps({"items": [native, native], "number_of_results": 2})
        }

    result = RORClient(transport=transport).search("Universität Berlin")
    assert result["status"] == "candidates"
    assert len(result["candidates"]) == 2
    assert "all_status" in calls[0]["params"]


def test_registry_history_survives_durable_store_restart(tmp_path):
    from src.knowledge_graph.foundation.store import DuckDBKnowledgeGraphStore

    native = json.loads(Path("tests/fixtures/integrations/ror-native.json").read_text())
    client = RORClient(transport=lambda **_: {"content": json.dumps(native)})
    path = tmp_path / "registry.duckdb"
    store = DuckDBKnowledgeGraphStore(path)
    node_id = client.enrich(native["id"], store).node_id
    store.connection.close()
    reopened = DuckDBKnowledgeGraphStore(path)
    node = client.enrich(native["id"], reopened)
    assert node.node_id == node_id
    assert len(node.properties["ror_history"]) == 1
    assert node.properties["ror_record"]["native_record"] == native
    reopened.connection.close()
