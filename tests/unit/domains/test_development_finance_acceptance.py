"""Offline funder-to-activities acceptance for the Funding & Grants development-finance feature (D11, #2038).

The pinned IATI (two publishers, two versions of one publisher's file), World
Bank and OECD CRS fixtures (authored, fictional organisations, activities and
amounts) run through the real clients on an injected DurableHTTP transport and
the real source-pack adapter, with the ``development-finance`` feature selected
in the composition plan. This is offline evidence only; it is never live
provider coverage (see ``docs/development/development-finance-evidence/``).
"""

from __future__ import annotations

import socket

import pytest

from src.kb.development_finance import (
    DevelopmentFinanceError,
    DevelopmentFinanceStore,
    feature_enabled,
    readiness,
)
from src.kb.development_finance_identity import DevelopmentFinanceIdentity, subject_key
from src.kb.development_finance_monitoring import DevelopmentFinanceMonitor
from src.kb.development_finance_normalise import DevelopmentFinanceNormaliser
from src.kb.development_finance_queries import DevelopmentFinanceQueries
from src.kb.funding_bundle import readiness as funding_readiness
from tests.unit.composition.test_migration import _migrated
from tests.unit.funding import development_finance_harness as h

WATER = "XM-DAC-99901-FICT-0001"
FDPA_ID = "iati:ref:XM-DAC-99901"
NGO_ID = "iati:ref:XI-IATI-FICTNGO"
FUNDER = "devfin:publisher:" + FDPA_ID


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def enabled_env():
    env = h.Env()
    _, coordinator, bundles, _ = _migrated(env.conn)
    coordinator.select(
        "funding-grants",
        bundles["funding-grants"]["version"],
        features=["development-finance"],
    )
    coordinator.activate("development-finance-acceptance")
    assert feature_enabled(env.conn) is True
    return env


