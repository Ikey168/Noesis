"""Public ORCID v3 professional-record enrichment with explicit identity binding."""

import json
import re

from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.integrations.common import IntegrationError, digest
from src.knowledge_graph.foundation import EntityType, Node, make_node_id


def identifier(value):
    value = str(value).removeprefix("https://orcid.org/")
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", value):
        raise IntegrationError("invalid_identifier", "An ORCID identifier is required")
    digits = value.replace("-", "")
    total = 0
    for digit in digits[:-1]:
        total = (total + int(digit)) * 2
    remainder = (12 - total % 11) % 11
    if digits[-1] != ("X" if remainder == 10 else str(remainder)):
        raise IntegrationError("invalid_identifier", "ORCID checksum is invalid")
    return value


def normalize_public_record(native):
    orcid = identifier(native["orcid-identifier"]["path"])
    person = native.get("person") or {}
    name = person.get("name") or {}
    public_name = name if name.get("visibility") == "public" else None
    display = None
    if public_name:
        display = (name.get("credit-name") or {}).get("value") or " ".join(
            ((name.get(k) or {}).get("value") or "")
            for k in ("given-names", "family-name")
        ).strip()
    activities = native.get("activities-summary") or {}
    affiliations, works = [], []
    for section, singular in [
        ("employments", "employment"),
        ("educations", "education"),
    ]:
        for group in (activities.get(section) or {}).get("affiliation-group") or []:
            for summary in group.get("summaries") or []:
                value = summary.get(singular + "-summary") or {}
                if value.get("visibility") == "public":
                    affiliations.append({"kind": singular, "assertion": value})
    for group in (activities.get("works") or {}).get("group") or []:
        for work in group.get("work-summary") or []:
            if work.get("visibility") == "public":
                works.append(work)
    if len(affiliations) + len(works) > 5000:
        raise IntegrationError("input_limit", "Too many public record assertions")
    result = {
        "orcid": orcid,
        "name": display or None,
        "name_assertion": public_name,
        "affiliations": affiliations,
        "works": works,
        "last_modified": (native.get("history") or {}).get("last-modified-date"),
        "source_url": "https://pub.orcid.org/v3.0/" + orcid + "/record",
        "api_version": "3.0",
        "visibility": "public-only",
        "missingness": {
            "name": "available" if display else "unavailable-or-private",
            "affiliations": "public-assertions"
            if affiliations
            else "unavailable-or-private",
            "works": "public-assertions" if works else "unavailable-or-private",
        },
    }
    return {**result, "sha256": digest(result)}


class ORCIDClient:
    def __init__(self, *, token, transport=None):
        if not token:
            raise IntegrationError(
                "credential_unavailable", "Configure a /read-public ORCID OAuth token"
            )
        if (
            not isinstance(token, str)
            or len(token) > 8192
            or any(c.isspace() for c in token)
        ):
            raise IntegrationError("invalid_credential", "Invalid ORCID token format")
        self.token = token
        self.transport = transport or HTTPSPageAdapter._request

    def record(self, orcid):
        orcid = identifier(orcid)
        response = self.transport(
            url="https://pub.orcid.org/v3.0/" + orcid + "/record",
            params={},
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer " + self.token,
            },
            timeout=15,
            max_bytes=2_000_000,
        )
        if response.get("status", 200) != 200:
            raise IntegrationError(
                "source_unavailable", "ORCID public record is unavailable"
            )
        content = response["content"]
        if len(content) > 2_000_000:
            raise IntegrationError("input_limit", "ORCID response exceeds byte budget")
        record = normalize_public_record(json.loads(content))
        if record["orcid"] != orcid:
            raise IntegrationError(
                "identity_mismatch", "ORCID record differs from requested identity"
            )
        return record

    def enrich(self, orcid, store):
        record = self.record(orcid)
        # Match the foundation resolver's identifier-based ID construction;
        # never resolve a newly fetched identifier by name similarity.
        node_id = make_node_id(
            EntityType.PERSON,
            "identifiers:" + json.dumps({"orcid": record["orcid"]}, sort_keys=True),
        )
        existing = store.get_node(node_id)
        history = list(existing.properties.get("orcid_history", [])) if existing else []
        if not history or history[-1]["sha256"] != record["sha256"]:
            history.append(record)
        return store.add_node(
            Node(
                EntityType.PERSON,
                record["name"] or record["orcid"],
                node_id=node_id,
                aliases=[record["name"]] if record["name"] else [],
                properties={
                    "orcid": record["orcid"],
                    "orcid_record": record,
                    "orcid_history": history,
                    "resolution_status": "identifier-confirmed",
                    "assertion_semantics": "attributed public registry assertions; not independent verification",
                },
            )
        )
