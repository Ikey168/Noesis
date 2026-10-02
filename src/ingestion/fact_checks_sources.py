"""Published fact-checks for the News pack: Google Fact Check Tools, Data Commons ClaimReview and the IFCN signatory
list (#2659, FC01, FC03-FC05).

One native connector, ``fact-checks``, reads a bounded, declared selection from
one documented provider per source and emits ``noesis-fact-check-record-v2``
records exactly as the publisher published them:

* ``google-factcheck-claims-search-json`` - the Fact Check Tools API
  ``claims:search`` (v1alpha1) for declared queries or publisher sites: each
  ``claimReview`` of a returned claim becomes a ``fact-check`` record keyed by
  the publisher, the review URL and the reviewed claim, with the claim text as
  quoted, the claimant as named, the claim date, the review date and the
  ``textualRating`` verbatim. The API publishes no revision stamp: a changed
  review date or rating is a new revision of the same record;
* ``datacommons-claimreview-feed-jsonld`` - a Data Commons ClaimReview
  ``DataFeed`` release filtered to declared publisher sites and a review-date
  window. Each release is a **vintage**; the ``reviewRating`` (``alternateName``,
  ``ratingValue``, ``bestRating``, ``worstRating``) is kept verbatim with its
  scale, appearance URLs as published. A release is complete within its declared
  scope, so a review that a later release no longer carries becomes an
  ``absent-from-release`` revision, never a deletion;
* ``ifcn-signatories-html`` - the IFCN Code of Principles signatory listing: one
  ``publisher`` record per signatory website domain with the status as published
  (verified, expired, under review) and its dates; a status change is a dated
  revision and a signatory missing from a later listing is an
  ``absent-from-listing`` revision.

**Data minimisation (FC01).** Claimants are kept as the publisher named them
(name and published ``@type``); a claimant's ``sameAs`` survives only when it is
an identifier URL (Wikidata, ROR). Social-media profile URLs, images, job titles,
contact details and birth dates are dropped *here*, before any record, document or
receipt exists, and listed under ``minimisation.withheld``. Appearance URLs on
social platforms are stored as the host plus the SHA-256 of the canonical URL
(``wa-canon-v1``), so a caller holding the URL can still match it while the record
never exposes an account handle. A review's individual author (a Person) is
dropped; the publishing organisation is kept. The store refuses any record that
still carries one of these fields.

A unit is all-or-nothing: a result longer than the declared page bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest; the Google API key travels in the ``X-Goog-Api-Key`` header and never
appears in a URL, a receipt or a record. Nothing here issues a truth verdict,
normalises a rating or matches a claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-fact-check-record-v2"
MINIMISATION_POLICY = "fact-checks-minimisation-v1"
URL_RULES = "wa-canon-v1"
CONNECTOR = "fact-checks"
GOOGLE_PAGE_SIZE = 50
MAX_PAGES_PER_UNIT = 4
MAX_UNITS = 20
MAX_RELEASE_RECORDS = 2000
MAX_SIGNATORIES = 500
REVIEW_BOUNDARY = ("Records are what fact-check publishers published. Noesis issues no truth verdict, never "
                   "normalises a rating into its own scale or presents one as the publisher's, never matches a claim "
                   "without review and never scrapes beyond each publisher's terms.")
EXCLUSIONS = ("no truth verdicts by Noesis", "no rating normalisation presented as the publisher's",
              "no automatic claim matching without review", "no scraping beyond each publisher's terms")

FORMATS: dict[str, dict[str, Any]] = {
    "google-factcheck-claims-search-json": {"provider": "google-fact-check-tools", "unit": "searches",
                                            "keyed": True, "feature": "fact-checks-google"},
    "datacommons-claimreview-feed-jsonld": {"provider": "datacommons-claimreview", "unit": "releases",
                                            "keyed": False, "feature": "fact-checks-datacommons"},
    "ifcn-signatories-html": {"provider": "ifcn-signatories", "unit": "listings", "keyed": False,
                              "feature": "fact-checks-ifcn"},
}
PROVIDERS = tuple(spec["provider"] for spec in FORMATS.values())
RECORD_KINDS = ("fact-check", "publisher")
STATUSES = ("published", "absent-from-release", "absent-from-listing")
IFCN_STATUSES = ("verified", "expired", "under-review", "other")

# FC01 access decisions. Endpoints, fields and terms are recorded from the providers' published documentation as
# known without network access (the documentation hosts were unreachable from this runtime on 2026-09-30); every item
# marked ``verify`` must be checked before a dated live run (FC13, #2722).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "google-fact-check-tools": {
        "publisher": "Google Fact Check Tools API (claims:search, v1alpha1)",
        "endpoints": ["https://factchecktools.googleapis.com/v1alpha1/claims:search"],
        "formats": ["google-factcheck-claims-search-json"],
        "authentication": "Google Cloud API key (required-secret NOESIS_GOOGLE_FACTCHECK_API_KEY) sent as the "
        "X-Goog-Api-Key header, never as the key query parameter, in a URL or in a receipt; restrict the key to this "
        "API in the Cloud console",
        "rate_limits": "per-project quota set in the Google Cloud console (verify the default); HTTP 429 with "
        "Retry-After is rate_limited; one request per page, at most 4 pages of 50 claims per declared search",
        "pagination": "pageSize/pageToken; a search whose nextPageToken survives the page bound is budget_exhausted, "
        "never truncated",
        "identifiers": ["publisher site", "review URL", "claim text as quoted"],
        "revisions": "no revision stamp: the API returns the current ClaimReview markup; a changed reviewDate, title, "
        "textualRating or claim is a new revision of the record keyed by publisher, review URL and reviewed claim; a "
        "review missing from a later search is not a removal (search results are not a complete listing)",
        "licence": "Google APIs Terms of Service; the ClaimReview content is the publishers' own markup, returned for "
        "attribution with a link to the review; records keep the publisher, the review URL and short fields only and "
        "never mirror the review article (verify the current API terms)",
        "attribution": "Fact-check by <publisher>, via the Google Fact Check Tools API; link to the review URL.",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser in the documented v1alpha1 claims:search JSON shape; the quota, the "
        "header-key support and the current terms must be verified",
    },
    "datacommons-claimreview": {
        "publisher": "Data Commons ClaimReview data feed (DataFeed of schema.org ClaimReview)",
        "endpoints": ["https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json"],
        "formats": ["datacommons-claimreview-feed-jsonld"],
        "authentication": "none",
        "rate_limits": "a static file; one request per declared release; the whole file must fit the source's "
        "max_bytes budget or the unit fails as response_too_large (verify the current file size)",
        "pagination": "none: one file per release, filtered after download to the declared publisher sites and "
        "review-date window; more than 2000 matching reviews is budget_exhausted",
        "identifiers": ["ClaimReview url (review URL)", "author.url (publisher site)", "itemReviewed.appearance"],
        "revisions": "each release is a vintage (dateModified, else the declared release label and the response "
        "digest); a changed review is a revision, a review a later release no longer carries within the declared "
        "scope is an absent-from-release revision",
        "licence": "Data Commons terms of use; the fact-check feed is published for research reuse with attribution "
        "to the fact-checking publishers (verify whether the feed carries CC BY 4.0 or another licence before any "
        "redistribution; until then records are kept for local research only)",
        "attribution": "Fact-check by <publisher>; ClaimReview data via Data Commons.",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the documented DataFeed/ClaimReview JSON-LD shape; the release path, "
        "dateModified and the licence must be verified",
    },
    "ifcn-signatories": {
        "publisher": "International Fact-Checking Network (Poynter), Code of Principles signatory list",
        "endpoints": ["https://ifcncodeofprinciples.poynter.org/signatories"],
        "formats": ["ifcn-signatories-html"],
        "authentication": "none",
        "rate_limits": "undocumented; one request per declared listing, at most daily",
        "pagination": "one listing page; more than 500 signatories is budget_exhausted",
        "identifiers": ["signatory website (domain)", "profile path"],
        "revisions": "the listing publishes the current status only: each acquisition is dated, a changed status or "
        "status date is a dated revision, a signatory missing from a later listing is an absent-from-listing "
        "revision; the listing states no revision stamp",
        "licence": "Poynter website terms of use; no documented API or bulk export. Only the listing page is read "
        "(no profile pages, no assessments), at most once a day (verify that the terms allow this reuse; if they do "
        "not, the source stays declared-but-not-run and publisher status is reported as unavailable)",
        "attribution": "Signatory status as published by the IFCN (Poynter).",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for an authored listing shape; the listing markup, the status labels and "
        "the terms must be verified before a live run",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
               "fixtures only; terms were not re-verified live (documentation hosts unreachable)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (FC01): nothing implies complete coverage of a publisher, a language or a topic.
BOUNDED_COVERAGE = {
    "google-fact-check-tools": f"at most {MAX_UNITS} declared searches (a query and/or a publisher site, optional "
    f"language and maxAgeDays <= 3650), {MAX_PAGES_PER_UNIT} pages of {GOOGLE_PAGE_SIZE} claims each",
    "datacommons-claimreview": f"at most 3 declared releases, each filtered to 1-20 publisher sites and a review-date "
    f"window of at most 366 days; at most {MAX_RELEASE_RECORDS} reviews per release",
    "ifcn-signatories": f"the one signatory listing page, at most {MAX_SIGNATORIES} signatories, no profile pages",
    "links": "news articles, OSINT corroboration and claim timelines reached only by URL citation or accepted matches",
}
# Social platforms whose appearance URLs identify an account; stored as host + digest only.
SOCIAL_HOSTS = frozenset({
    "facebook.com", "fb.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com", "youtu.be",
    "t.me", "telegram.me", "threads.net", "reddit.com", "linkedin.com", "vk.com", "whatsapp.com", "bsky.app",
    "mastodon.social", "truthsocial.com", "rumble.com", "social.example",
})
IDENTIFIER_HOSTS = {"wikidata.org": "wikidata", "ror.org": "ror"}
# FC01 data-minimisation decision (docs/development/fact-checks-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "stored": ["publisher organisation name and site as published", "review URL, title, date and language",
               "claim text as quoted by the publisher",
               "claimant name and published @type as named by the publisher",
               "claimant identifier URLs (Wikidata, ROR) only", "claim date",
               "appearance URLs on news and web hosts", "rating text, value and scale verbatim",
               "IFCN signatory organisation, website, country, status and status dates as published"],
    "redacted": [("appearance and first-appearance URLs on social platforms: host plus SHA-256 of the wa-canon-v1 "
                  "canonical URL, never the URL or account handle")],
    "never_stored": ["claimant image", "claimant job title", "claimant contact details (address, email, telephone)",
                     "claimant birth date", "claimant social-media profile URLs (sameAs)",
                     "a review's individual author (Person)", "IFCN profile pages and named staff",
                     "review article bodies"],
    "matching": "claimants are matched to canonical entities only as reviewable assertions (published identifier "
    "first, then the name as published); claimant accounts and social-platform appearances are never matched",
    "query_scope": "knowledge:news:fact-checks:read plus namespace read access; the redacted social appearance URLs "
    "can only be matched by a caller who already holds the URL",
    "retention": "retained with each record revision; no personal identifier beyond the name as published is "
    "stored, and a publisher's removal is a revision that stops the record being current",
}
PERSONAL_KEYS = frozenset({"image", "job_title", "jobtitle", "email", "telephone", "address", "birth_date",
                           "birthdate", "review_author", "reviewer", "reviewer_name", "same_as"})


class FactCheckFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                          ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def canonical_url(url: Any) -> tuple[str, list[str]]:
    """The ``wa-canon-v1`` canonical key of a URL and the rules that changed it (shared with web archives)."""
    from src.kb.web_archive_identity import canonicalize

    return canonicalize(str(url or ""))


def url_digest(url: Any) -> str:
    return hashlib.sha256(canonical_url(url)[0].encode()).hexdigest()


def domain(value: Any) -> str | None:
    """The host of a site or URL, lowercased, without ``www.``."""
    text = str(value or "").strip()
    if not text:
        return None
    host = (urlsplit(text if "://" in text else f"https://{text}").hostname or "").casefold().removeprefix("www.")
    return host or None


def social_host(url: Any) -> str | None:
    host = domain(url)
    if not host:
        return None
    for social in SOCIAL_HOSTS:
        if host == social or host.endswith("." + social):
            return social
    return None


def claim_digest(text: Any) -> str:
    folded = re.sub(r"[^\w\s]", "", " ".join(str(text or "").casefold().split()))
    return hashlib.sha256(folded.encode()).hexdigest()


def publisher_key(site: Any) -> str:
    return f"fact-check:publisher:{domain(site) or 'unknown'}"


def review_key(site: Any, review_url: Any) -> str:
    return f"fact-check:review:{domain(site) or 'unknown'}:{url_digest(review_url)[:20]}"


def fact_check_key(site: Any, review_url: Any, claim_text: Any) -> str:
    """Keyed by publisher and review URL, plus the reviewed claim (one review page may review several claims)."""
    return f"{review_key(site, review_url)}:{claim_digest(claim_text)[:10]}"


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3])).isoformat()
    except ValueError:
        return None


def _https(value: Any) -> str | None:
    text = _clean(value)
    return text if text and text.startswith("https://") else None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactCheckFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


# ------------------------------------------------------------------ minimisation


def appearance(url: Any) -> dict[str, Any] | None:
    """An appearance as stored: the URL on news and web hosts; host and digest only on social platforms."""
    text = _clean(url)
    if not text or not text.startswith(("https://", "http://")):
        return None
    social = social_host(text)
    if social:
        return {"host": social, "url_sha256": url_digest(text), "platform_post": True, "url_withheld": True}
    canonical, rules = canonical_url(text)
    return {"url": text, "url_canonical": canonical, "canonical_rules": rules, "platform_post": False}


def claimant(value: Any, withheld: list[str], path: str) -> dict[str, Any] | None:
    """A claimant as the publisher named it; only identifier ``sameAs`` URLs survive (FC01)."""
    if value is None:
        return None
    if isinstance(value, str):
        name = _clean(value)
        return {"name_as_published": name, "type_as_published": None, "identifiers": []} if name else None
    if not isinstance(value, Mapping):
        return None
    for key in value:
        folded = str(key).casefold()
        if folded in {"image", "jobtitle", "job_title", "email", "telephone", "address", "birthdate"}:
            withheld.append(f"{path}.{key}")
    identifiers = []
    same = value.get("sameAs")
    for item in ([same] if isinstance(same, str) else same if isinstance(same, list) else []):
        host = domain(item)
        scheme = IDENTIFIER_HOSTS.get(host or "")
        if scheme == "wikidata" and re.search(r"/(Q\d+)$", str(item)):
            identifiers.append({"scheme": "wikidata", "value": re.search(r"/(Q\d+)$", str(item))[1]})
        elif scheme == "ror" and re.search(r"/(0[a-z0-9]{8})$", str(item)):
            identifiers.append({"scheme": "ror", "value": re.search(r"/(0[a-z0-9]{8})$", str(item))[1]})
        elif item:
            withheld.append(f"{path}.sameAs({host or 'unparsed'})")
    name = _clean(value.get("name"))
    if not name and not identifiers:
        return None
    return {"name_as_published": name, "type_as_published": _clean(value.get("@type")),
            "identifiers": sorted(identifiers, key=lambda i: (i["scheme"], i["value"]))}


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of personal fields a stored record must not carry (FC01)."""
    found: list[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            if value.get("platform_post") is True and ("url" in value or "url_canonical" in value):
                found.append(f"{path}.url")
            for key, item in value.items():
                if str(key).casefold() in PERSONAL_KEYS:
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(record.get("fields") or {}, "$.fields")
    for index, claim in enumerate((record.get("fields") or {}).get("claims") or []):
        for identifier in ((claim.get("claimant") or {}).get("identifiers") or []):
            if identifier.get("scheme") not in set(IDENTIFIER_HOSTS.values()):
                found.append(f"$.fields.claims[{index}].claimant.identifiers")
        for entry in (claim.get("appearances") or []) + [claim.get("first_appearance") or {}]:
            if entry.get("url") and social_host(entry["url"]):
                found.append(f"$.fields.claims[{index}].appearances")
    return sorted(set(found))


# ------------------------------------------------------------------ records


def _record(fmt: str, kind: str, record_key: str, *, title: Any, locator: str, fields: Mapping[str, Any],
            publisher: str | None, review: str | None = None, native_revision: Any = None, revision_order: str = "",
            effective_on: Any = None, scope_key: str | None = None, withheld: Sequence[str] = ()) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if kind not in RECORD_KINDS:
        raise FactCheckFormatError("schema_drift", f"unknown record kind {kind!r}")
    if not str(locator or "").startswith("https://"):
        raise FactCheckFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "record_kind": kind,
        "record_key": record_key,
        "publisher_key": publisher,
        "review_key": review,
        "scope_key": scope_key,
        "native_revision": _clean(native_revision),
        "revision_order": revision_order,
        "effective_on": _day(effective_on),
        "title": _clean(title) or record_key,
        "locator": locator,
        "minimisation": {"policy": MINIMISATION_POLICY, "withheld": sorted(set(withheld))},
        "fields": dict(fields),
    }


