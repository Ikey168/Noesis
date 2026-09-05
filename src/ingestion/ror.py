"""ROR v2 registry enrichment with identifier-scoped organization identities."""

import json
import re

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError, digest
from src.knowledge_graph.foundation.model import Node, make_node_id
from src.knowledge_graph.foundation.ontology import EntityType


class RORClient:
    def __init__(self, *, transport=None):
        self.transport = transport or HTTPSPageAdapter._request

    def _get(self, suffix="", params=None):
        response = self.transport(
            url="https://api.ror.org/v2/organizations" + suffix,
            params=params or {},
            headers={"Accept": "application/json"},
            timeout=15,
            max_bytes=2_000_000,
        )
        if response.get("status", 200) != 200:
            raise IntegrationError("source_unavailable", "ROR registry request failed")
        content = response["content"]
        if len(content) > 2_000_000:
            raise IntegrationError("input_limit", "ROR response exceeds byte budget")
        return json.loads(content)

    @staticmethod
    def identifier(value):
        value = value.removeprefix("https://ror.org/")
        if not re.fullmatch(r"0[0-9a-hj-km-np-tv-z]{6}[0-9]{2}", value):
            raise IntegrationError("invalid_identifier", "ROR identifier required")
        return value

    def record(self, identifier):
        identifier = self.identifier(identifier)
        native = self._get("/" + identifier)
        if native.get("id") != "https://ror.org/" + identifier:
            raise IntegrationError(
                "identity_mismatch", "ROR record identifier differs from request"
            )
        return self.normalize(native)

    def search(self, query, *, page=1):
        if (
            not isinstance(query, str)
            or not 1 <= len(query) <= 500
            or not 1 <= page <= 500
        ):
            raise ValueError("Bounded query and page required")
        native = self._get(params={"query": query, "page": page, "all_status": ""})
        items = native.get("items")
        if not isinstance(items, list) or len(items) > 20:
            raise IntegrationError("schema_drift", "Unexpected ROR search page")
        # Search results are candidates, even when only one name happens to match.
        return {
            "status": "candidates",
            "query": query,
            "page": page,
            "total": native.get("number_of_results"),
            "candidates": [self.normalize(item) for item in items],
        }

    def normalize(self, native):
        identifier = self.identifier(native["id"])
        names = native.get("names") or []
        if not names or len(names) > 1000:
            raise IntegrationError("schema_drift", "ROR names are missing or excessive")
        display = next(
            (n["value"] for n in names if "ror_display" in n["types"]),
            names[0]["value"],
        )
        relationships = native.get("relationships") or []
        for relation in relationships:
            self.identifier(relation["id"])
            if relation["type"] not in {
                "parent",
                "child",
                "related",
                "predecessor",
                "successor",
            }:
                raise IntegrationError("schema_drift", "Unknown ROR relationship type")
        return {
            "ror": "https://ror.org/" + identifier,
            "name": display,
            "names": names,
            "status": native["status"],
            "relationships": relationships,
            "locations": native.get("locations") or [],
            "native_record": native,
            "api_version": "v2",
            "sha256": digest(native),
        }

    def enrich(self, identifier, store):
        record = self.record(identifier)
        # Explicit registry identity never collapses parent/child or same-name IDs.
        node_id = make_node_id(EntityType.ORGANIZATION, "ror:" + record["ror"])
        existing = store.get_node(node_id)
        history = list(existing.properties.get("ror_history", [])) if existing else []
        if not history or history[-1]["sha256"] != record["sha256"]:
            history.append(record)
        node = Node(
            type=EntityType.ORGANIZATION,
            name=record["name"],
            node_id=node_id,
            aliases=[n["value"] for n in record["names"]],
            properties={
                "ror": record["ror"],
                "ror_record": record,
                "ror_history": history,
                "resolution_status": "identifier-confirmed",
            },
        )
        result = store.add_node(node)
        return result
