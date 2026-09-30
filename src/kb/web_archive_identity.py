"""URL canonicalisation and reviewable capture-to-citation matching for web archives (#2226, WA08).

Canonicalisation is explicit and versioned (``CANONICALISATION_VERSION``); every
rule has an id, and a match records which rules made the cited URL and the
capture's URI-R agree. Exact URI-R matches are distinguished from canonicalised
and redirect-derived matches. Only exact matches are accepted automatically;
every other match is a reviewable identity decision, and a rejected match is
never used for pinning. Archive-reported redirects are recorded as published and
never followed to substitute a different page.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

CANONICALISATION_VERSION = "wa-canon-v1"
TRACKING_PARAMETERS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "yclid", "_ga", "_gl",
})
TRACKING_PREFIXES = ("utm_",)
# Ordered, versioned rules. Changing one requires a new CANONICALISATION_VERSION.
RULES = (
    ("lowercase-scheme-host", "scheme and host compare case-insensitively"),
    ("scheme-equivalence", "http and https name the same page"),
    ("drop-default-port", ":80 for http and :443 for https are dropped"),
    ("strip-www", "a leading 'www.' host label is dropped"),
    ("strip-trailing-slash", "a trailing '/' on a non-root path is dropped; an empty path is '/'"),
    ("drop-tracking-parameters", "utm_* and click-id query parameters are dropped"),
    ("sort-query-parameters", "query parameters compare in sorted order"),
    ("drop-fragment", "the fragment is not sent to servers and is dropped"),
)
RULE_IDS = tuple(rule for rule, _ in RULES)


def canonicalize(url: str) -> tuple[str, list[str]]:
    """Return the canonical key of ``url`` and the ids of the rules that changed it."""
    raw = str(url or "").strip()
    parts = urlsplit(raw)
    applied: list[str] = []
    scheme = parts.scheme
    host = parts.hostname or ""
    if scheme != scheme.lower() or (parts.netloc.split("@")[-1].split(":")[0] != host):
        applied.append("lowercase-scheme-host")
    scheme = scheme.lower()
    if scheme in {"http", "https"}:
        if scheme == "http":
            applied.append("scheme-equivalence")
        scheme = "https"
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and (port, parts.scheme.lower()) in {(80, "http"), (443, "https")}:
        applied.append("drop-default-port")
        port = None
    if host.startswith("www."):
        applied.append("strip-www")
        host = host[4:]
    netloc = host + (f":{port}" if port is not None else "")
    path = quote(unquote(parts.path or ""), safe="/:@!$&'()*+,;=-._~%")
    if not path:
        path = "/"
    elif len(path) > 1 and path.endswith("/"):
        applied.append("strip-trailing-slash")
        path = path.rstrip("/") or "/"
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if k.lower() not in TRACKING_PARAMETERS
            and not k.lower().startswith(TRACKING_PREFIXES)]
    if len(kept) != len(pairs):
        applied.append("drop-tracking-parameters")
    ordered = sorted(kept)
    if ordered != kept:
        applied.append("sort-query-parameters")
    if parts.fragment:
        applied.append("drop-fragment")
    canonical = urlunsplit((scheme, netloc, path, urlencode(ordered), ""))
    return canonical, [rule for rule in RULE_IDS if rule in applied]


def canonical_key(url: str) -> str:
    return canonicalize(url)[0]


def match_rules(cited_url: str, uri_r: str) -> list[str]:
    """Rules that had to apply to either side for the two URLs to agree."""
    return sorted(set(canonicalize(cited_url)[1]) | set(canonicalize(uri_r)[1]),
                  key=RULE_IDS.index)


def classify(cited_url: str, capture: dict[str, Any]) -> dict[str, Any] | None:
    """How a capture relates to a cited URL: exact, canonicalised, redirect-derived or no match."""
    if capture["uri_r"] == cited_url:
        return {"match_kind": "exact", "rules": []}
    if canonical_key(capture["uri_r"]) == canonical_key(cited_url):
        return {"match_kind": "canonicalised", "rules": match_rules(cited_url, capture["uri_r"])}
    redirect = capture.get("archive_redirect")
    if redirect and redirect.get("location") and canonical_key(redirect["location"]) == canonical_key(cited_url):
        # The archive published a redirect from the capture's URI-R to the cited
        # URL. Recorded as published; the redirect is never followed.
        return {"match_kind": "redirect-derived",
                "rules": ["archive-reported-redirect", *match_rules(cited_url, redirect["location"])]}
    return None


def candidates(cited_url: str, captures: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    found = []
    for capture in captures:
        kind = classify(cited_url, capture)
        if kind:
            found.append({**kind, "capture": capture})
    return found