def _rating(text: Any, value: Any = None, best: Any = None, worst: Any = None) -> dict[str, Any]:
    """The rating exactly as published: text, and any numeric value with its scale, never converted."""
    def published(item: Any) -> str | None:
        return None if item is None or item == "" else str(item)

    value, best, worst = published(value), published(best), published(worst)
    scale = None
    if value is not None and (best is not None or worst is not None):
        scale = f"{value} on a scale from {worst if worst is not None else '?'} to {best if best is not None else '?'}"
    return {"text": _clean(text), "value": value, "best": best, "worst": worst, "scale_as_published": scale}


def _fact_check(fmt: str, *, site: str, publisher_name: Any, review_url: str, review_title: Any, review_date: Any,
                language: Any, claim: Mapping[str, Any], native_revision: Any, revision_order: str,
                scope_key: str | None, withheld: list[str]) -> dict[str, Any]:
    canonical, rules = canonical_url(review_url)
    fields = {
        "publisher": {"name_as_published": _clean(publisher_name), "site_as_published": _clean(site),
                      "domain": domain(site)},
        "review_url": review_url, "review_url_canonical": canonical, "canonical_rules": rules,
        "review_title": _clean(review_title), "review_date": _clean(review_date), "review_day": _day(review_date),
        "language": _clean(language), "status": "published",
        "claims": [dict(claim)],
    }
    return _record(fmt, "fact-check", fact_check_key(site, review_url, claim.get("claim_text")),
                   title=review_title or claim.get("claim_text"), locator=review_url, fields=fields,
                   publisher=publisher_key(site), review=review_key(site, review_url),
                   native_revision=native_revision, revision_order=revision_order, effective_on=review_date,
                   scope_key=scope_key, withheld=withheld)


