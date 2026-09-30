"""Capture-to-cited-URL matching through canonicalisation and reviewable identity (#2295, WA08)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.kb.citation_preservation import READ_SCOPE, CitationPreservationError
from src.kb.review_targets import ReviewTargets
from src.kb.web_archive_identity import CANONICALISATION_VERSION, RULE_IDS, CaptureMatcher, canonicalize
from tests.unit import web_archive_harness as h

SCHEMA = json.loads((Path(__file__).resolve().parents[3]
                     / "contracts/schemas/jsonschema/noesis-web-archive-match-v1.json").read_text())


@pytest.mark.parametrize(("variant", "rules"), [
    ("https://example.org/report", []),
    ("https://example.org/report/", ["strip-trailing-slash"]),
    ("https://www.example.org/report", ["strip-www"]),
    ("http://example.org/report", ["scheme-equivalence"]),
    ("https://EXAMPLE.org:443/report#section-2", ["lowercase-scheme-host", "drop-default-port", "drop-fragment"]),
    ("https://example.org/report?utm_source=x&fbclid=1", ["drop-tracking-parameters"]),
])
def test_canonicalisation_rules_are_explicit_and_versioned(variant, rules):
    canonical, applied = canonicalize(variant)
    assert canonical == "https://example.org/report" and applied == rules
    assert set(applied) <= set(RULE_IDS) and CANONICALISATION_VERSION == "wa-canon-v1"


def test_query_parameter_order_and_root_paths():
    assert canonicalize("https://example.org/r?b=2&a=1")[0] == canonicalize("https://example.org/r?a=1&b=2")[0]
    assert "sort-query-parameters" in canonicalize("https://example.org/r?b=2&a=1")[1]
    assert canonicalize("https://example.org")[0] == canonicalize("https://example.org/")[0] == "https://example.org/"
    assert canonicalize("https://example.org/a")[0] != canonicalize("https://example.org/b")[0]


def _seed(conn):
    client = h.client(conn)
    client.resolve_archive(h.URL, "internet-archive", request_id="ia")
    # A capture of an old URL for which the archive published a redirect to the cited page.
    client.record({"archive_id": "internet-archive", "uri_r": "https://example.org/old-report",
                   "uri_m": "https://web.archive.org/web/20231001000000/https://example.org/old-report",
                   "memento_datetime": "20231001000000", "status": 301,
                   "archive_redirect": {"status": 301, "location": "https://example.org/report"},
                   "receipt": {"request_id": "manual"}})
    return client


def test_exact_canonicalised_and_redirect_derived_matches_are_distinguished():
    conn = h.connect()
    client = _seed(conn)
    matcher = CaptureMatcher(conn, now=h.clock())
    exact = matcher.propose(h.NS, "cite:exact", h.URL, principal_id="alice", scopes=h.SCOPES)
    assert {m["match_kind"] for m in exact["matches"]} == {"exact", "redirect-derived"}
    assert all(m["state"] == "accepted" for m in exact["matches"] if m["match_kind"] == "exact")
    variant = matcher.propose(h.NS, "cite:variant", h.CITED, principal_id="alice", scopes=h.SCOPES)
    kinds = {m["match_kind"] for m in variant["matches"]}
    assert kinds == {"canonicalised", "redirect-derived"} and variant["pending_review"] == len(variant["matches"])
    canonicalised = next(m for m in variant["matches"] if m["match_kind"] == "canonicalised")
    assert canonicalised["rules"] == ["strip-www", "strip-trailing-slash", "drop-tracking-parameters"]
    assert canonicalised["rule_version"] == CANONICALISATION_VERSION
    Draft202012Validator(SCHEMA).validate(canonicalised)
    redirect = next(m for m in variant["matches"] if m["match_kind"] == "redirect-derived")
    assert redirect["rules"][0] == "archive-reported-redirect"
    # The redirect is recorded as published; the capture still names its own URI-R.
    assert redirect["uri_r"] == "https://example.org/old-report"
    assert redirect["archive_redirect"] == {"status": 301, "location": "https://example.org/report"}
    assert matcher.propose(h.NS, "cite:variant", h.CITED, principal_id="alice",
                           scopes=h.SCOPES)["matches"][0]["idempotent"] is True
    assert client.store.captures_for_url(h.NS, "https://example.org/reports/2024", scopes={READ_SCOPE}) == []


def test_non_exact_matches_are_reviewed_and_rejected_matches_never_pin():
    conn = h.connect()
    client = _seed(conn)
    matcher = CaptureMatcher(conn, now=h.clock())
    proposal = matcher.propose(h.NS, "cite:v", h.CITED, principal_id="alice", scopes=h.SCOPES)
    canonical = [m for m in proposal["matches"] if m["match_kind"] == "canonicalised"]
    first, second = canonical[0], canonical[1]
    with pytest.raises(CitationPreservationError) as pending:
        client.store.pin_citation(h.NS, "cite:v", first["capture_id"], h.CITED, principal_id="alice",
                                  scopes=h.SCOPES)
    assert pending.value.code == "match_unreviewed"
    with pytest.raises(CitationPreservationError) as own:
        matcher.review(h.NS, first["match_id"], "accept", "same page", principal_id="alice", scopes=h.SCOPES)
    assert own.value.code == "self_review"
    rejected = matcher.review(h.NS, second["match_id"], "reject", "different edition", principal_id="bob",
                              scopes=h.SCOPES)
    assert rejected["state"] == "rejected" and rejected["review"]["reviewer_id"] == "bob"
    with pytest.raises(CitationPreservationError) as refused:
        client.store.pin_citation(h.NS, "cite:v", second["capture_id"], h.CITED, principal_id="alice",
                                  scopes=h.SCOPES)
    assert refused.value.code == "match_rejected"
    # Review through the shared review-target flow.
    targets = ReviewTargets(conn)
    target = {"kind": "archive_match", "namespace": h.NS, "id": first["match_id"]}
    state = targets.inspect(target, scopes=h.SCOPES)
    assert state["record"]["match_kind"] == "canonicalised" and state["context"]["capture"]["archive_id"]
    accepted = targets.route(target, {"decision": "accept"}, rationale="same page, tracking parameters only",
                             principal_id="bob", scopes=h.SCOPES, task_id="task:1")
    assert accepted["state"] == "accepted"
    pin = client.store.pin_citation(h.NS, "cite:v", first["capture_id"], h.CITED, principal_id="alice",
                                    scopes=h.SCOPES)
    assert pin["match"]["match_kind"] == "canonicalised" and pin["cited_url"] == h.CITED
