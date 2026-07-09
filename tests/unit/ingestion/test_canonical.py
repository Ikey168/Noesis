"""Unit tests for ingest-time URL canonicalization + content hashing (#895)."""

from __future__ import annotations

import pytest

from src.ingestion.canonical import canonicalize_url, content_hash


# --------------------------------------------------------------------------- #
# canonicalize_url
# --------------------------------------------------------------------------- #


def test_strips_tracking_params_and_fragment():
    a = canonicalize_url("https://ex.com/story?utm_source=nl&utm_medium=email#top")
    b = canonicalize_url("https://ex.com/story")
    assert a == b == "https://ex.com/story"


def test_strips_assorted_click_ids():
    for noisy in [
        "https://ex.com/p?fbclid=abc",
        "https://ex.com/p?gclid=xyz",
        "https://ex.com/p?mc_cid=1&mc_eid=2",
        "https://ex.com/p?ref=twitter",
    ]:
        assert canonicalize_url(noisy) == "https://ex.com/p"


def test_keeps_meaningful_query_and_sorts_keys():
    a = canonicalize_url("https://ex.com/s?b=2&a=1&utm_source=x")
    assert a == "https://ex.com/s?a=1&b=2"


def test_lowercases_host_and_scheme_keeps_path_case():
    u = canonicalize_url("HTTPS://Example.COM/Story/Path")
    assert u == "https://example.com/Story/Path"


def test_drops_default_port_keeps_custom():
    assert canonicalize_url("https://ex.com:443/p") == "https://ex.com/p"
    assert canonicalize_url("http://ex.com:80/p") == "http://ex.com/p"
    assert canonicalize_url("https://ex.com:8443/p") == "https://ex.com:8443/p"


def test_trailing_slash_normalized_but_root_kept():
    assert canonicalize_url("https://ex.com/a/b/") == "https://ex.com/a/b"
    assert canonicalize_url("https://ex.com/") == "https://ex.com/"


def test_distinct_urls_stay_distinct():
    assert canonicalize_url("https://ex.com/a") != canonicalize_url("https://ex.com/b")
    assert canonicalize_url("https://a.com/x") != canonicalize_url("https://b.com/x")


def test_non_url_returned_unchanged():
    assert canonicalize_url("") == ""
    assert canonicalize_url("not a url") == "not a url"
    assert canonicalize_url("file:///local") == "file:///local"  # no netloc


# --------------------------------------------------------------------------- #
# content_hash
# --------------------------------------------------------------------------- #


def test_same_body_hashes_equal_regardless_of_whitespace_case():
    assert content_hash("The Budget  Passed.\n") == content_hash("the budget passed.")


def test_different_body_hashes_differ():
    assert content_hash("A bill passed") != content_hash("A bill failed")


def test_empty_and_none_hash_stably():
    assert content_hash("") == content_hash("   \n ")
    assert content_hash(None) == content_hash("")  # type: ignore[arg-type]


def test_hash_is_hex_sha256():
    h = content_hash("x")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_syndicated_article_dedup_scenario():
    """Same story under two outlets' URLs -> same content hash, different canon URL."""
    body = "Parliament approved the budget after a marathon debate."
    u1 = canonicalize_url("https://bbc.com/news/budget?utm_source=rss")
    u2 = canonicalize_url("https://guardian.com/uk/budget#comments")
    assert u1 != u2
    assert content_hash(body) == content_hash(body + "  ")