def parse_google(pages: Sequence[bytes], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Fact Check Tools ``claims:search`` pages -> one fact-check record per (publisher, review URL, claim)."""
    fmt = "google-factcheck-claims-search-json"
    out: dict[str, dict[str, Any]] = {}
    for raw in pages:
        payload = _json(raw)
        if not isinstance(payload, Mapping):
            raise FactCheckFormatError("schema_drift", "claims:search response is not an object")
        claims = payload.get("claims", [])
        if not isinstance(claims, list):
            raise FactCheckFormatError("schema_drift", "claims:search response has no claims list")
        for item in claims:
            if not isinstance(item, Mapping) or not isinstance(item.get("claimReview", []), list):
                raise FactCheckFormatError("schema_drift", "a claim has no claimReview list")
            text = _clean(item.get("text"))
            if not text:
                continue
            for review in item.get("claimReview") or []:
                url = _https(review.get("url"))
                publisher = dict(review.get("publisher") or {})
                site = publisher.get("site") or domain(url)
                if not url or not site:
                    continue
                withheld: list[str] = []
                claim = {"claim_ref": claim_digest(text)[:10], "claim_text": text,
                         "claimant": claimant(item.get("claimant"), withheld, "claimant"),
                         "claim_date": _clean(item.get("claimDate")), "claim_day": _day(item.get("claimDate")),
                         "appearances": [], "first_appearance": None,
                         "rating": _rating(review.get("textualRating"))}
                record = _fact_check(fmt, site=site, publisher_name=publisher.get("name"), review_url=url,
                                     review_title=review.get("title"), review_date=review.get("reviewDate"),
                                     language=review.get("languageCode"), claim=claim,
                                     native_revision=review.get("reviewDate"),
                                     revision_order=_day(review.get("reviewDate")) or "", scope_key=None,
                                     withheld=withheld)
                out.setdefault(record["record_key"], record)
    del unit
    return [out[k] for k in sorted(out)]


def _first(value: Any) -> Any:
    return value[0] if isinstance(value, list) and value else value


def _work_url(value: Any) -> Any:
    """The URL of a schema.org CreativeWork given as an object or as a bare URL string."""
    value = _first(value)
    return value.get("url") if isinstance(value, Mapping) else value


def parse_datacommons(raw: bytes, unit: Mapping[str, Any], scope_key: str) -> tuple[list[dict[str, Any]], str]:
    """A ClaimReview DataFeed release filtered to the declared publisher sites and review-date window."""
    fmt = "datacommons-claimreview-feed-jsonld"
    payload = _json(raw)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("dataFeedElement"), list):
        raise FactCheckFormatError("schema_drift", "ClaimReview feed has no dataFeedElement list")
    vintage = _clean(payload.get("dateModified")) or f"{unit['release']}:sha256:{hashlib.sha256(raw).hexdigest()[:12]}"
    order = _day(payload.get("dateModified")) or ""
    sites = {domain(s) for s in unit["publishers"]}
    start, end = unit["from"], unit["to"]
    out: dict[str, dict[str, Any]] = {}
    for element in payload["dataFeedElement"]:
        if not isinstance(element, Mapping):
            raise FactCheckFormatError("schema_drift", "a feed element is not an object")
        items = element.get("item") or []
        for review in items if isinstance(items, list) else [items]:
            if not isinstance(review, Mapping) or review.get("@type") != "ClaimReview":
                continue
            url = _https(review.get("url"))
            author = _first(review.get("author")) or {}
            withheld: list[str] = []
            if isinstance(author, Mapping) and author.get("@type") == "Person":
                withheld.append("review.author(Person)")
                author = {}
            publisher = _first(review.get("publisher"))
            if not author and isinstance(publisher, Mapping) and publisher.get("@type") != "Person":
                author = publisher  # the publishing organisation when the review's author is a person
            site = domain((author or {}).get("url")) if isinstance(author, Mapping) else None
            site = site or domain(url)
            if not url or site not in sites:
                continue
            day = _day(review.get("datePublished"))
            if day is None or not start <= day <= end:
                continue
            item = review.get("itemReviewed") or {}
            text = _clean(review.get("claimReviewed"))
            if not text or not isinstance(item, Mapping):
                continue
            listed = item.get("appearance") or []
            listed = listed if isinstance(listed, list) else [listed]
            appearances = [a for a in (appearance(_work_url(x)) for x in listed) if a]
            first = appearance(_work_url(item.get("firstAppearance"))) if item.get("firstAppearance") else None
            rating = review.get("reviewRating") or {}
            rating = rating if isinstance(rating, Mapping) else {}
            claim = {"claim_ref": claim_digest(text)[:10], "claim_text": text,
                     "claimant": claimant(_first(item.get("author")), withheld, "itemReviewed.author"),
                     "claim_date": _clean(item.get("datePublished")), "claim_day": _day(item.get("datePublished")),
                     "appearances": sorted(appearances, key=lambda a: a.get("url_canonical") or a["url_sha256"]),
                     "first_appearance": first,
                     "rating": _rating(rating.get("alternateName"), rating.get("ratingValue"),
                                       rating.get("bestRating"), rating.get("worstRating"))}
            record = _fact_check(fmt, site=site, publisher_name=(author or {}).get("name"), review_url=url,
                                 review_title=review.get("name") or review.get("headline"),
                                 review_date=review.get("datePublished"), language=review.get("inLanguage"),
                                 claim=claim, native_revision=vintage, revision_order=order, scope_key=scope_key,
                                 withheld=withheld)
            out.setdefault(record["record_key"], record)
    if len(out) > MAX_RELEASE_RECORDS:
        raise FactCheckFormatError("input_limit", "release scope holds more reviews than the declared bound")
    return [out[k] for k in sorted(out)], vintage


class _SignatoryParser(HTMLParser):
    """Collects ``div.signatory`` blocks: name, country, website, status text, dated status fields, profile path."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[dict[str, Any]] = []
        self.current: dict[str, Any] | None = None
        self.depth = 0
        self.field: str | None = None
        self.date_kind: str | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(str(attrs.get("class") or "").split())
        if self.current is None:
            if tag == "div" and "signatory" in classes:
                self.current = {"data_status": attrs.get("data-status"), "dates": {}, "text": {}}
                self.depth = 1
            return
        if tag == "div":
            self.depth += 1
        for name in ("signatory-name", "signatory-country", "signatory-status", "signatory-date", "signatory-website",
                     "signatory-profile"):
            if name in classes:
                self.field = name
                if name == "signatory-date":
                    self.date_kind = str(attrs.get("data-kind") or "date")
                if name == "signatory-website":
                    self.current["website"] = attrs.get("href")
                if name == "signatory-profile":
                    self.current["profile"] = attrs.get("href")

    def handle_endtag(self, tag):
        if self.current is None:
            return
        if tag == "div":
            self.depth -= 1
            if self.depth == 0:
                self.items.append(self.current)
                self.current = None
        self.field = None

    def handle_data(self, data):
        if self.current is None or not self.field or not data.strip():
            return
        if self.field == "signatory-date":
            self.current["dates"][self.date_kind] = " ".join(data.split())
        else:
            key = self.field.removeprefix("signatory-")
            self.current["text"][key] = " ".join((self.current["text"].get(key, "") + " " + data).split())


def ifcn_status(text: Any, data_status: Any = None) -> str:
    folded = f"{data_status or ''} {text or ''}".casefold()
    if "expired" in folded:
        return "expired"
    if "review" in folded or "renewal" in folded:
        return "under-review"
    if "verified" in folded:
        return "verified"
    return "other"


def parse_ifcn(raw: bytes, unit: Mapping[str, Any], scope_key: str, endpoint: str) -> list[dict[str, Any]]:
    fmt = "ifcn-signatories-html"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FactCheckFormatError("schema_drift", "listing is not UTF-8 HTML") from exc
    parser = _SignatoryParser()
    parser.feed(text)
    if not parser.items:
        raise FactCheckFormatError("schema_drift", "listing carries no signatory blocks")
    if len(parser.items) > MAX_SIGNATORIES:
        raise FactCheckFormatError("input_limit", "listing holds more signatories than the declared bound")
    base = endpoint.rstrip("/")
    out: dict[str, dict[str, Any]] = {}
    for item in parser.items:
        site = domain(item.get("website"))
        name = item["text"].get("name")
        if not site or not name:
            continue
        status_text = item["text"].get("status")
        status = ifcn_status(status_text, item.get("data_status"))
        dates = {k: _day(v) or v for k, v in sorted(item["dates"].items())}
        status_date = dates.get("expired") if status == "expired" else dates.get("verified") \
            if status == "verified" else dates.get("review") or dates.get("submitted")
        profile = item.get("profile")
        profile_url = (profile if str(profile or "").startswith("https://")
                       else base + str(profile) if str(profile or "").startswith("/") else None)
        fields = {"name_as_published": name, "website": _clean(item.get("website")), "domain": site,
                  "country_as_published": item["text"].get("country"), "status": "published", "ifcn_status": status,
                  "status_as_published": status_text or item.get("data_status"), "status_dates": dates,
                  "status_date": _day(status_date), "profile_url": profile_url, "listing": "ifcn-signatories"}
        key = publisher_key(site)
        out[key] = _record(fmt, "publisher", key, title=name, locator=profile_url or base + "/signatories",
                           fields=fields, publisher=key, native_revision=f"{status}:{status_date or ''}",
                           revision_order=_day(status_date) or "", effective_on=status_date, scope_key=scope_key)
    del unit
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ units and requests

_SITE = re.compile(r"^[a-z0-9][a-z0-9.-]{0,252}\.[a-z0-9-]{2,63}$")
_RELEASE = re.compile(r"^(latest|\d{4}-\d{2}-\d{2}|[a-z0-9][a-z0-9-]{0,40})$")


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) for u in selection.get(key) or [] if isinstance(u, Mapping)]
    if not 1 <= len(units) <= MAX_UNITS or len(units) != len(selection.get(key) or []):
        raise SourcePackError("invalid_manifest", f"a fact-checks selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if key == "searches":
            query, site = _clean(unit.get("query")), _clean(unit.get("publisher_site"))
            if not query and not site:
                raise SourcePackError("invalid_manifest", "a search names a query or a publisher site")
            if site and not _SITE.fullmatch(site):
                raise SourcePackError("invalid_manifest", f"not a publisher site: {site!r}")
            if unit.get("max_age_days") is not None and not 1 <= int(unit["max_age_days"]) <= 3650:
                raise SourcePackError("invalid_manifest", "max_age_days is 1-3650")
        elif key == "releases":
            if len(units) > 3:
                raise SourcePackError("invalid_manifest", "at most three Data Commons releases per source")
            if not _RELEASE.fullmatch(str(unit.get("release") or "")):
                raise SourcePackError("invalid_manifest", "a release is 'latest', a date or a release label")
            sites = unit.get("publishers") or []
            if not 1 <= len(sites) <= 20 or not all(_SITE.fullmatch(str(s)) for s in sites):
                raise SourcePackError("invalid_manifest", "a release is filtered to 1-20 publisher sites")
            start, end = _day(unit.get("from")), _day(unit.get("to"))
            if not start or not end or end < start or (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
                raise SourcePackError("invalid_manifest", "a release names a review-date window of at most a year")
        elif key == "listings":
            if len(units) != 1 or unit.get("page") != "signatories":
                raise SourcePackError("invalid_manifest", "the IFCN source reads the one signatory listing")
    return units


def scope_key(source_id: str, fmt: str, unit: Mapping[str, Any]) -> str:
    return "fact-check-scope:" + _digest([source_id, fmt, {k: v for k, v in unit.items() if k != "release"}])[:20]


def requests_for(fmt: str, unit: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The request path (relative to the endpoint) and parameters for one unit (first page)."""
    if fmt == "google-factcheck-claims-search-json":
        params: dict[str, Any] = {"pageSize": GOOGLE_PAGE_SIZE}
        if unit.get("query"):
            params["query"] = str(unit["query"])
        if unit.get("publisher_site"):
            params["reviewPublisherSiteFilter"] = str(unit["publisher_site"])
        if unit.get("language"):
            params["languageCode"] = str(unit["language"])
        if unit.get("max_age_days"):
            params["maxAgeDays"] = int(unit["max_age_days"])
        return "/v1alpha1/claims:search", params
    if fmt == "datacommons-claimreview-feed-jsonld":
        return f"/datacommons-feeds/claimreview/{unit['release']}/data.json", {}
    return "/signatories", {}


def fact_checks_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("fact_checks") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "fact-checks sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "fact-checks sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "fact-checks sources declare the FC01 minimisation policy")
    _units(fmt, dict(declared.get("selection") or {}))
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "the keyed fact-checks format declares a required secret")
    return declared


class FactChecksAdapter:
    """Fetch one declared selection unit per page from the source's endpoint host and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = fact_checks_declaration(self.source)
        self.format = self.declared["format"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "fact_checks": {"provider": self.declared["provider"], "format": self.format, "units": len(self.units),
                            "keyed": bool(FORMATS[self.format]["keyed"]), "minimisation": MINIMISATION_POLICY},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "fact-checks runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/json, application/ld+json, text/html"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "this fact-checks source needs its API key")
            headers["X-Goog-Api-Key"] = self.secret
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "fact-checks response was served from another host")
        status = int(response.get("status", 200))
        headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        query = urlencode(sorted(params.items()))
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def _collect(self, unit: Mapping[str, Any]) -> tuple[list[bytes], list[dict[str, Any]]]:
        path, params = requests_for(self.format, unit)
        if self.format != "google-factcheck-claims-search-json":
            raw, receipt = self._get(path, params)
            return [raw], [receipt]
        pages, receipts, request = [], [], dict(params)
        while True:
            raw, receipt = self._get(path, request)
            pages.append(raw)
            receipts.append(receipt)
            try:
                payload = _json(raw)
            except FactCheckFormatError as exc:
                raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
            token = payload.get("nextPageToken") if isinstance(payload, Mapping) else None
            if not token:
                return pages, receipts
            if len(pages) >= MAX_PAGES_PER_UNIT:
                raise SourcePackError("budget_exhausted", "search is longer than its page bound; never truncated")
            request = {**params, "pageToken": str(token)}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests = self._collect(unit)
        scope = scope_key(self.source["source_id"], self.format, unit)
        vintage = None
        try:
            if self.format == "google-factcheck-claims-search-json":
                records = parse_google(responses, unit)
            elif self.format == "datacommons-claimreview-feed-jsonld":
                records, vintage = parse_datacommons(responses[0], unit, scope)
            else:
                records = parse_ifcn(responses[0], unit, scope, self.source["endpoint"])
        except FactCheckFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        complete = self.format != "google-factcheck-claims-search-json"
        receipt = {
            "contract": "noesis-fact-check-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.declared["provider"], "format": self.format, "unit_index": index, "unit": unit,
            "requests": requests, "records": len(records), "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "minimisation": MINIMISATION_POLICY,
            "withheld_fields": sum(len(r["minimisation"]["withheld"]) for r in records),
            "scope": {"scope_key": scope, "complete": complete, "vintage": vintage,
                      "vintage_order": (max((r["revision_order"] for r in records), default="")
                                        if vintage else ""),
                      "record_kind": "publisher" if self.format == "ifcn-signatories-html" else "fact-check",
                      "absence": ("absent-from-listing" if self.format == "ifcn-signatories-html"
                                  else "absent-from-release") if complete else None},
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"],
                "language": (record["fields"].get("language") or "und"),
                "published_at": record["effective_on"], "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "fact_check_record": record, "fact_check_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, sum(r["bytes"] for r in requests), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: FactChecksAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        query = urlencode(sorted(dict(params or {}).items()))
        key = parts.path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = FactChecksAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                secret=FIXTURE_SECRET)
    records, cursor = [], None
    for _ in range(len(adapter.units)):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "PROVIDER_CONTRACTS",
    "RECORD_CONTRACT",
    "REVIEW_BOUNDARY",
    "FactCheckFormatError",
    "FactChecksAdapter",
    "canonical_url",
    "fact_checks_declaration",
    "fixture_transport",
    "minimisation_violations",
    "replay_native_fixture",
    "requests_for",
]
