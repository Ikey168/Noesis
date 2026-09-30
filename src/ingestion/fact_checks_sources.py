"""Published fact-checks for the News bundle: ClaimReview records and IFCN signatory status (#2659, FC01, FC03-FC05).

One native connector, ``fact-checks``, reads a bounded, declared selection from
one documented provider per source and emits ``noesis-fact-check-record-v1``
records exactly as the publishers published them:

* ``google-factcheck-claimsearch-json`` - the Google Fact Check Tools API
  ``claims:search`` method for declared queries or review-publisher sites. Each
  (publisher site, review URL) becomes one fact-check record with every claim
  the page reviewed: the claim text as quoted, the claimant as named, the claim
  date and the textual rating exactly as the publisher wrote them;
* ``datacommons-claimreview-feed-json`` - the Data Commons ClaimReview
  ``DataFeed`` (schema.org ``ClaimReview`` markup). A release is one vintage;
  items are filtered to the declared publisher sites and review-date window;
  ``reviewRating`` values (``ratingValue``, ``bestRating``, ``worstRating``,
  ``alternateName``) are stored verbatim with the publisher's own scale;
* ``ifcn-signatories-html`` - the IFCN Code of Principles signatories listing:
  one publisher record per signatory profile with the status label and any
  status date exactly as published (verified, expired, under renewal or under
  review). The listing publishes no API or download; a live fetch is refused
  until an operator records a terms confirmation for the source (FC01).

**No verdicts and no normalisation.** A rating is the publisher's text (and any
numeric value with the publisher's own scale); nothing here maps ratings onto a
common scale, derives a verdict or matches a claim to anything. The legacy
lookup in :mod:`src.argument_mining.factcheck` (which normalises verdicts and
attaches the first review to a claim) is not used by this provider.

**Data minimisation (FC01).** Records keep the publisher, review URL, title and
date, language, claim text as quoted, claimant as named (with the claimant type
and any ``sameAs`` identifiers the publisher published), claim date and
appearance URLs. Review authors who are natural persons, images, job titles and
any other personal attribute of claimants or authors are dropped *here*, before
any record exists, and listed under ``minimisation.withheld``; the store refuses
any record that still carries them.

A unit is all-or-nothing: a result set longer than the declared page bound is
``budget_exhausted``, never truncated; a redirect to another host is a
network-policy failure. Receipts name every request path, status and response
digest; the Google API key travels in the ``X-Goog-Api-Key`` header and never
appears in a URL, a receipt or a record.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-fact-check-record-v1"
MINIMISATION_POLICY = "fact-checks-minimisation-v1"
CONNECTOR = "fact-checks"
GOOGLE_PAGE_SIZE = 50
MAX_PAGES_PER_UNIT = 5
MAX_UNITS = 20
FEED_RECORD_CAP = 2000
LISTING_RECORD_CAP = 500
MAX_WINDOW_DAYS = 366
GOOGLE_ATTRIBUTION = ("ClaimReview data via the Google Fact Check Tools API; each fact-check belongs to its "
                      "publisher.")
DC_ATTRIBUTION = ("Data Commons ClaimReview data (compilation licensed CC BY); the structured data of each markup "
                  "is licensed as its sdLicense states and each fact-check belongs to its publisher.")
IFCN_ATTRIBUTION = "IFCN Code of Principles signatories as published by the International Fact-Checking Network."
REVIEW_BOUNDARY = ("Records are what the publishers published. No truth verdict by Noesis, no rating normalisation "
                   "presented as the publisher's, no automatic claim matching without review and no scraping "
                   "beyond each publisher's terms.")
EXCLUSIONS = ("truth verdicts by Noesis", "rating normalisation presented as the publisher's",
              "automatic claim matching without review", "scraping beyond each publisher's terms")

# format -> provider, the selection list it reads, whether an API key is required, and whether one unit's response
# is a complete listing (so a record missing from a later acquisition is a source removal, stored as a revision).
FORMATS: dict[str, dict[str, Any]] = {
    "google-factcheck-claimsearch-json": {"provider": "google-fact-check-tools", "unit": "queries", "keyed": True,
                                          "complete": False, "kind": "fact-check"},
    "datacommons-claimreview-feed-json": {"provider": "datacommons", "unit": "releases", "keyed": False,
                                          "complete": True, "kind": "fact-check"},
    "ifcn-signatories-html": {"provider": "ifcn", "unit": "listings", "keyed": False, "complete": True,
                              "kind": "publisher"},
}
PROVIDERS = ("google-fact-check-tools", "datacommons", "ifcn")
RECORD_KINDS = ("fact-check", "publisher")
STATUSES = ("published", "absent-from-release", "absent-from-listing")
FEATURES = {"google-fact-check-tools": "fact-checks-google", "datacommons": "fact-checks-datacommons",
            "ifcn": "fact-checks-ifcn"}

# FC01 access decisions (docs/development/fact-checks-evidence/source-audit.md). The official pages could not be
# fetched from this runtime (egress blocked, 2026-09-30); every item marked "verify" is checked before a dated live
# run (FC13, #2722).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "google-fact-check-tools": {
        "publisher": "Google (Fact Check Tools API v1alpha1, Fact Checked Claim Search)",
        "endpoints": ["https://factchecktools.googleapis.com/v1alpha1/claims:search"],
        "formats": ["google-factcheck-claimsearch-json"],
        "authentication": "Google Cloud API key (required-secret NOESIS_GOOGLE_FACTCHECK_API_KEY) sent as the "
        "X-Goog-Api-Key header, never as the key query parameter, in a URL or in a receipt (verify that the header "
        "is accepted; the legacy GOOGLE_FACTCHECK_API_KEY lookup is not reused)",
        "rate_limits": "not stated in the material read; per-project quota in the Google Cloud console (verify); at "
        "most 5 pages of 50 claims per declared unit",
        "pagination": "pageSize/pageToken; a unit longer than 5 pages is budget_exhausted, never truncated",
        "parameters": ["query", "languageCode", "reviewPublisherSiteFilter", "maxAgeDays", "pageSize", "pageToken"],
        "identifiers": ["claimReview.url (review URL)", "claimReview.publisher.site"],
        "revisions": "no revision stamp; reviewDate as published orders revisions; a changed review (date, title, "
        "rating text, claims) is a new revision of the (publisher site, review URL) record; a search result that "
        "stops appearing is not a removal (ranking and maxAgeDays), so absence is never recorded",
        "licence": "Google APIs Terms of Service (verify the Fact Check Tools API terms for storage and display); "
        "each fact-check and rating belongs to its publisher and is shown as a cited quotation with a link",
        "attribution": GOOGLE_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser in the documented claims:search JSON shape (claims[].text, claimant, "
        "claimDate, claimReview[].publisher.name/site, url, title, reviewDate, textualRating, languageCode); header "
        "authentication and quotas must be verified",
    },
    "datacommons": {
        "publisher": "Data Commons (ClaimReview data feed and research dataset)",
        "endpoints": ["https://storage.googleapis.com/datacommons-feeds/claimreview/latest/data.json"],
        "formats": ["datacommons-claimreview-feed-json"],
        "authentication": "none",
        "rate_limits": "none documented; one request per declared release",
        "pagination": "whole release file within the source byte budget; a larger file is budget_exhausted, never "
        f"truncated; at most {FEED_RECORD_CAP} fact-check records per release after filtering",
        "identifiers": ["ClaimReview url (review URL)", "author/publisher url (site)", "DataFeedItem dateModified"],
        "revisions": "each release is a vintage (feed dateModified and response digest in the receipt); a markup "
        "changed in a later release is a new revision; a record absent from a later release of the same selection "
        "is an absent-from-release revision, never a deletion",
        "licence": "compilation and feed licensed CC BY (download page, as excerpted 2026-09-30; verify); the "
        "structured data of each markup is licensed as its sdLicense states; publishers' own terms apply to their "
        "articles, which are never fetched",
        "attribution": DC_ATTRIBUTION,
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser for the schema.org DataFeed/ClaimReview shape; the feed path and size and "
        "the release cadence must be verified",
    },
    "datacommons-research-dataset": {
        "publisher": "Data Commons (historical ClaimReview research dataset)",
        "endpoints": ["https://datacommons.org/factcheck/download"],
        "formats": [],
        "authentication": "none",
        "rate_limits": "none documented",
        "pagination": "whole dataset files",
        "identifiers": ["ClaimReview url"],
        "revisions": "dated historical compilation; not updated",
        "licence": "CC BY (download page excerpt; verify)",
        "attribution": DC_ATTRIBUTION,
        "access_decision": "documented-not-acquired",
        "reason": "a historical, unmaintained compilation superseded by the feed for the bounded window; reserved for "
        "the FC13 cross-check",
    },
    "ifcn": {
        "publisher": "International Fact-Checking Network at Poynter (Code of Principles signatories)",
        "endpoints": ["https://ifcncodeofprinciples.poynter.org/signatories"],
        "formats": ["ifcn-signatories-html"],
        "authentication": "none",
        "rate_limits": "none documented; one request per declared listing, weekly at most",
        "pagination": f"one listing page per unit; at most {LISTING_RECORD_CAP} signatories",
        "identifiers": ["IFCN profile path per signatory", "signatory website (domain)"],
        "revisions": "the listing publishes the current status label (and where shown a status date); a changed "
        "status is a dated revision of the signatory's publisher record; a signatory absent from a later listing "
        "is an absent-from-listing revision",
        "licence": "no API, download or reuse terms found in the material read (verify); a live fetch is refused "
        "until an operator records a terms confirmation on the source (terms_confirmation); the CC0 GitHub list "
        "IFCN/verified-signatories is historical and not used",
        "attribution": IFCN_ATTRIBUTION,
        "access_decision": "unverified-live",
        "requires_terms_confirmation": True,
        "reason": "fixture-verified tolerant parser (profile links, status labels, website, country); the listing "
        "markup and the site terms must be verified before a live run",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
                                                              "fixtures only"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded first coverage (FC01): nothing implies complete coverage of a publisher, a language or a period.
BOUNDED_COVERAGE = {
    "google": f"declared queries or review-publisher sites, at most {MAX_UNITS} units per source, maxAgeDays at most "
    f"{MAX_WINDOW_DAYS}, at most {MAX_PAGES_PER_UNIT} pages of {GOOGLE_PAGE_SIZE} claims per unit",
    "datacommons": f"one release per unit filtered to at most 50 declared publisher sites and a review-date window of "
    f"at most {MAX_WINDOW_DAYS} days; at most {FEED_RECORD_CAP} fact-check records per release",
    "ifcn": f"the signatories listing, at most {LISTING_RECORD_CAP} signatories; status history is what successive "
    "acquisitions observed plus any status date the listing publishes",
    "claims": "argument claims, news articles and claimants are reached only through citations and reviewed matches",
}
# FC01 data-minimisation decision (docs/development/fact-checks-evidence/source-audit.md).
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "stored": ["publisher name and site as published", "review URL, title, date and language", "claim text as quoted",
               ("claimant as named by the publisher, the claimant type and any sameAs identifiers the publisher "
                "published"), "claim date and appearance URLs",
               "rating text and any numeric rating with the publisher's scale",
               "IFCN signatory name, website, country and status as published"],
    "never_stored": ["names of review authors who are natural persons", "images of claimants, authors or ratings",
                     "claimant job titles, birth dates, addresses, e-mail or telephone", "appearance authors",
                     "article bodies of fact-checks or appearances"],
    "claimants": "a claimant is the publisher's published attribution, stored as named and never enriched; claimant "
    "matches to canonical entities are reviewable proposals only and never inferred for unnamed or generic "
    "claimants (e.g. 'social media users')",
    "query_scope": "fact-checks are read with knowledge:news:fact-checks:read (claimants shown as named, as cited "
    "quotations); searching, matching or subscribing by claimant needs "
    "knowledge:news:fact-checks:claimant:read in addition",
    "retention": "retained with the revision chain; a publisher's removal or correction is a revision; an erasure "
    "request for a claimant is an operator action outside the first coverage (none automated)",
}
# Keys that must never appear anywhere in a record's fields (personal attributes or a Noesis verdict).
FORBIDDEN_PERSONAL_KEYS = frozenset({
    "author_name", "reviewer", "reviewer_name", "image", "image_url", "job_title", "jobtitle", "birth_date",
    "birthdate", "address", "email", "telephone", "author_image", "claimant_image", "appearance_author",
})
FORBIDDEN_VERDICT_KEYS = frozenset({
    "verdict", "normalized_rating", "normalised_rating", "truth", "truth_value", "veracity", "noesis_rating",
    "rating_normalised", "rating_normalized", "score", "credibility",
})
RATING_KEYS = ("textual_rating", "rating_value", "best_rating", "worst_rating", "rating_name")
CLAIM_KEYS = ("claim_text_as_quoted", "claimant_as_named", "claimant_type_as_published", "claimant_same_as",
              "claim_date", "appearance_urls", "first_appearance_url", "rating")
IFCN_STATUS_LABELS = ("verified active", "verified", "under renewal", "under review", "expired", "renewal in progress")


class FactCheckFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "none"


def day(value: Any) -> str | None:
    """An ISO day from an ISO date/time, or from ``d Month yyyy`` / ``Month d, yyyy``."""
    text = str(value or "").strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    for pattern in ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()  # noqa: DTZ007 - a calendar day, no time
        except ValueError:
            continue
    return None


def site_of(value: Any) -> str | None:
    """The host of a URL or a bare site name, lower-cased, without ``www.``."""
    text = str(value or "").strip()
    if not text:
        return None
    host = urlsplit(text if "://" in text else f"https://{text}").hostname or ""
    host = host.casefold().removeprefix("www.")
    return host or None


def canonical_url(url: Any) -> tuple[str, list[str]]:
    """The versioned URL canonicalisation shared with the web-archives provider (``wa-canon-v1``)."""
    from src.kb.web_archive_identity import canonicalize

    return canonicalize(str(url or ""))


def url_rule() -> dict[str, Any]:
    from src.kb.web_archive_identity import CANONICALISATION_VERSION, RULES

    return {"version": CANONICALISATION_VERSION, "rules": [{"id": r, "description": d} for r, d in RULES]}


def review_key(site: str, review_url: str) -> str:
    return f"fact-checks:review:{site}:{hashlib.sha256(canonical_url(review_url)[0].encode()).hexdigest()[:20]}"


def ifcn_key(profile: str) -> str:
    return f"fact-checks:ifcn:{slug(profile)}"


def _https(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return None
    if text.startswith("http://"):
        text = "https://" + text[len("http://"):]
    return text if text.startswith("https://") else None


def _record(fmt: str, record_key: str, *, unit_key: str, title: Any, locator: str, fields: Mapping[str, Any],
            publisher_site: str | None, native_revision: Any = None, revision_order: str = "",
            effective_on: Any = None, language: Any = None, withheld: Sequence[str] = (),
            review_url_canonical: str | None = None) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if not str(locator or "").startswith("https://"):
        raise FactCheckFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "record_kind": spec["kind"],
        "record_key": record_key,
        "unit_key": unit_key,
        "publisher_site": publisher_site,
        "review_url_canonical": review_url_canonical,
        "status": "published",
        "native_revision": _clean(native_revision),
        "revision_order": revision_order,
        "effective_on": day(effective_on) if effective_on else None,
        "language": _clean(language),
        "title": _clean(title) or record_key,
        "locator": locator,
        "minimisation": {"policy": MINIMISATION_POLICY, "withheld": sorted(set(withheld))},
        "fields": dict(fields),
    }


def _rating(textual: Any, value: Any = None, best: Any = None, worst: Any = None, name: Any = None
            ) -> dict[str, Any]:
    """A rating exactly as published: strings stay strings, numbers stay numbers; nothing is mapped or scaled."""
    def verbatim(item: Any) -> Any:
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return item
        return _clean(item)

    return {"textual_rating": _clean(textual), "rating_value": verbatim(value), "best_rating": verbatim(best),
            "worst_rating": verbatim(worst), "rating_name": _clean(name)}


def _unit_key(fmt: str, unit: Mapping[str, Any]) -> str:
    return f"{fmt}:" + hashlib.sha256(json.dumps(dict(unit), sort_keys=True).encode()).hexdigest()[:16]


# ------------------------------------------------------------------ Google Fact Check Tools


def parse_claim_search(pages: Sequence[Any], unit: Mapping[str, Any]) -> list[dict[str, Any]]:
    fmt = "google-factcheck-claimsearch-json"
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in pages:
        if not isinstance(payload, Mapping) or not isinstance(payload.get("claims", []), list):
            raise FactCheckFormatError("schema_drift", "claims:search response has no claims list")
        for claim in payload.get("claims") or []:
            if not isinstance(claim, Mapping) or not isinstance(claim.get("claimReview", []), list):
                raise FactCheckFormatError("schema_drift", "a claim is not an object with claimReview")
            for review in claim.get("claimReview") or []:
                publisher = dict(review.get("publisher") or {})
                url = _https(review.get("url"))
                site = site_of(publisher.get("site")) or site_of(url)
                if not url or not site:
                    raise FactCheckFormatError("schema_drift", "a claimReview has no review URL or publisher site")
                entry = grouped.setdefault((site, canonical_url(url)[0]), {
                    "url": url, "site": site, "publisher": _clean(publisher.get("name")), "claims": [],
                    "titles": set(), "dates": set(), "languages": set()})
                if review.get("title"):
                    entry["titles"].add(_clean(review["title"]))
                if review.get("reviewDate"):
                    entry["dates"].add(str(review["reviewDate"]))
                if review.get("languageCode"):
                    entry["languages"].add(_clean(review["languageCode"]))
                entry["claims"].append({
                    "claim_text_as_quoted": _clean(claim.get("text")),
                    "claimant_as_named": _clean(claim.get("claimant")),
                    "claimant_type_as_published": None,
                    "claimant_same_as": [],
                    "claim_date": day(claim.get("claimDate")),
                    "appearance_urls": [],
                    "first_appearance_url": None,
                    "rating": _rating(review.get("textualRating")),
                })
    out = []
    unit_key = _unit_key(fmt, unit)
    for (site, canon), entry in sorted(grouped.items()):
        claims = sorted({json.dumps(c, sort_keys=True): c for c in entry["claims"]}.values(),
                        key=lambda c: (c["claim_text_as_quoted"] or "", c["claimant_as_named"] or ""))
        review_date = max(entry["dates"]) if entry["dates"] else None
        fields = {
            "publisher": {"name_as_published": entry["publisher"], "site": site},
            "review_url": entry["url"], "review_url_canonical": canon, "canonicalisation": url_rule()["version"],
            "review_title": min(entry["titles"]) if entry["titles"] else None,
            "review_date": review_date, "claims": claims, "sd_license": None,
            "rating_scale_note": "textual rating only; the API publishes no numeric rating",
        }
        out.append(_record(fmt, review_key(site, entry["url"]), unit_key=unit_key, title=fields["review_title"],
                           locator=entry["url"], fields=fields, publisher_site=site, native_revision=review_date,
                           revision_order=review_date or "", effective_on=review_date,
                           language=min(entry["languages"]) if entry["languages"] else None,
                           review_url_canonical=canon))
    return out


# ------------------------------------------------------------------ Data Commons ClaimReview feed


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _type(node: Any) -> str | None:
    if not isinstance(node, Mapping):
        return None
    kind = node.get("@type")
    return _clean(kind[0] if isinstance(kind, list) and kind else kind)


def _language(value: Any) -> str | None:
    if isinstance(value, Mapping):
        return _clean(value.get("alternateName") or value.get("name"))
    return _clean(value)


def parse_datafeed(payload: Any, unit: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(records, release) for one Data Commons release filtered to the unit's sites and review window."""
    fmt = "datacommons-claimreview-feed-json"
    if not isinstance(payload, Mapping) or not isinstance(payload.get("dataFeedElement"), list):
        raise FactCheckFormatError("schema_drift", "the ClaimReview feed has no dataFeedElement list")
    sites = {site_of(s) for s in unit.get("publisher_sites") or []}
    start, end = day(unit.get("from")), day(unit.get("to"))
    release = {"date_modified": _clean(payload.get("dateModified")), "item_count": len(payload["dataFeedElement"])}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for element in payload["dataFeedElement"]:
        if not isinstance(element, Mapping):
            raise FactCheckFormatError("schema_drift", "a feed element is not an object")
        for item in _as_list(element.get("item")):
            if _type(item) != "ClaimReview":
                continue
            url = _https(item.get("url"))
            publisher = item.get("publisher") if isinstance(item.get("publisher"), Mapping) else item.get("author")
            publisher = publisher[0] if isinstance(publisher, list) and publisher else publisher
            publisher = dict(publisher) if isinstance(publisher, Mapping) else {}
            site = site_of(publisher.get("url")) or site_of(url)
            if not url or not site:
                raise FactCheckFormatError("schema_drift", "a ClaimReview has no review URL or publisher site")
            published = day(item.get("datePublished"))
            if site not in sites or not published or not (start <= published <= end):
                continue
            withheld = []
            person_author = _type(publisher) == "Person"
            if person_author:
                withheld.append("ClaimReview.author (natural-person review author)")
            for key in ("image", "logo"):
                if publisher.get(key):
                    withheld.append(f"ClaimReview.author.{key}")
            reviewed = dict(item.get("itemReviewed") or {})
            claimant = reviewed.get("author")
            claimant = claimant[0] if isinstance(claimant, list) and claimant else claimant
            claimant = dict(claimant) if isinstance(claimant, Mapping) else {}
            for key in ("image", "jobTitle", "birthDate", "address", "email", "telephone"):
                if claimant.get(key):
                    withheld.append(f"itemReviewed.author.{key}")
            appearances, first = [], None
            for node in _as_list(reviewed.get("appearance")):
                link = _https(node.get("url") if isinstance(node, Mapping) else node)
                if link:
                    appearances.append(link)
                if isinstance(node, Mapping) and (node.get("author") or node.get("creator")):
                    withheld.append("itemReviewed.appearance.author")
            first_node = reviewed.get("firstAppearance")
            if first_node:
                first = _https(first_node.get("url") if isinstance(first_node, Mapping) else first_node)
                if isinstance(first_node, Mapping) and (first_node.get("author") or first_node.get("creator")):
                    withheld.append("itemReviewed.firstAppearance.author")
            rating = dict(item.get("reviewRating") or {})
            if rating.get("image"):
                withheld.append("reviewRating.image")
            same_as = [s for s in (_https(v) for v in _as_list(claimant.get("sameAs"))) if s]
            entry = grouped.setdefault((site, canonical_url(url)[0]), {
                "url": url, "site": site, "publisher": None if person_author else _clean(publisher.get("name")),
                "claims": [], "titles": set(), "dates": set(), "modified": set(), "languages": set(),
                "licences": set(), "withheld": set()})
            entry["withheld"].update(withheld)
            if item.get("name") or item.get("headline"):
                entry["titles"].add(_clean(item.get("name") or item.get("headline")))
            entry["dates"].add(published)
            if element.get("dateModified") or item.get("dateModified"):
                entry["modified"].add(str(item.get("dateModified") or element.get("dateModified")))
            if _language(item.get("inLanguage")):
                entry["languages"].add(_language(item.get("inLanguage")))
            if item.get("sdLicense"):
                entry["licences"].add(_clean(item["sdLicense"]))
            entry["claims"].append({
                "claim_text_as_quoted": _clean(item.get("claimReviewed")),
                "claimant_as_named": _clean(claimant.get("name")),
                "claimant_type_as_published": _type(claimant),
                "claimant_same_as": sorted(set(same_as)),
                "claim_date": day(reviewed.get("datePublished")),
                "appearance_urls": sorted(set(appearances)),
                "first_appearance_url": first,
                "rating": _rating(rating.get("alternateName"), rating.get("ratingValue"), rating.get("bestRating"),
                                  rating.get("worstRating"), rating.get("name")),
            })
    out = []
    unit_key = _unit_key(fmt, unit)
    for (site, canon), entry in sorted(grouped.items()):
        claims = sorted({json.dumps(c, sort_keys=True): c for c in entry["claims"]}.values(),
                        key=lambda c: (c["claim_text_as_quoted"] or "", c["claimant_as_named"] or ""))
        review_date = max(entry["dates"])
        modified = max(entry["modified"]) if entry["modified"] else None
        fields = {
            "publisher": {"name_as_published": entry["publisher"], "site": site},
            "review_url": entry["url"], "review_url_canonical": canon, "canonicalisation": url_rule()["version"],
            "review_title": min(entry["titles"]) if entry["titles"] else None,
            "review_date": review_date, "date_modified_as_published": modified, "claims": claims,
            "sd_license": min(entry["licences"]) if entry["licences"] else None,
            "rating_scale_note": "textual_rating is reviewRating.alternateName; ratingValue, bestRating and worstRating "
                                 "are the publisher's own scale as published",
        }
        out.append(_record(fmt, review_key(site, entry["url"]), unit_key=unit_key, title=fields["review_title"],
                           locator=entry["url"], fields=fields, publisher_site=site,
                           native_revision=modified or review_date,
                           revision_order="|".join(p for p in (review_date, modified) if p),
                           effective_on=max(review_date, day(modified) or ""),
                           language=min(entry["languages"]) if entry["languages"] else None,
                           withheld=sorted(entry["withheld"]), review_url_canonical=canon))
    if len(out) > FEED_RECORD_CAP:
        raise FactCheckFormatError("input_limit", "the filtered release exceeds the record cap; never truncated")
    return out, release


