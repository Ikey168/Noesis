"""Boilerplate exclusion, the contradiction subject gate, and stored Jev verdicts."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import duckdb
import pytest

from src.argument_mining.boilerplate import boilerplate_reason
from src.kb.claim_exclusions import (
    restore_claims,
    run_claim_exclusion_pass,
    undo_claim_exclusions,
)
from src.kb.claim_link_review import JevClaimLinkReviewer
from src.kb.claim_links import ensure_claim_link_schema, run_claim_linking_pass
from src.kb.claim_subjects import claim_subjects, subject_gate
from src.kb.jev_verdicts import apply_jev_verdicts, keep_links, undo_jev_verdicts
from src.kb.nli import CONTRADICTION, ENTAILMENT, NEUTRAL, NLIResult
from tests.unit.kb.test_claim_link_review import FakeRuntime, resolver

DAY = 86_400_000
BASE = 1_750_000_000_000


@pytest.fixture()
def conn():
    return duckdb.connect()


def _words(text):
    return set(re.findall(r"[a-z0-9.@-]+", text.lower())) - {"the", "a", "in", "of"}


class WordNLI:
    """Entails when the hypothesis' words are a subset; contradicts on a negation flip."""

    name = "nli:word"
    prediction_mode = "zero-shot:word"
    model_version = "word@1"

    def classify(self, premise, hypothesis):
        p, h = _words(premise), _words(hypothesis)
        if ("not" in p) != ("not" in h) and len((p ^ h) - {"not", "does"}) <= 2:
            return NLIResult(CONTRADICTION, 0.9, self.prediction_mode)
        if h - {"not"} <= p:
            return NLIResult(ENTAILMENT, 0.9, self.prediction_mode)
        return NLIResult(NEUTRAL, 0.6, self.prediction_mode)


def _seed(conn, rows):
    """rows: (claim_id, text, document_id, source_id, ingested_at)."""
    ensure_claim_link_schema(conn)
    for claim_id, text, document_id, source_id, ingested_at in rows:
        conn.execute(
            "INSERT OR IGNORE INTO documents (document_id, source_type, source_id, url, ingested_at)"
            " VALUES (?, 'blog', ?, ?, ?)",
            [document_id, source_id, f"https://{source_id}.example/{document_id}", ingested_at],
        )
        conn.execute(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type, confidence)"
            " VALUES (?, ?, ?, 'blog', 0.8)",
            [claim_id, text, document_id],
        )


def _links(conn):
    return sorted(tuple(r) for r in conn.execute(
        "SELECT claim_a, claim_b, relation FROM claim_links").fetchall())


# -- 1. boilerplate ---------------------------------------------------------------


@pytest.mark.parametrize("text,reason", [
    ("Skip to main content", "navigation"),
    ("Home | Blog | Threat Research | Malware", "navigation"),
    ("Next post: How we tracked the loader campaign", "navigation"),
    ("Read more about the campaign here.", "navigation"),
    ("Facebook Twitter LinkedIn Email", "share"),
    ("Share this post on LinkedIn with your team", "share"),
    ("By Jane Doe, Senior Researcher", "byline"),
    ("Posted by Alex Kim on March 3, 2026", "byline"),
    ("Published on 2026-03-04 by the research team", "byline"),
    ("Subscribe to our newsletter for weekly threat updates.", "marketing"),
    ("Book a demo to see how we stop supply chain attacks.", "marketing"),
    ("It was bad.", "too_short"),
    ("5 min read", "too_short"),
])
def test_furniture_is_recognised(text, reason):
    assert boilerplate_reason(text) == reason


@pytest.mark.parametrize("text", [
    "The malicious package exfiltrated npm tokens to a remote server in March.",
    "The attackers sent phishing emails disguised as a newsletter from the bank.",
    "By March, the botnet had infected more than 40,000 routers worldwide.",
    "By default, the package runs a postinstall script that downloads a binary.",
    "Next, the loader downloads a second-stage payload from GitHub.",
    "Home routers running old firmware were the main target of the campaign.",
    "Victims who clicked the share button were redirected to a credential harvesting page.",
])
def test_real_claims_are_kept(text):
    assert boilerplate_reason(text) is None


def test_extraction_drops_furniture(monkeypatch):
    from services.ingest.common.document_model import Document
    from src.argument_mining import evidence

    sentences = ["Share on Twitter Facebook LinkedIn Email", "The loader writes a scheduled task named Updater.",
                 "Subscribe to our newsletter for weekly updates.", "Ok then."]
    detector = SimpleNamespace(
        prediction_mode="fake",
        predict=lambda document: [
            SimpleNamespace(is_claim=True, text=text, sentence_idx=i, confidence=0.9)
            for i, text in enumerate(sentences)
        ],
    )
    monkeypatch.setattr(evidence, "get_claim_detector", lambda: detector)
    document = Document(document_id="d1", source_type="blog", language="en", ingested_at=0,
                        content=" ".join(sentences))
    kept = [c.claim_text for c in evidence.extract_claims(document)]
    assert kept == ["The loader writes a scheduled task named Updater."]
    assert len(evidence.extract_claims(document, filter_boilerplate=False)) == 4


