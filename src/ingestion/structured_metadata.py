"""Metadata candidates with deterministic selection and field provenance."""

import hashlib
import json
import importlib.metadata
from urllib.parse import urljoin


def extract_metadata(html, url=None):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    diagnostics = []
    for i, script in enumerate(soup.select('script[type="application/ld+json"]')):
        try:
            json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            diagnostics.append(
                {
                    "locator": f"script[application/ld+json][{i}]",
                    "reason": "malformed_json_ld",
                }
            )

    def add(field, value, origin, locator, rank):
        if isinstance(value, str) and value.strip():
            candidates.append(
                {
                    "field": field,
                    "value": value.strip(),
                    "origin": origin,
                    "locator": locator,
                    "rank": rank,
                }
            )
        elif value is not None:
            diagnostics.append({"locator": locator, "reason": "invalid_field_type"})

    try:
        import extruct

        structured = extruct.extract(
            html, base_url=url or "", syntaxes=["json-ld", "opengraph"], errors="log"
        )
        version = importlib.metadata.version("extruct")
    except ImportError:
        structured = {}
        version = None
        diagnostics.append({"reason": "optional_extruct_unavailable"})
    except Exception:
        structured = {}
        version = None
        diagnostics.append({"reason": "structured_parse_failed"})

    def walk(value, locator):
        if isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, f"{locator}/{i}")
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if not isinstance(types, list):
                diagnostics.append(
                    {"locator": locator + "/@type", "reason": "invalid_field_type"}
                )
                types = []
            if any(
                t in ("Article", "NewsArticle", "BlogPosting", "ScholarlyArticle")
                for t in types
            ):
                identity = value.get("url") or value.get("@id")
                rank = (
                    0
                    if isinstance(identity, str) and urljoin(url or "", identity) == url
                    else 1
                )
                for field, key in [
                    ("title", "headline"),
                    ("published_at", "datePublished"),
                    ("modified_at", "dateModified"),
                    ("canonical_url", "url"),
                ]:
                    add(field, value.get(key), "json-ld", locator + "/" + key, rank)
                authors = value.get("author", [])
                for i, author in enumerate(
                    authors if isinstance(authors, list) else [authors]
                ):
                    add(
                        "author",
                        author.get("name") if isinstance(author, dict) else author,
                        "json-ld",
                        f"{locator}/author/{i}",
                        rank,
                    )
            if "@graph" in value:
                walk(value["@graph"], locator + "/@graph")

    walk(structured.get("json-ld", []), "json-ld")
    for i, meta in enumerate(soup.select("meta[property],meta[name]")):
        key = meta.get("property") or meta.get("name")
        field = {
            "og:title": "title",
            "og:url": "canonical_url",
            "article:published_time": "published_at",
            "article:modified_time": "modified_at",
            "author": "author",
        }.get(key)
        if field:
            add(field, meta.get("content"), "html-meta", f"meta[{i}]", 2)
    for i, canonical in enumerate(soup.select('link[rel="canonical"]')):
        add(
            "canonical_url",
            canonical.get("href"),
            "html-link",
            f"link[rel=canonical][{i}]",
            3,
        )
    h1 = soup.find("h1")
    if h1:
        add("title", h1.get_text(" ", strip=True), "visible-html", "h1[0]", 4)
    selected = {}
    for candidate in sorted(
        candidates, key=lambda c: (c["rank"], c["locator"], c["value"])
    ):
        selected.setdefault(
            candidate["field"],
            {
                **candidate,
                "selection_reason": "matching article URL, then article entity, metadata, canonical link, visible heading; locator breaks ties",
            },
        )
    return {
        "extractor": "extruct+html-metadata",
        "version": version,
        "snapshot_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "selected": selected,
        "candidates": candidates,
        "diagnostics": diagnostics,
        "canonical_identity_authoritative": False,
    }