# ------------------------------------------------------------------ IFCN signatories listing


class _ListingParser(HTMLParser):
    """Collect one card per signatory profile link: the link text, card text and outbound links.

    The parser is deliberately tolerant: a card starts at an anchor whose path contains ``/profile/`` and runs to
    the next such anchor. Markup assumptions are recorded in the FC01 audit and verified in FC13.
    """

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.cards: list[dict[str, Any]] = []
        self._anchor: dict[str, Any] | None = None
        self._class_stack: list[str] = []

    _VOID = frozenset({"img", "br", "hr", "meta", "link", "input", "source", "wbr", "area", "base", "col"})

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag not in self._VOID:
            self._class_stack.append(str(attributes.get("class") or ""))
        if tag == "a":
            href = urljoin(self.base, str(attributes.get("href") or ""))
            if "/profile/" in urlsplit(href).path:
                self.cards.append({"profile": href, "name": [], "text": [], "links": [], "country": [],
                                   "status": []})
                self._anchor = self.cards[-1]
            elif self.cards and href.startswith(("http://", "https://")):
                self.cards[-1]["links"].append(href)
        if tag == "img" and self.cards:
            self.cards[-1].setdefault("images", 0)

    def handle_endtag(self, tag):
        if self._class_stack and tag not in self._VOID:
            self._class_stack.pop()
        if tag == "a":
            self._anchor = None

    def handle_data(self, data):
        text = _clean(data)
        if not text or not self.cards:
            return
        card = self.cards[-1]
        if self._anchor is card:
            card["name"].append(text)
            return
        classes = " ".join(self._class_stack).casefold()
        if "country" in classes:
            card["country"].append(text)
        if "status" in classes:
            card["status"].append(text)
        card["text"].append(text)


