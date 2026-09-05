import copy
import json
from pathlib import Path

import pytest

from src.ingestion.orcid import ORCIDClient, identifier, normalize_public_record
from src.knowledge_graph.foundation import KnowledgeGraphStore


def fixture():
    return json.loads(
        Path("tests/fixtures/integrations/orcid-public-projection.json").read_text()
    )


def test_public_assertions_retain_attribution_and_private_fields_stay_missing():
    native = fixture()
    public = normalize_public_record(native)
    assert public["name"] == "Josiah Carberry"
    assert public["works"] and public["affiliations"]
    assert all("source" in work for work in public["works"])
    native["person"]["name"]["visibility"] = "limited"
    native["activities-summary"] = {}
    unavailable = normalize_public_record(native)
    assert unavailable["name"] is None
    assert unavailable["missingness"]["works"] == "unavailable-or-private"
    with pytest.raises(ValueError):
        identifier("0000-0002-1825-0098")
    with pytest.raises(ValueError):
        ORCIDClient(token=None)


def test_explicit_ids_keep_same_name_authors_separate_and_replay_updates():
    native = fixture()
    # Synthetic German name and changed affiliation, not human evaluation data.
    native["person"]["name"]["given-names"]["value"] = "Jürgen"
    native["person"]["name"]["family-name"]["value"] = "Müller"
    calls = []

    def transport(**kwargs):
        calls.append(kwargs)
        return {"content": json.dumps(native)}

    client = ORCIDClient(token="test-token", transport=transport)
    store = KnowledgeGraphStore()
    first = client.enrich(native["orcid-identifier"]["path"], store)
    same = client.enrich(native["orcid-identifier"]["path"], store)
    assert first.node_id == same.node_id and len(same.properties["orcid_history"]) == 1
    assert "test-token" not in json.dumps(first.to_dict())
    group = native["activities-summary"]["employments"]["affiliation-group"][0]
    group["summaries"][0]["employment-summary"]["organization"]["name"] = (
        "Berliner Forschungsinstitut"
    )
    changed = client.enrich(native["orcid-identifier"]["path"], store)
    assert len(changed.properties["orcid_history"]) == 2
    other = copy.deepcopy(native)
    other["orcid-identifier"]["path"] = "0000-0000-0000-001X"
    native = other
    second = client.enrich(native["orcid-identifier"]["path"], store)
    assert second.name == first.name and second.node_id != first.node_id
    assert calls[0]["headers"]["Authorization"] == "Bearer test-token"
