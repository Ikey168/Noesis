"""Citations to standards, regulations, directives, recommendations and recalls by exact identifier (ES11, #2072)."""

from __future__ import annotations

import pytest

from src.kb.engineering_safety_citations import extract, link_citations
from src.kb.engineering_safety_records import EngineeringSafetyError
from tests.unit.engineering_safety import harness as h


def kinds(text):
    return [(c["kind"], c["reference_key"], c["raw"]) for c in extract(text, {"paragraph": "(g)"})]


def test_each_citation_kind_is_extracted_with_its_span_and_shared_boundaries():
    text = ("Comply with 14 CFR 39.13 and Regulation (EU) 2018/1139 and Regulation (EU) No 748/2012; inspect per SAE "
            "ARP5089, RTCA DO-178C, API RP 1160, ASME B31.8 and ISO 9001:2015. AD 2025-12-05 and EASA AD 2026-0123R1 "
            "apply; see NTSB A-26-015, CSB 2026-02-I-TX-R1, recall 26V104000 and Service Bulletin EX100-32-0045.")
    found = kinds(text)
    assert ("legal", "cfr:14:39.13", "14 CFR 39.13") in found
    assert ("legal", "celex:32018R1139", "Regulation (EU) 2018/1139") in found
    assert ("legal", "celex:32012R0748", "Regulation (EU) No 748/2012") in found
    assert {r for k, _, r in found if k == "standard"} == {"SAE ARP5089", "RTCA DO-178C", "API RP 1160",
                                                         "ASME B31.8", "ISO 9001:2015"}
    assert ("directive", "faa-ad:2025-12-05", "AD 2025-12-05") in found
    assert ("directive", "easa-ad:2026-0123", "EASA AD 2026-0123R1") in found
    assert not [f for f in found if f[1] == "faa-ad:2026-0123"]  # the EASA number is not also an FAA AD
    assert ("recommendation", "ntsb:A-26-015", "A-26-015") in found
    assert ("recommendation", "csb:2026-02-I-TX-R1", "2026-02-I-TX-R1") in found
    assert ("recall", "nhtsa:26V104000", "26V104000") in found
    assert ("service_bulletin", "service-bulletin:EX100-32-0045", "Service Bulletin EX100-32-0045") in found
    span = next(c for c in extract(text, {"p": 1}) if c["raw"] == "14 CFR 39.13")["locator"]["span"]
    assert text[span[0]:span[1]] == "14 CFR 39.13"
    # Identifier boundaries: nothing inside a longer token or a path.
    assert kinds("xAD 2025-12-05 26V104000X eli/26V104000/x") == []


@pytest.fixture()
def env():
    env = h.Env()
    assert env.run("r1")["status"] == "complete"
    return env


def test_internal_citations_resolve_revision_aware_and_the_rest_stay_unresolved(env):
    result = link_citations(env.conn, h.NS, scopes=h.WRITE, principal_id="linker")
    linked = {(c["raw"], c["target_kind"]) for c in result["linked"]}
    assert ("AD 2025-12-05", "engineering-safety-record") in linked
    assert ("A-26-015", "engineering-safety-record") in linked
    assert ("EASA AD 2026-0123R1", "engineering-safety-record") in linked  # cited by the BFU recommendation
    unresolved = {c["raw"] for c in result["unresolved"]}
    assert {"SAE ARP5089", "14 CFR 39.19", "Service Bulletin EX100-32-0045"} <= unresolved
    assert "26V104000" in unresolved  # no Products namespace named: kept as the cited identifier
    link = next(c for c in result["linked"] if c["raw"] == "AD 2025-12-05")
    target = env.store.record(h.NS, link["target_id"])
    assert target["native_id"] == "2025-12-05"
    assert link["target_revision_id"] == env.store.current_revision_id(h.NS, link["target_id"])
    assert link_citations(env.conn, h.NS, scopes=h.WRITE, principal_id="linker")["linked"] == []  # idempotent
    record = env.record("faa-ad", "2026-04-12")
    parts = env.store.parts(h.NS, env.store.current_revision_id(h.NS, record))
    cited = next(c for c in parts["citations"] if c["raw"] == "AD 2025-12-05")
    assert cited["resolution"] == "resolved" and cited["links"][0]["target_revision_id"]
    kept = next(c for c in parts["citations"] if c["kind"] == "service_bulletin")
    assert kept["resolution"].startswith("unresolved")


def test_owners_are_consulted_only_when_named_and_with_their_scopes(env):
    notice_id = h.seed_recall(env.conn)
    with pytest.raises(EngineeringSafetyError) as denied:
        link_citations(env.conn, h.NS, scopes=h.WRITE, principal_id="linker", products_namespace=h.NS)
    assert denied.value.code == "unauthorized"
    result = link_citations(env.conn, h.NS, scopes=h.ALL, principal_id="linker", products_namespace=h.NS,
                            standards_namespace=h.NS, legal_namespace=h.NS)
    recall = next(c for c in result["linked"] if c["raw"] == "26V104000")
    assert recall["target_kind"] == "product-safety-notice" and recall["target_id"] == notice_id
    assert "SAE ARP5089" in {c["raw"] for c in result["unresolved"]}  # no catalogue edition: never by topic