_STATUS_DATE = re.compile(r"(?i)\b(verified on|verified since|expired on|expires on|expiration date|renewal "
                          r"due|since)\s*:?\s*([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2} [A-Za-z]+ [0-9]{4}|[A-Za-z]+ "
                          r"[0-9]{1,2}, [0-9]{4})")


def parse_signatories(raw: bytes, unit: Mapping[str, Any], *, base: str) -> list[dict[str, Any]]:
    fmt = "ifcn-signatories-html"
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise FactCheckFormatError("schema_drift", "the signatories listing is not UTF-8") from exc
    parser = _ListingParser(base)
    parser.feed(text)
    if not parser.cards:
        raise FactCheckFormatError("schema_drift", "no signatory profile links in the listing; nothing is inferred")
    if len(parser.cards) > LISTING_RECORD_CAP:
        raise FactCheckFormatError("input_limit", "the listing exceeds the signatory cap; never truncated")
    unit_key = _unit_key(fmt, unit)
    wanted = {slug(p) for p in unit.get("profiles") or []}
    out = {}
    for card in parser.cards:
        profile = _https(card["profile"])
        name = _clean(" ".join(card["name"]))
        if not profile or not name:
            raise FactCheckFormatError("schema_drift", "a signatory card has no profile link or name")
        profile_slug = urlsplit(profile).path.rstrip("/").rsplit("/", 1)[-1]
        if wanted and slug(profile_slug) not in wanted:
            continue
        body = " ".join(card["text"])
        status = _clean(" ".join(card["status"])) or next(
            (label for label in IFCN_STATUS_LABELS if re.search(rf"(?i)\b{re.escape(label)}\b", body)), None)
        if status:
            status = next((m.group(0) for m in [re.search(rf"(?i)\b{re.escape(status)}\b", body)] if m), status)
        dated = _STATUS_DATE.search(body)
        website = next((link for link in card["links"] if site_of(link) and site_of(link) != site_of(base)), None)
        fields = {
            "name_as_published": name, "ifcn_profile_url": profile, "website": _https(website) or _clean(website),
            "site": site_of(website), "country_as_published": _clean(" ".join(card["country"])),
            "status_as_published": status, "status_date_label": _clean(dated.group(1)) if dated else None,
            "status_date_as_published": day(dated.group(2)) if dated else None,
        }
        withheld = ["signatory logo image"] if "images" in card else []
        out[ifcn_key(profile_slug)] = _record(
            fmt, ifcn_key(profile_slug), unit_key=unit_key, title=name, locator=profile, fields=fields,
            publisher_site=fields["site"], native_revision=fields["status_date_as_published"],
            revision_order=fields["status_date_as_published"] or "",
            effective_on=fields["status_date_as_published"], withheld=withheld)
    return [out[k] for k in sorted(out)]