FOOTER = "Our researchers track threats across every major package registry."


def _site_corpus(conn):
    _seed(conn, [
        ("f1", FOOTER, "d1", "vendor", BASE),
        ("f2", FOOTER, "d2", "vendor", BASE + DAY),
        ("f3", FOOTER.upper(), "d3", "vendor", BASE + 2 * DAY),
        ("o1", FOOTER, "d4", "other", BASE),  # same wording, another site: kept
        ("o2", FOOTER, "d5", "other", BASE + DAY),
        ("s1", "Share this article on LinkedIn now", "d1", "vendor", BASE),
        ("r1", "The event-stream package downloads a payload.", "d1", "vendor", BASE),
        ("r2", "The event-stream package does not download a payload.", "d4", "other", BASE),
    ])


def test_exclusion_pass_marks_repeated_site_wording_and_furniture(conn):
    _site_corpus(conn)
    plan = run_claim_exclusion_pass(conn, dry_run=True)
    assert plan["claims_excluded"] == 4 and plan["by_reason"]["repeated_on_site"] == 3
    assert plan["by_reason"]["share"] == 1
    assert conn.execute("SELECT COUNT(*) FROM argument_claim_exclusions").fetchone()[0] == 0

    summary = run_claim_exclusion_pass(conn, run_id="ex-1")
    excluded = dict(conn.execute("SELECT claim_id, reason FROM argument_claim_exclusions").fetchall())
    assert excluded == {"f1": "repeated_on_site", "f2": "repeated_on_site",
                        "f3": "repeated_on_site", "s1": "share"}
    assert conn.execute("SELECT COUNT(*) FROM argument_claims").fetchone()[0] == 8  # nothing deleted
    assert run_claim_exclusion_pass(conn, run_id="ex-2")["claims_excluded"] == 0  # idempotent
    assert summary["claims_total"] == 8


def test_excluded_claims_are_not_linked_and_their_links_are_withheld_reversibly(conn):
    _site_corpus(conn)
    run_claim_linking_pass(conn, nli=WordNLI())
    before = _links(conn)
    assert ("f1", "f2", "duplicate") in before
    summary = run_claim_exclusion_pass(conn, run_id="ex-1")
    after = _links(conn)
    assert summary["links_withheld"] == len(before) - len(after) > 0
    assert all(not ({a, b} & {"f1", "f2", "f3", "s1"}) for a, b, _ in after)
    assert ("r1", "r2", "contradicts") in after

    # New claims never link to excluded ones.
    _seed(conn, [("f4", FOOTER, "d6", "third", BASE)])
    run_claim_linking_pass(conn, nli=WordNLI())
    assert not [k for k in _links(conn) if "f4" in k[:2] and {"f1", "f2", "f3"} & set(k[:2])]

    undone = undo_claim_exclusions(conn, "ex-1")
    assert undone["claims_restored"] == 4
    assert set(before) <= set(_links(conn))


def test_restore_claims_overrides_future_passes(conn):
    _site_corpus(conn)
    run_claim_linking_pass(conn, nli=WordNLI())
    run_claim_exclusion_pass(conn, run_id="ex-1")
    restored = restore_claims(conn, ["f1", "f2"], note="real claim")
    assert restored["claims_restored"] == 2 and restored["links_restored"] >= 1
    assert ("f1", "f2", "duplicate") in _links(conn)
    run_claim_exclusion_pass(conn, run_id="ex-2")
    excluded = {r[0] for r in conn.execute("SELECT claim_id FROM argument_claim_exclusions").fetchall()}
    assert {"f1", "f2"}.isdisjoint(excluded) and "f3" in excluded


def test_workflow_materialization_marks_repeated_wording(conn):
    from src.kb.workflows import _materialize_argument_claims

    _seed(conn, [])
    for index in range(3):
        conn.execute(
            "INSERT INTO documents (document_id, source_type, source_id) VALUES (?, 'blog', 'vendor')",
            [f"w{index}"],
        )
    outputs = [
        {"status": "produced", "output": {"output_type": "claim", "value": {
            "claim_id": f"wc{index}", "statement": FOOTER, "document_id": f"w{index}"}}}
        for index in range(3)
    ]
    result = _materialize_argument_claims(
        conn, [{"document_id": f"w{i}"} for i in range(3)], {"outputs": outputs}, run_id="wf-1",
    )
    assert result["claims_indexed"] == 3 and result["claims_excluded"] == 3


