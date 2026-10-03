"""Media metadata MCP entry points: registration, declared scopes, exclusions and answers (MM11, #2508)."""

from __future__ import annotations


import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import media_metadata_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.cultural import CULTURAL_TOOLS, MEDIA_SCOPES, MEDIA_TOOLS, MEDIA_WRITES
from src.mcp_host.introspection import tool_map

NAMESPACE = {"namespace:global:read", "namespace:global:write"}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "media-mcp.duckdb")
    env = h.Env(duckdb.connect(path))
    assert env.run()["status"] == "complete"
    env.cultural_object("FIXTUREDDBMEDIA0000000000000001", "Portrait photograph",
                        creators=[{"name": "Quell, Mara", "role": "creator",
                                   "authority_id": f"https://d-nb.info/gnd/{h.GND}"}])
    env.canonical_entity("ent-mara-quell", "Mara Quell")
    env.conn.close()
    state = {"principal": "analyst", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_beside_cultural_with_mutability_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert MEDIA_TOOLS <= set(tools) and MEDIA_TOOLS <= CULTURAL_TOOLS
    for name in MEDIA_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in MEDIA_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == MEDIA_SCOPES[name], name
    contracts = " ".join(tools["media_metadata_source_contracts"].description.lower().split())
    assert "no full text" in contracts and "popularity" in contracts and "rights clearance" in contracts
    assert "popularity" in tools["search_media_titles"].description.lower()


def test_every_tool_works_holding_exactly_its_declared_scopes(mcp_env):
    tools, state = mcp_env

    def call(name, **kwargs):
        state["scopes"] = set(MEDIA_SCOPES[name]) | NAMESPACE
        result = tools[name].fn(**kwargs)
        assert result.get("ok") is not False, (name, result)
        return result

    contracts = call("media_metadata_source_contracts")
    assert contracts["contracts"]["musicbrainz"]["status"] == "unverified-live"
    status = call("media_metadata_status")
    assert {s["LIVE_VERIFICATION"] for s in status["sources"]} == {"unverified-live"}
    assert len(status["sources"]) == 7 and status["features"] == {"media-metadata": False,
                                                                  "media-metadata-news": False}
    proposed = call("propose_media_identity_matches", namespace="global")
    assert proposed["conflicts"] and proposed["matches"]
    listed = call("list_media_identity_matches", namespace="global", record=f"wikidata:{h.AUTHOR_QID}")
    assert listed["matches"]
    candidate = next(m for m in proposed["matches"] if m["candidate_state"] == "candidate"
                     and m["right_kind"] == "canonical-entity")
    reviewed = call("review_media_identity_match", namespace="global", match_id=candidate["match_id"],
                    decision="accepted", reason="same person per GND")
    assert reviewed["state"] == "accepted"
    assert call("revert_media_identity_match", namespace="global", match_id=candidate["match_id"],
                reason="check again")["state"] == "reverted"
    cultural = call("link_media_cultural_objects", namespace="global")
    assert [link["basis"] for link in cultural["links"] if link["state"] == "linked"] == ["identifier"]
    assert {link["basis"] for link in cultural["links"] if link["state"] != "linked"} <= {"keyword-overlap"}
    news = call("link_media_news_entities", namespace="global")
    assert news["links"] and {link["state"] for link in news["links"]} == {"candidate"}
    reject = call("review_media_link", namespace="global", link_id=news["links"][0]["link_id"],
                  decision="rejected", reason="not the journalist of the same name")
    assert reject["state"] == "rejected"
    asserted = call("assert_media_link", namespace="global", subject_kind="news-entity", subject_id="ent-mara-quell",
                    record=f"wikidata:{h.AUTHOR_QID}", evidence="interview names the novel")
    assert asserted["basis"] == "reviewed-assertion"
    answer = call("resolve_media_identifier", namespace="global", identifier=h.SECOND_ISBN)
    assert answer["status"] == "resolved"
    assert call("search_media_titles", namespace="global", title="The Glass Orchard")["status"] == "candidates"
    history = call("media_authority_history", namespace="global", record=f"wikidata:{h.AUTHOR_QID}")
    assert [r["revision_marker"] for r in history["revisions"]] == ["2000000001", "2100000001"]
    bundle = call("export_media_evidence_bundle", namespace="global", identifier=h.AUTHOR_QID)
    assert bundle["contract"] == "noesis-evidence-bundle-v1"
    created = call("create_media_metadata_monitor", namespace="global", request_key="k",
                   identifiers=[h.OLD_RECORDING_MBID])
    ran = call("run_media_metadata_monitor", subscription_id=created["subscription_id"])
    assert "merge" in {n["kind"] for n in ran["notifications"]}
    assert call("poll_media_metadata_monitor", subscription_id=created["subscription_id"])["events"]


def test_reads_without_the_cultural_scope_are_refused(mcp_env):
    tools, state = mcp_env
    state["scopes"] = NAMESPACE
    refused = tools["resolve_media_identifier"].fn(namespace="global", identifier=h.AUTHOR_QID)
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
