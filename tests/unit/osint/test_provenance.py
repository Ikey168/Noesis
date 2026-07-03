"""Tests for trace_artifact() (issue #639, completing #614's provenance bullet)."""

from src.osint import trace_artifact


def _corpus(seed):
    seed.articles(
        [
            ("d1", "Rule cuts sector output by 12 percent", "http://a/1", "Alpha Wire", "2026-06-01"),
        ]
    )
    seed.claims(
        [
            ("k1", "The rule cuts output 12 percent.", "d1", "news", 0.9, "disputed"),
            ("k2", "A sibling claim from the same doc.", "d1", "news", 0.7, None),
        ]
    )
    seed.actors(
        [
            ("d1", "Grid Authority", "org:ga", "subject"),
            ("d1", "Jordan Rivera", "person:jr", "speaker"),
        ]
    )
    seed.evidence([("e1", "k1", "d1", "news", "supports", 0.8)])


def test_trace_a_claim_end_to_end(seed):
    _corpus(seed)
    out = trace_artifact(seed.conn, claim_id="k1")
    assert out["cited"] is True
    stages = [s["stage"] for s in out["chain"]]
    # Ordered: source -> document -> enrichment -> claim.
    assert stages[:4] == ["source", "document", "enrichment", "claim"]
    src = out["chain"][0]
    assert src["source"] == "Alpha Wire" and src["cite"]["cited"] is True
    doc = out["chain"][1]
    assert doc["title"].startswith("Rule cuts") and doc["url"] == "http://a/1"
    enrich = out["chain"][2]
    assert enrich["claim_count"] == 2 and enrich["entity_count"] == 2
    claim = out["chain"][3]
    assert claim["claim_id"] == "k1" and claim["verdict"] == "disputed"


def test_trace_a_document(seed):
    _corpus(seed)
    out = trace_artifact(seed.conn, document_id="d1")
    assert out["artifact"] == {"type": "document", "id": "d1"}
    stages = [s["stage"] for s in out["chain"]]
    assert stages == ["source", "document", "enrichment"]  # no single claim stage
    assert len(out["claims"]) == 2  # both claims from the document surfaced


def test_trace_includes_routed_namespace(seed):
    _corpus(seed)
    # Simulate the document having been routed into a provisioned KG.
    from datetime import datetime, timezone

    from src.provisioning import namespaces, store

    store.ensure_schema(seed.conn)
    store.upsert_kg(seed.conn, "energy", "Energy KG", None, datetime(2026, 6, 1, tzinfo=timezone.utc))
    namespaces.create_namespace(seed.conn, "energy")
    seed.conn.execute(
        "INSERT INTO kg_energy_documents (id, title, source, source_type, url, published_at, routed_at) "
        "VALUES ('d1', 't', 'Alpha Wire', 'news', 'http://a/1', NULL, NULL)"
    )
    out = trace_artifact(seed.conn, claim_id="k1")
    ns_stage = [s for s in out["chain"] if s["stage"] == "namespaces"]
    assert ns_stage and ns_stage[0]["routed_into"][0]["kg"] == "energy"


def test_uncited_when_document_missing(seed):
    seed.claims([("k9", "A claim with no resolvable document.", "ghost", "news", 0.5, None)])
    out = trace_artifact(seed.conn, claim_id="k9")
    assert out["cited"] is False
    assert out["chain"][0]["cite"]["cited"] is False


def test_unknown_claim_errors(seed):
    _corpus(seed)
    assert trace_artifact(seed.conn, claim_id="nope")["code"] == "not_found"


def test_requires_an_input(seed):
    assert trace_artifact(seed.conn)["code"] == "no_input"