def test_funder_to_cited_activities_with_coverage_vintages_identity_and_monitoring():
    env = enabled_env().load()
    h.seed_ownership(env.conn)
    h.register_places(env.conn, now=env.now())
    store = DevelopmentFinanceStore(env.conn)
    queries = DevelopmentFinanceQueries(env.conn, now=env.now)

    # Normalisation: codes from published code lists, places by code, nothing edited.
    normaliser = DevelopmentFinanceNormaliser(env.conn, now=env.now)
    normaliser.publish_codelists(principal_id="operator", scopes=h.SCOPES)
    normalised = normaliser.normalise_current(h.NS, scopes=h.SCOPES)
    assert (
        len([n for n in normalised if n["activity_key"].endswith(WATER)]) == 2
    )  # one per publisher
    places = {
        link["code"]: link["state"]
        for link in normaliser.resolve_places(
            h.NS, principal_id="analyst", scopes=h.SCOPES
        )["links"]
        if link["code"]
    }
    assert places == {"KE": "resolved", "UG": "resolved", "289": "aggregate"}

    # Identity: the reported funder reference is reviewed onto the publisher; the implementer onto its register entry.
    identity = DevelopmentFinanceIdentity(env.conn, now=env.now)
    proposed = identity.propose(
        h.NS, principal_id="analyst", scopes=h.SCOPES, ownership_namespace=h.NS
    )
    for candidate in proposed["candidates"]:
        if (
            FUNDER in candidate["records"]
            or "companies-house:99000001" in candidate["records"]
        ):
            identity.service.review(
                h.NS,
                candidate["candidate_id"],
                "accept",
                "stated identifier",
                principal_id="reviewer",
                scopes=h.REVIEW_SCOPES,
            )

    # The monitor watches the funder before the next publications arrive.
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    subscription = monitor.create(
        h.NS,
        "acceptance",
        filters={
            "funders": [FUNDER],
            "crs_cells": [
                store.crs_cells(h.NS, recipient="KEN", price_basis="current")[0][
                    "cell_id"
                ]
            ],
        },
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    first = monitor.run(
        subscription["subscription_id"], principal_id="analyst", scopes=h.SCOPES
    )

    # Funder to activities as of April: both publishers' reports, cited, with coverage; never summed.
    april = queries.list_activities(
        h.NS,
        {"funder": FUNDER, "as_of": "2098-04-30", "crs_recipient": "KEN"},
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    publishers = {p["publisher_id"]: p for p in april["publishers"]}
    assert set(publishers) == {FDPA_ID, NGO_ID}
    for publisher in publishers.values():
        assert (
            publisher["coverage"]["selections"] and not publisher["coverage"]["stale"]
        )
        for activity in publisher["activities"]:
            cites = activity["cites"]
            assert (
                cites["publisher_id"] == publisher["publisher_id"]
                and cites["revision_id"]
                and cites["dataset_id"]
            )
    assert april["conflicts"][0]["publishers"] == [NGO_ID, FDPA_ID]
    assert april["crs"] == []  # the CRS vintage was observed after the as-of date
    unmatched = {
        o["record_key"]: o for o in april["unknowns"]["unmatched_organisations"]
    }
    trust = subject_key(FDPA_ID, None, "Fictional Learning Trust")
    assert unmatched[trust]["as_reported"] == "Fictional Learning Trust"

    transactions = queries.search_transactions(
        h.NS, {"iati_identifier": WATER}, principal_id="analyst", scopes=h.SCOPES
    )
    totals = {p["publisher_id"]: p["totals"] for p in transactions["publishers"]}
    assert set(totals) == {FDPA_ID, NGO_ID}  # never one number across publishers

    # CRS: the vintage in force on each date; statistics, never activities.
    env.at("2099-07-20T08:00:00").crs(
        "crs_deu_ken_140_2099-07.csv",
        release={"label": "CRS release 2099-07", "published_on": "2099-07-15"},
    )
    for as_of, expected in (("2099-02-01", "2099-01-15"), ("2099-08-01", "2099-07-15")):
        cells = queries.crs_aggregates(
            h.NS,
            {"recipient": "KEN", "price_basis": "current", "as_of": as_of},
            principal_id="analyst",
            scopes=h.SCOPES,
        )["cells"]
        assert cells[0]["vintage"]["published_on"] == expected

    # World Bank: an explicit link by a stated identifier, a candidate only by similarity, one without counterpart.
    links = queries.world_bank_links(h.NS, scopes=h.SCOPES)
    states = {(link["project_id"], link["state"]) for link in links["links"]}
    assert ("P999001", "linked") in states and ("P999002", "candidate") in states
    assert ("P999002", "linked") not in states and links["unlinked_projects"] == [
        "P999003"
    ]

    # An amount without a citable rate stays unconverted; no default rate is assumed.
    ngo_income = next(
        tx
        for p in transactions["publishers"]
        if p["publisher_id"] == NGO_ID
        for tx in p["transactions"]
    )
    with pytest.raises(DevelopmentFinanceError):
        normaliser.convert(
            h.NS,
            ngo_income["transaction_id"],
            target_currency="USD",
            rate="1.08",
            rate_date="2098-02-22",
            rate_source="",
            citation="",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    assert (
        normaliser.conversions(h.NS, ngo_income["transaction_id"], scopes=h.SCOPES)
        == []
    )

    # Publisher A's June file: a corrected disbursement, a new one, a result posting, a withdrawal and a new activity.
    env.at("2099-08-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    heard = monitor.run(
        subscription["subscription_id"], principal_id="analyst", scopes=h.SCOPES
    )
    kinds = {n["kind"] for n in heard["notifications"]}
    assert {
        "corrected_transaction",
        "new_transaction",
        "new_result_posting",
        "retracted_activity",
        "new_activity",
        "new_crs_vintage",
    } <= kinds
    inspected = queries.inspect_activity(
        h.NS, WATER, principal_id="analyst", scopes=h.SCOPES
    )
    fdpa = next(r for r in inspected["reports"] if r["publisher_id"] == FDPA_ID)
    assert [c["revision_no"] for c in fdpa["history"]] == [1, 2]
    assert {c["change"] for c in fdpa["transaction_changes"]} == {"corrected", "new"}

    # Re-ingestion is idempotent and a restarted monitor resumes from its recorded watermark.
    revisions = env.conn.execute(
        "SELECT count(*) FROM devfin_activity_revisions"
    ).fetchone()[0]
    again = env.at("2099-08-06T08:00:00").iati(
        "iati_fdpa_2098-06.xml", observation="r3:fdpa"
    )
    assert set(again["counts"]) == {"unchanged"}
    assert (
        env.conn.execute("SELECT count(*) FROM devfin_activity_revisions").fetchone()[0]
        == revisions
    )
    restarted = DevelopmentFinanceMonitor(env.conn, now=env.now)
    replay = restarted.run(
        subscription["subscription_id"], principal_id="analyst", scopes=h.SCOPES
    )
    assert (
        replay["status"] == "replayed"
        and replay["watermark"] == heard["watermark"] > first["watermark"]
    )
    assert replay["notifications"] == []

    # The answer is a receipt a research project pins and a report cites.
    recorded = queries.list_activities(
        h.NS, {"funder": FUNDER}, principal_id="analyst", scopes=h.SCOPES
    )
    stored = queries.answer(h.NS, recorded["receipt"]["answer_id"], scopes=h.SCOPES)
    assert stored["request"] == {"funder": FUNDER} and stored["coverage"]
    assert "Published aid activities" in recorded["boundary"]


def test_offline_and_live_evidence_are_reported_separately():
    env = h.Env().load()
    report = readiness(env.conn, h.NS)
    assert report["providers"]["iati-datastore"]["live"] == "outstanding"
    assert report["providers"]["transparenzportal-bund"]["live"] == "not-implemented"
    origins = {
        r[0]
        for r in env.conn.execute(
            "SELECT DISTINCT evidence_origin FROM devfin_datasets"
        ).fetchall()
    }
    assert origins == {"fixture"}
    evidence = h.ROOT / "docs/development/development-finance-evidence"
    assert (evidence / "README.md").exists()
    assert not list(
        evidence.glob("live-check-*.json")
    )  # no dated live run yet (D12, #2039)
    assert "never recorded here" in (evidence / "README.md").read_text()


def test_with_the_feature_disabled_the_funding_bundle_is_unchanged():
    env = h.Env()
    conn, coordinator, _, _ = _migrated(env.conn)
    assert feature_enabled(conn) is False
    plan = coordinator.active()["plan"]
    assert "funding.development-finance" not in {
        b["provider"] for b in plan["bindings"]
    }
    status = funding_readiness(
        conn, "funding", scopes={"operator", "knowledge:funding:read"}
    )
    assert "funding.development-finance" not in {
        o["provider"] for o in status["composition"]["operations"]
    }
    acceptance = (h.ROOT / "tests/unit/funding/test_funding_acceptance.py").read_text()
    assert "development_finance" not in acceptance and "devfin" not in acceptance