# -- 2. subject gate ----------------------------------------------------------------


def test_subjects_recognise_indicators_and_names():
    subjects = claim_subjects(
        "The Lumma stealer in package `left-pad-x` beacons to evil[.]example[.]com and 185.220.101.4, "
        "dropping update.exe (9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08) "
        "and paying 0x52908400098527886E0F7030069857D2E4169EE7; see CVE-2024-3094."
    )
    assert subjects.indicators == {
        "pkg:left-pad-x", "host:evil.example.com", "host:185.220.101.4", "file:update.exe",
        "hash:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "wallet:0x52908400098527886e0f7030069857d2e4169ee7", "cve:CVE-2024-3094",
    }
    assert subjects.names == {"lumma"}
    assert claim_subjects("Attackers published colors@1.4.1 to npm.").indicators == {"pkg:colors"}
    assert claim_subjects("A loader dubbed GhostLoader hit telecoms.").names == {"ghostloader"}


@pytest.mark.parametrize("a,b,relation,allowed,reason", [
    ("The install hook in the event-stream package downloads a payload.",
     "The install hook in the ua-parser-js package does not download a payload.",
     "contradicts", False, "different_subjects"),
    ("The install hook in the event-stream package downloads a payload.",
     "The install hook in the event-stream package does not download a payload.",
     "contradicts", True, None),
    ("The Lumma stealer exfiltrates cookies to 185.220.101.4.", "Lumma does not exfiltrate cookies.",
     "contradicts", True, None),
    ("The install hook in the event-stream package downloads a payload.",
     "The install hook does not download a payload.", "contradicts", False, "subject_unmatched"),
    ("The central bank will raise rates.", "The central bank will not raise rates.",
     "contradicts", True, None),
    ("Sample 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08 is a loader.",
     "Sample 2c26b46b68ffc68ff99b453c1d30413413422d706483bfa0f98a5e886266e7ae is a loader.",
     "supports", False, "different_subjects"),
    ("Researchers at Acme Labs found the event-stream package harmless.",
     "Researchers found the package harmless.", "supports", True, None),
])
def test_subject_gate(a, b, relation, allowed, reason):
    decision = subject_gate(a, b, relation)
    assert (decision.allowed, decision.reason) == (allowed, reason)


def test_two_campaigns_install_hooks_are_never_proposed_as_a_conflict(conn):
    _seed(conn, [
        ("h1", "The postinstall hook in package event-stream will run the payload.", "d1", "a", BASE),
        ("h2", "The postinstall hook in package flatmap-stream will not run the payload.", "d2", "b", BASE),
        ("h3", "The postinstall hook in package event-stream will not run the payload.", "d3", "c", BASE),
    ])
    summary = run_claim_linking_pass(conn, nli=WordNLI(), candidate_threshold=0.3)
    contradictions = [k for k in _links(conn) if k[2] == "contradicts"]
    assert contradictions == [("h1", "h3", "contradicts")]
    assert summary["subject_gate"]["different_subjects"] >= 1

    other = duckdb.connect()
    _seed(other, [
        ("h1", "The postinstall hook in package event-stream will run the payload.", "d1", "a", BASE),
        ("h2", "The postinstall hook in package flatmap-stream will not run the payload.", "d2", "b", BASE),
    ])
    run_claim_linking_pass(other, nli=WordNLI(), subject_gate=False, candidate_threshold=0.3)
    assert [k for k in _links(other) if k[2] == "contradicts"]  # the gate is what blocks it


def test_conflict_graph_applies_the_gate():
    from src.argument_mining.conflict_graph import detect_conflict

    a = "The postinstall hook in package event-stream will run the payload on install"
    b = "The postinstall hook in package flatmap-stream will not run the payload on install"
    assert detect_conflict("a", a, "news", "b", b, "blog", "npm") is None
    assert detect_conflict("a", a, "news", "b", b, "blog", "npm", subject_gate=False) is not None


# -- 3. stored Jev verdicts ------------------------------------------------------------

SUPPORT_A = "The event-stream package sharply raises install times."
SUPPORT_B = "The event-stream package raises install times."
CONTRA_A = "The event-stream package runs the payload."
CONTRA_B = "The event-stream package does not run the payload."


def _verdict_corpus(conn):
    _seed(conn, [
        ("s1", SUPPORT_A, "d1", "a", BASE),
        ("s2", SUPPORT_B, "d2", "b", BASE + DAY),
        ("c1", CONTRA_A, "d3", "c", BASE),
        ("c2", CONTRA_B, "d4", "d", BASE + DAY),
    ])


# Jev reads s2 -> s1 (reversed support) and the contradiction as reconcilable.
LABELS = {"a_to_b": "neutral", "b_to_a": "entailment",
          "kind": "temporal", "jointly_true": 0.9, "same_subject": 0.9}


