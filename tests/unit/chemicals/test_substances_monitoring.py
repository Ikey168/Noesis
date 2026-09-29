"""Classification, SVHC and restriction changes through subscriptions (CH10, #2309)."""

from __future__ import annotations

import pytest

from src.ingestion.substance_sources import fixture_transport
from src.kb.substances_links import SubstanceLinks
from src.kb.substances_monitoring import SubstanceMonitor
from src.kb.substances_records import SubstanceError
from tests.unit.chemicals import harness as h

NS = h.NS
WATCHED = ["echa:substance:100.001.133", "echa:substance:100.003.829", "echa:substance:100.000.526"]
ECHA = ["echa-clp-classifications", "echa-reach-lists"]


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    item.monitor = SubstanceMonitor(item.conn, now=lambda: next(item.clock))
    item.subscription = item.monitor.create(NS, "watch-1", substances=WATCHED, principal_id="analyst",
                                            scopes=h.ALL)["subscription_id"]
    yield item
    item.conn.close()


def run(env):
    return env.monitor.run(env.subscription, principal_id="analyst", scopes=h.ALL)


def test_the_monitor_is_a_knowledge_subscription_and_the_first_run_is_a_baseline(env):
    subscription = env.monitor.subscriptions.inspect(env.subscription, principal_id="analyst", scopes=h.ALL)
    assert subscription["domain"] == "chemicals" and subscription["query"]["kind"] == "substance-monitor"
    assert subscription["cadence"] == {"trigger": "watermark", "source_pack": "chemicals-substances"}
    first = run(env)
    assert first["baseline"] and first["notifications"]
    assert {n["kind"] for n in first["notifications"]} >= {"classification_revision", "candidate_list_inclusion",
                                                           "authorisation_inclusion", "restriction_inclusion"}
    replay = run(env)
    assert replay["status"] == "replayed" and replay["notifications"] == []  # deduplicated


def test_new_atp_inclusion_amendment_and_removal_show_prior_and_new_status_with_citations(env):
    run(env)
    assert env.run("second", source_ids=ECHA, overrides=h.LATER["second"])["status"] == "complete"
    second = run(env)
    assert not second["baseline"]
    kinds = {n["kind"]: n for n in second["notifications"]}
    assert set(kinds) == {"classification_revision", "authorisation_amendment", "candidate_list_inclusion"}
    atp = kinds["classification_revision"]
    assert atp["prior"]["citation"]["legal_act"]["celex"] == "32016R1179"
    assert atp["prior"]["effective_from"] == "2018-03-01"
    assert atp["new"]["citation"]["legal_act"]["celex"] == "32026R0000" and atp["new"]["effective_from"] == "2028-03-01"
    amendment = kinds["authorisation_amendment"]
    assert (amendment["prior"]["citation"]["legal_act"]["celex"], amendment["new"]["citation"]["legal_act"]["celex"]) \
        == ("32011R0143", "32020R0171")
    assert amendment["new"]["as_published"]["sunset_date"] == "2023-01-27"
    inclusion = kinds["candidate_list_inclusion"]
    assert inclusion["prior"] == {"state": "none on record", "effective_from": None, "citation": None}
    assert inclusion["new"]["citation"]["url"].endswith("/api-lists/v1/candidate-list/100.000.526")
    assert all("advice" not in n["message"].lower() for n in second["notifications"])
    assert env.run("third", source_ids=ECHA, overrides=h.LATER["third"])["status"] == "complete"
    third = run(env)
    (removal,) = third["notifications"]
    assert removal["kind"] == "candidate_list_removal" and removal["new"]["effective_from"] == "2026-10-01"
    assert removal["prior"]["state"] == "inclusion" and removal["prior"]["effective_from"] == "2026-09-15"
    assert run(env)["notifications"] == []


def test_a_partial_run_is_never_evaluated(env):
    run(env)
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    source = next(s for s in installed["sources"] if s["source_id"] == "echa-reach-lists")
    native = h.pages("echa-reach-lists")
    for page in native:
        if page["request"] in h.LATER["second"]:
            page["body"] = h.LATER["second"][page["request"]]
    native[-1]["status"] = 503
    adapters = {"echa-reach-lists": env.runtime.factory.compile(source, transport=fixture_transport(native))}
    result = env.run("partial", source_ids=["echa-reach-lists"], adapters=adapters)
    assert result["status"] != "complete"
    with pytest.raises(SubstanceError) as caught:
        mark = env.conn.execute("SELECT max(watermark) FROM source_pack_watermarks WHERE pack_id=?",
                                ["chemicals-substances"]).fetchone()[0]
        env.monitor.run(env.subscription, mark, principal_id="analyst", scopes=h.ALL)
    assert caught.value.code == "incomplete_run"
    assert run(env)["status"] == "replayed"  # the latest complete run is still the baseline


def test_a_newly_linked_notice_is_delivered_once(env):
    run(env)
    env.seed_notices()
    SubstanceLinks(env.conn).link_product_notices(NS, scopes=h.ALL, principal_id="linker")
    linked = run(env)
    assert linked["notifications"] and {n["kind"] for n in linked["notifications"]} == {"linked_notice"}
    assert {n["watched"] for n in linked["notifications"]} == {"echa:substance:100.001.133",
                                                               "echa:substance:100.003.829"}
    assert all(n["new"]["citation"]["citing_text"] for n in linked["notifications"])
    assert run(env)["notifications"] == []
    polled = env.monitor.poll(env.subscription, principal_id="analyst", scopes=h.ALL)
    assert polled["events"] and all(e["event_type"] == "added" for e in polled["events"])
