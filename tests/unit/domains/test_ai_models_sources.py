"""AI03-AI05 (#2757, #2764, #2770; track #2742): Hugging Face Hub, OpenML and Epoch AI contracts and acquisition."""

from __future__ import annotations

import json

import pytest

from src.ingestion.ai_models_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    DOCUMENTED_NOT_ACQUIRED,
    EXCLUSIONS,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    MINIMISATION,
    PACING_S,
    PROVIDER_CONTRACTS,
    SECRET_REF,
    AiModelsAdapter,
    fixture_transport,
    split_card,
    stated_identifiers,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import ai_models_harness as h

A, B, C, D, E = (h.SHAS[k] for k in "ABCDE")
AUDIT = (h.ROOT / "docs/development/ai-models-evidence/source-audit.md")


def test_every_provider_has_the_audited_contract_caps_minimisation_and_an_unverified_live_entry():
    assert set(PROVIDER_CONTRACTS) == set(h.SOURCES.values()) == set(LIVE_VERIFICATION) == set(CAPS) == set(PACING_S)
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("publisher", "delivers", "access", "endpoints", "authentication", "licence", "redistribution",
                    "rate_limits", "revision_model", "personal_data", "unavailable_fallback", "verify"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert LIVE_VERIFICATION[provider]["checked"] is None and PACING_S[provider] == 1.0
    assert CAPS["huggingface-hub"] == {"repositories": 5, "revisions_per_repository": 10, "card_bytes": 1_000_000}
    assert CAPS["openml"] == {"datasets": 2, "tasks": 1, "evaluation_rows": 100}
    assert CAPS["epoch-ai"] == {"files": 1, "bytes": 20_000_000, "rows": 20}
    assert BOUNDED_COVERAGE["huggingface-hub"]["caps"] == "5 repositories, 10 declared revisions each, card files of " \
                                                          "at most 1 MB"
    assert BOUNDED_COVERAGE["openml"]["caps"] == "2 datasets, 1 task, 100 evaluation rows"
    assert BOUNDED_COVERAGE["epoch-ai"]["caps"] == "1 file, 20 rows stored"
    assert SECRET_REF == "NOESIS_HF_TOKEN" and "never unlocks gated content" in PROVIDER_CONTRACTS[
        "huggingface-hub"]["authentication"]
    assert "never configured" in PROVIDER_CONTRACTS["openml"]["authentication"]
    assert "verify that v1 stays served after OpenML's server migration" in PROVIDER_CONTRACTS["openml"]["access"]
    assert {"stored", "redacted", "excluded", "retention", "who_may_query"} <= set(MINIMISATION)
    assert {"papers-with-code", "kaggle-datasets", "gated-and-private-hub-repositories"} <= set(
        DOCUMENTED_NOT_ACQUIRED)
    assert {"leaderboards or rankings", "download, like or trending counts", "licence-compliance interpretation",
            "downloading model weights or dataset files"} <= set(EXCLUSIONS)
    # The machine-readable copy matches the AI01 audit (verbatim decisions and caps).
    audit = AUDIT.read_text()
    for phrase in ("5 repositories, 10 declared revisions each, card files of at most 1 MB",
                   "2 datasets, 1 task, 100 evaluation rows", "1 file, 20 rows stored", "NOESIS_HF_TOKEN",
                   "paced at 1 request/second", "knowledge:technical:ai-models:read", "removed_by_source",
                   "not-returned", "retrieval_time", "`unverified-live`"):
        assert phrase in audit, phrase
    for decision in ("stored", "redacted", "excluded", "retention"):
        flat = " ".join(audit.replace("`", "").split())
        assert MINIMISATION[decision].split(",")[0].split(";")[0] in flat, decision


def test_the_pinned_fixtures_replay_offline_through_the_real_adapter_and_entries_are_unverified_live():
    manifest = h.manifest()
    assert manifest["pack_id"] == "technology-ai-models" and manifest["version"] == "1.0.0"
    assert {s["source_id"] for s in manifest["sources"]} == set(h.SOURCES.values())
    assert {s["mapping"]["target_schema"] for s in manifest["sources"]} == {"noesis-ai-model-record-v2"}
    assert {s["ai_models"]["live_verification"] for s in manifest["sources"]} == {"unverified-live"}
    assert h.source("hub")["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_HF_TOKEN"}
    assert h.source("openml")["auth"] == h.source("epoch")["auth"] == {"kind": "none"}
    replay = SourcePackConformance(h.ROOT).offline(manifest)
    assert replay["valid"] and replay["coverage"]["verified"] == 3


def test_hub_checks_the_organisation_reads_pinned_revisions_and_cards_and_keeps_counts_and_bodies_out():
    reader = h.adapter("hub")
    statements = [r["ai_statement"] for page in h.fetch("hub") for r in page]
    revisions = {s["sha"]: s for s in statements if s["record_type"] == "hub_repository_revision"}
    assert set(revisions) == {A, B, C, D}
    assert revisions[A]["declared"]["licence"] == {"license": "apache-2.0", "stated_in": "card front matter"}
    assert revisions[B]["declared"]["licence"] == {"license": "other", "license_name": "fixture-model-licence-1.0",
                                                   "license_link": "LICENSE", "stated_in": "card front matter"}
    assert revisions[C]["card"]["status"] == "none" and revisions[C]["declared"]["licence"]["stated_in"] == \
        "repository card data"
    assert revisions[A]["identifiers"]["arxiv"] == ["2095.01234"]
    assert revisions[D]["identifiers"]["doi"] == ["10.99999/fixture-corpus-2095"]
    assert revisions[D]["identifiers"]["openml_dataset"] == ["990061"]
    assert revisions[C]["identifiers"]["packages"] == ["pkg:pypi/fixture-lib"]
    assert revisions[A]["declared"]["library_name"] == "fixture-lib"  # stated text, never a package link by itself
    assert "authors" not in revisions[A]["card"]["front_matter"]
    assert reader.describe()["ai_models"]["caps"]["card_bytes"] == 1_000_000
    pages = h.fetch("hub")
    header = pages[0][0]["ai_unit"]
    assert header["evidence_origin"] == "fixture" and header["live_verification"] == "unverified-live"
    assert all(len(p) <= h.source("hub")["budgets"]["max_results"] for p in pages)


def test_a_user_namespace_is_refused_before_anything_of_the_repository_is_read():
    item = json.loads(json.dumps(h.source("hub")))
    item["ai_models"]["selection"]["repositories"] = [{"repo_id": "example-user/fixture-personal-model",
                                                       "kind": "model", "revisions": []}]
    seen = []
    base = fixture_transport([{"request": "huggingface.co/api/organizations/example-user/overview", "status": 404}])

    def transport(**kwargs):
        seen.append(kwargs["url"])
        return base(**kwargs)

    with pytest.raises(SourcePackError) as caught:
        h.fetch("hub", item=item, transport=transport)
    assert caught.value.code == "user_namespace"
    assert seen == ["https://huggingface.co/api/organizations/example-user/overview"]
    bare = json.loads(json.dumps(h.source("hub")))
    bare["ai_models"]["selection"]["repositories"] = [{"repo_id": "fixture-model", "kind": "model"}]
    with pytest.raises(SourcePackError, match="organisation namespace"):
        AiModelsAdapter(bare, transport=transport)


def test_the_optional_token_only_goes_in_a_header_never_unlocks_gated_content_and_is_never_recorded():
    headers = []
    base = fixture_transport(h.pages("hub", revision=True))

    def transport(**kwargs):
        headers.append(dict(kwargs["headers"]))
        return base(**kwargs)

    records = [r for page in h.fetch("hub", revision=True, transport=transport) for r in page]
    assert {x.get("Authorization") for x in headers} == {f"Bearer {FIXTURE_SECRET}"}
    assert FIXTURE_SECRET not in json.dumps(records)
    gated = [r["ai_statement"] for r in records if r["ai_statement"].get("repo_id") == h.SMALL]
    assert [s["state"] for s in gated] == ["withdrawn"] and gated[0]["state_detail"]["gated"] == "manual"
    # Gated content is not read further, token or not: no refs, revision or card request for the gated repository.
    requested = []
    base2 = fixture_transport(h.pages("hub", revision=True))

    def tracing(**kwargs):
        requested.append(kwargs["url"])
        return base2(**kwargs)

    h.fetch("hub", revision=True, transport=tracing)
    assert [u for u in requested if "fixture-small-model" in u] == [
        "https://huggingface.co/api/models/example-org/fixture-small-model"]
    assert h.adapter("openml", secret="ignored").secret is None  # OpenML never sends a credential


def test_requests_are_paced_at_one_per_second_and_429_gets_exactly_one_retry():
    clock = {"t": 0.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["t"] += seconds

    h.fetch("openml", sleep=sleep, clock=lambda: clock["t"])
    assert sleeps and all(abs(s - 1.0) < 1e-9 for s in sleeps)  # 6 requests, 5 waits of one second
    assert len(sleeps) == 5
    pages = h.pages("openml")
    calls = {"n": 0}
    base = fixture_transport(pages)

    def limited(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": 429, "headers": {"Retry-After": "2"}, "content": b""}
        return base(**kwargs)

    waits = []
    reader = h.adapter("openml", transport=limited, sleep=waits.append)
    page = reader.fetch_page({"operation": "registry", "parameters": {}, "limit": 200}, cursor=None)
    assert page.receipt["retries"] == 1 and 2.0 in waits
    always = h.adapter("openml", transport=lambda **_: {"status": 429, "headers": {"Retry-After": "3"},
                                                        "content": b""})
    with pytest.raises(SourcePackError) as caught:
        always.fetch_page({"operation": "registry", "parameters": {}, "limit": 200}, cursor=None)
    assert caught.value.code == "rate_limited" and always.requests == 2


def test_a_rename_redirect_is_recorded_and_never_followed_to_another_host():
    pages = h.pages("hub")
    renamed = [{**p, "final_url": "https://huggingface.co/api/models/example-org/fixture-model-renamed"}
               if p["request"] == f"huggingface.co/api/models/{h.MODEL}" else p for p in pages]
    statements = h.statements("hub", transport=fixture_transport(renamed))
    stated = [s for s in statements if s.get("repo_id") == h.MODEL]
    assert stated == [{"record_type": "hub_repository_revision", "source": "huggingface-hub", "kind": "model",
                       "repo_id": h.MODEL, "organisation": "example-org", "state": "renamed",
                       "state_detail": {"renamed_to": "example-org/fixture-model-renamed",
                                        "basis": "the source redirected the repository id (same host); the target "
                                                 "is recorded and not acquired"},
                       "url": f"https://huggingface.co/{h.MODEL}"}]
    elsewhere = [{**p, "final_url": "https://cdn.example.org/api/models/x"}
                 if p["request"] == f"huggingface.co/api/models/{h.MODEL}" else p for p in pages]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("hub", transport=fixture_transport(elsewhere))
    assert caught.value.code == "network_policy"


def test_declarations_enforce_the_audited_caps():
    item = json.loads(json.dumps(h.source("hub")))
    item["ai_models"]["selection"]["repositories"] = [
        {"repo_id": f"example-org/fixture-{n}", "kind": "model"} for n in range(6)]
    with pytest.raises(SourcePackError, match="1-5 repositories"):
        AiModelsAdapter(item, transport=fixture_transport([]))
    item["ai_models"]["selection"]["repositories"] = [
        {"repo_id": "example-org/fixture-model", "kind": "model", "revisions": [f"{n:040x}" for n in range(11)]}]
    with pytest.raises(SourcePackError, match="at most 10"):
        AiModelsAdapter(item, transport=fixture_transport([]))
    openml = json.loads(json.dumps(h.source("openml")))
    openml["ai_models"]["selection"]["datasets"] = [1, 2, 3]
    with pytest.raises(SourcePackError, match="1-2"):
        AiModelsAdapter(openml, transport=fixture_transport([]))
    openml = json.loads(json.dumps(h.source("openml")))
    openml["ai_models"]["selection"]["evaluation_limit"] = 101
    with pytest.raises(SourcePackError, match="100"):
        AiModelsAdapter(openml, transport=fixture_transport([]))
    keyed = json.loads(json.dumps(h.source("openml")))
    keyed["auth"] = {"kind": "optional-secret", "secret_ref": "NOESIS_OPENML_KEY"}
    with pytest.raises(SourcePackError, match="upload key is never configured"):
        AiModelsAdapter(keyed, transport=fixture_transport([]))
    epoch = json.loads(json.dumps(h.source("epoch")))
    epoch["ai_models"]["selection"]["models"] = [f"Model {n}" for n in range(21)]
    with pytest.raises(SourcePackError, match="1-20"):
        AiModelsAdapter(epoch, transport=fixture_transport([]))


def test_openml_reads_without_a_key_and_drops_creators_contributors_and_uploaders():
    headers = []
    base = fixture_transport(h.pages("openml"))

    def transport(**kwargs):
        headers.append(dict(kwargs["headers"]))
        return base(**kwargs)

    records = [r for page in h.fetch("openml", transport=transport) for r in page]
    assert not any("Authorization" in x for x in headers)
    text = json.dumps(records)
    for dropped in ("Ada Example", "Bob Example", "uploader", "990001", "Fictional corpus description", "citation"):
        assert dropped not in text, dropped
    evaluations = [r["ai_statement"] for r in records if r["ai_statement"].get("observation_kind")]
    assert [e["run_id"] for e in evaluations] == [99000001, 99000002, 99000003]
    listing = next(r["ai_statement"] for r in records if r["ai_statement"]["record_type"] == "evaluation_listing")
    assert listing["complete"] is True and listing["run_ids"] == [99000001, 99000002, 99000003]
    assert "API v1" in " ".join(PROVIDER_CONTRACTS["openml"]["verify"])


def test_epoch_keeps_declared_rows_with_confidence_and_dates_by_stamp_or_retrieval_time():
    first = h.fetch("epoch")
    header = first[0][0]["ai_unit"]
    assert header["vintage"]["release_basis"] == "page_last_updated"
    assert header["vintage"]["last_updated"] == "2096-01-15T00:00:00Z"
    rows = [r["ai_statement"] for r in first[0] if r["ai_statement"]["record_type"] == "epoch_model_revision"]
    assert [r["model"] for r in rows] == ["Fixture Model", "Fixture Small Model"]
    assert "Ada Example" not in json.dumps(first) and "A fictional abstract" not in json.dumps(first)
    assert rows[0]["identifiers"]["hub"] == ["model:example-org/fixture-model"]
    second = h.fetch("epoch", revision=True)
    assert second[0][0]["ai_unit"]["vintage"]["release_basis"] == "retrieval_time"
    listing = second[0][-1]["ai_statement"]
    assert listing["present"] == ["Fixture Model"] and listing["declared"] == ["Fixture Model", "Fixture Small Model"]
    big = h.adapter("epoch", transport=lambda **_: {"status": 200, "content": b"x" * 20_000_001})
    with pytest.raises(SourcePackError) as caught:
        big.fetch_page({"operation": "registry", "parameters": {}, "limit": 40}, cursor=None)
    assert caught.value.code == "response_too_large"


def test_card_front_matter_digest_and_identifier_helpers():
    raw = b"---\nlicense: mit\nauthors: [Ada Example]\nmodel-index: []\n---\nBody prose.\n"
    card = split_card(raw)
    assert card["front_matter"] == {"license": "mit"} and len(card["body_sha256"]) == 64
    assert "Body prose" not in json.dumps(card) and card["bytes"] == len(raw)
    assert stated_identifiers(["arxiv:2095.01234", "doi:10.99999/X.Y", "https://www.openml.org/d/42",
                               "https://huggingface.co/datasets/example-org/fixture-corpus", "pkg:npm/fixture"]) == {
        "arxiv": ["2095.01234"], "doi": ["10.99999/x.y"], "openml_dataset": ["42"], "packages": ["pkg:npm/fixture"],
        "hub": ["dataset:example-org/fixture-corpus"]}