def _reviewer(mode):
    texts = {"s1": SUPPORT_A, "s2": SUPPORT_B, "c1": CONTRA_A, "c2": CONTRA_B}
    return JevClaimLinkReviewer(
        FakeRuntime(texts, dict(LABELS)), "ns", principal_id="alice", scopes={"operator"},
        policy={"model": "jev-1.13.0"}, allow_remote=True, resolve_claim=resolver, mode=mode,
    )


def test_apply_stored_verdicts_withholds_and_reverses_then_undoes(conn):
    _verdict_corpus(conn)
    summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=_reviewer("shadow"))
    written = _links(conn)
    assert ("s1", "s2", "supports") in written and ("c1", "c2", "contradicts") in written
    assert summary["jev"]["disagrees"] == 2

    plan = apply_jev_verdicts(conn, summary["run_id"])
    assert plan["applied"] is False and plan["withheld"] == 1 and plan["reversed"] == 1
    assert _links(conn) == written  # dry run changes nothing

    applied = apply_jev_verdicts(conn, summary["run_id"], apply=True)
    links = _links(conn)
    assert ("c1", "c2", "contradicts") not in links
    assert ("s1", "s2", "supports") not in links and ("s2", "s1", "supports") in links
    method = conn.execute(
        "SELECT method FROM claim_links WHERE claim_a='s2' AND claim_b='s1'").fetchone()[0]
    assert method.endswith("+jev_reversed")
    reasons = dict(conn.execute("SELECT relation, reason FROM claim_links_withheld").fetchall())
    assert reasons == {"contradicts": "jev_rejected", "supports": "jev_reversed"}
    # The verdicts themselves are untouched and stay inspectable.
    assert conn.execute("SELECT COUNT(*) FROM claim_link_jev_assessments").fetchone()[0] == 2

    assert apply_jev_verdicts(conn, summary["run_id"])["withheld"] == 0  # nothing left to do
    states = {(r[0], r[1], r[2]): r[3] for r in conn.execute(
        "SELECT claim_a, claim_b, relation, state FROM claim_link_review_state").fetchall()}
    assert states == {("c1", "c2", "contradicts"): "withheld", ("s1", "s2", "supports"): "reversed"}
    assert applied["states"] == {"reversed": 1, "withheld": 1}
    assert "clusters" in applied  # clusters rebuilt from the changed duplicate graph

    undone = undo_jev_verdicts(conn, applied["op_id"])
    assert {k: undone[k] for k in ("restored", "replacements_removed", "kept_newer")} == {
        "restored": 2, "replacements_removed": 1, "kept_newer": 0}
    assert "clusters" in undone
    assert _links(conn) == written
    assert conn.execute("SELECT COUNT(*) FROM claim_link_review_state").fetchone()[0] == 0


def test_overrides_keep_jev_mistakes_and_apply_to_gate_runs(conn):
    _verdict_corpus(conn)
    summary = run_claim_linking_pass(conn, nli=WordNLI(), jev_reviewer=_reviewer("shadow"))
    keep = ("c1", "c2", "contradicts")
    applied = apply_jev_verdicts(conn, summary["run_id"], apply=True, keep=[keep], note="Jev misread")
    assert applied["kept_by_override"] == 1 and keep in _links(conn)

    # Overriding after the fact restores a withheld link.
    keep_links(conn, [("s1", "s2", "supports")], note="direction was right")
    assert ("s1", "s2", "supports") in _links(conn) and ("s2", "s1", "supports") not in _links(conn)

    # Gate mode reverses on its own, and honours stored overrides.
    fresh = duckdb.connect()
    _verdict_corpus(fresh)
    gated = run_claim_linking_pass(fresh, nli=WordNLI(), jev_reviewer=_reviewer("gate"))
    assert gated["jev"]["reversed"] == 1 and gated["jev"]["withheld"] == 1
    assert ("s2", "s1", "supports") in _links(fresh)
    assert ("c1", "c2", "contradicts") not in _links(fresh)
    actions = dict(fresh.execute("SELECT relation, action FROM claim_link_jev_assessments").fetchall())
    assert actions == {"supports": "reversed", "contradicts": "withheld"}

    overridden = duckdb.connect()
    _verdict_corpus(overridden)
    keep_links(overridden, [keep], note="Jev misread")
    run_claim_linking_pass(overridden, nli=WordNLI(), jev_reviewer=_reviewer("gate"))
    assert keep in _links(overridden)
    action = overridden.execute(
        "SELECT action FROM claim_link_jev_assessments WHERE relation='contradicts'").fetchone()[0]
    assert action == "override_written"
    detail = json.loads(overridden.execute(
        "SELECT detail_json FROM claim_link_jev_assessments WHERE relation='contradicts'").fetchone()[0])
    assert detail["kind"] == "temporal"  # Jev's verdict stays visible
