"""Unit tests for the HTTP→Playwright escalation policy (#883) — offline."""

from __future__ import annotations

from src.scraper.adaptive.escalation import ESCALATE, OK, FetchEscalationPolicy


def test_productive_http_pass_is_ok():
    policy = FetchEscalationPolicy()
    assert policy.assess("bbc", articles_found=12) == OK
    assert not policy.should_use_js("bbc", configured_requires_js=False)


def test_empty_http_pass_escalates():
    policy = FetchEscalationPolicy()
    assert policy.assess("bbc", articles_found=0) == ESCALATE


def test_configured_requires_js_always_wins():
    policy = FetchEscalationPolicy()
    assert policy.should_use_js("verge", configured_requires_js=True)


def test_sticky_promotion_after_consecutive_js_successes():
    policy = FetchEscalationPolicy(promote_after=2)
    # Run 1: HTTP empty -> escalate -> JS succeeds.
    assert policy.assess("bbc", 0) == ESCALATE
    policy.record_js_result("bbc", 10)
    assert not policy.should_use_js("bbc", False)  # one success isn't enough
    # Run 2: same again -> promoted.
    assert policy.assess("bbc", 0) == ESCALATE
    policy.record_js_result("bbc", 9)
    assert policy.should_use_js("bbc", False)
    assert policy.promoted_sources() == ["bbc"]


def test_single_js_success_does_not_promote_no_flapping():
    policy = FetchEscalationPolicy(promote_after=2)
    policy.record_js_result("bbc", 10)
    policy.record_js_result("bbc", 0)   # streak broken
    policy.record_js_result("bbc", 10)
    assert not policy.should_use_js("bbc", False)


def test_demotion_after_consecutive_js_empty():
    policy = FetchEscalationPolicy(promote_after=1, demote_after=2)
    policy.assess("bbc", 0)
    policy.record_js_result("bbc", 10)
    assert policy.should_use_js("bbc", False)
    policy.record_js_result("bbc", 0)
    assert policy.should_use_js("bbc", False)  # one empty isn't enough
    policy.record_js_result("bbc", 0)
    assert not policy.should_use_js("bbc", False)  # demoted
    assert policy.promoted_sources() == []


def test_sources_isolated():
    policy = FetchEscalationPolicy(promote_after=1)
    policy.assess("a", 0)
    policy.record_js_result("a", 5)
    assert policy.should_use_js("a", False)
    assert not policy.should_use_js("b", False)


def test_report_shape():
    policy = FetchEscalationPolicy()
    policy.assess("bbc", 0)
    report = policy.report()
    assert report["bbc"]["escalations"] == 1
    assert report["bbc"]["promoted"] is False