# ------------------------------------------------------------------ minimisation guard


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of personal attributes or Noesis verdict fields a record still carries (empty when it honours FC01)."""
    found = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                bare = str(key).casefold()
                if bare in FORBIDDEN_PERSONAL_KEYS and item not in (None, "", [], {}):
                    found.append(f"{path}.{key}")
                if bare in FORBIDDEN_VERDICT_KEYS:
                    found.append(f"{path}.{key}")
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(record.get("fields") or {}, "$.fields")
    for index, claim in enumerate((record.get("fields") or {}).get("claims") or []):
        extra = set(claim) - set(CLAIM_KEYS)
        found += [f"$.fields.claims[{index}].{k}" for k in sorted(extra)]
        found += [f"$.fields.claims[{index}].rating.{k}" for k in sorted(set(claim.get("rating") or {})
                                                                          - set(RATING_KEYS))]
    return sorted(set(found))


# ------------------------------------------------------------------ units and requests


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    units = [dict(u) for u in selection.get(key) or [] if isinstance(u, Mapping)]
    if not 1 <= len(units) <= MAX_UNITS or len(units) != len(selection.get(key) or []):
        raise SourcePackError("invalid_manifest", f"a fact-checks selection names 1-{MAX_UNITS} {key} objects")
    for unit in units:
        if key == "queries":
            if not (_clean(unit.get("query")) or _clean(unit.get("publisher_site"))):
                raise SourcePackError("invalid_manifest", "a claim search names a query or a publisher site")
            if not 1 <= int(unit.get("max_age_days") or 0) <= MAX_WINDOW_DAYS:
                raise SourcePackError("invalid_manifest", f"a claim search declares maxAgeDays 1-{MAX_WINDOW_DAYS}")
        if key == "releases":
            sites = unit.get("publisher_sites") or []
            start, end = day(unit.get("from")), day(unit.get("to"))
            if not str(unit.get("path") or "").startswith("/") or not 1 <= len(sites) <= 50:
                raise SourcePackError("invalid_manifest", "a release names its path and 1-50 publisher sites")
            if not start or not end or end < start or \
                    (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days > MAX_WINDOW_DAYS:
                raise SourcePackError("invalid_manifest", f"a release declares a review window of at most "
                                                          f"{MAX_WINDOW_DAYS} days")
        if key == "listings" and not str(unit.get("path") or "").startswith("/"):
            raise SourcePackError("invalid_manifest", "a listing names its path")
    return units


def requests_for(fmt: str, unit: Mapping[str, Any]) -> tuple[str, dict[str, Any], str]:
    """The request path (relative to the endpoint), its parameters and the paging style for one unit."""
    if fmt == "google-factcheck-claimsearch-json":
        params: dict[str, Any] = {"pageSize": GOOGLE_PAGE_SIZE, "maxAgeDays": int(unit["max_age_days"])}
        if _clean(unit.get("query")):
            params["query"] = _clean(unit["query"])
        if _clean(unit.get("publisher_site")):
            params["reviewPublisherSiteFilter"] = site_of(unit["publisher_site"])
        if _clean(unit.get("language")):
            params["languageCode"] = _clean(unit["language"])
        return "/claims:search", params, "token"
    if fmt == "datacommons-claimreview-feed-json":
        return str(unit["path"]), {}, "whole"
    if fmt == "ifcn-signatories-html":
        return str(unit["path"]), {}, "whole"
    raise SourcePackError("invalid_manifest", f"unknown fact-checks format {fmt!r}")


def parse_unit(fmt: str, responses: Sequence[bytes], unit: Mapping[str, Any], *, base: str = ""
               ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the responses of one unit; every emitted record honours the minimisation decision."""
    release: dict[str, Any] = {}
    if fmt == "google-factcheck-claimsearch-json":
        records = parse_claim_search([_json(raw) for raw in responses], unit)
    elif fmt == "datacommons-claimreview-feed-json":
        records, release = parse_datafeed(_json(responses[0]), unit)
    elif fmt == "ifcn-signatories-html":
        records = parse_signatories(responses[0], unit, base=base)
    else:
        raise FactCheckFormatError("schema_drift", f"unknown fact-checks format {fmt!r}")
    for record in records:
        if minimisation_violations(record):
            raise FactCheckFormatError("minimisation_violation", f"{record['record_key']} carries withheld fields")
    return records, release


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FactCheckFormatError("schema_drift", "response is not valid UTF-8 JSON") from exc


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
        raise SourcePackError("invalid_manifest", "keyed fact-checks formats declare a required secret")
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
        self.provider = self.declared["provider"]
        self.units = _units(self.format, dict(self.declared.get("selection") or {}))
        self.secret = secret
        if transport is None:
            if PROVIDER_CONTRACTS[self.provider].get("requires_terms_confirmation") and \
                    not _clean(self.declared.get("terms_confirmation")):
                raise SourcePackError("licensing", "this source's terms are unconfirmed; an operator records a terms "
                                                   "confirmation before any live fetch (FC01)")
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "fact_checks": {"provider": self.provider, "format": self.format, "units": len(self.units),
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
        headers = {"Accept": "application/json, text/html"}
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
        """All pages of one unit (all-or-nothing within MAX_PAGES_PER_UNIT)."""
        path, params, paging = requests_for(self.format, unit)
        if paging == "whole":
            raw, receipt = self._get(path, params)
            return [raw], [receipt]
        pages, receipts, request = [], [], dict(params)
        while True:
            raw, receipt = self._get(path, request)
            pages.append(raw)
            receipts.append(receipt)
            try:
                token = _json(raw).get("nextPageToken")
            except (FactCheckFormatError, AttributeError) as exc:
                raise SourcePackError("schema_drift", f"claims:search response is not an object: {exc}") from exc
            if not token:
                return pages, receipts
            if len(pages) >= MAX_PAGES_PER_UNIT:
                raise SourcePackError("budget_exhausted", "unit is longer than its page bound; never truncated")
            request = {**params, "pageToken": str(token)}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor)
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        responses, requests = self._collect(unit)
        try:
            records, release = parse_unit(self.format, responses, unit, base=self.source["endpoint"])
        except FactCheckFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = "fixture" if all(r["origin"] == "fixture" for r in requests) else "live"
        vintage = None
        if self.format == "datacommons-claimreview-feed-json":
            vintage = f"{release.get('date_modified') or 'undated'}|{requests[0]['sha256'][:16]}"
        receipt = {
            "contract": "noesis-fact-checks-acquisition-receipt-v1", "source_id": self.source["source_id"],
            "provider": self.provider, "format": self.format, "unit_index": index, "unit": unit,
            "unit_key": _unit_key(self.format, unit), "complete_listing": bool(FORMATS[self.format]["complete"]),
            "release": release or None, "vintage": vintage, "requests": requests, "records": len(records),
            "record_keys": [r["record_key"] for r in records], "evidence_origin": origin,
            "live_verification": self.declared["live_verification"], "minimisation": MINIMISATION_POLICY,
            "withheld_fields": sum(len(r["minimisation"]["withheld"]) for r in records),
            "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin, "vintage": vintage}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"],
                "language": record["language"] or "und", "published_at": record["effective_on"],
                "updated_at": record["native_revision"],
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
    "parse_unit",
    "replay_native_fixture",
    "requests_for",
]
