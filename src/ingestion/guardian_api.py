"""Shared Guardian native page mapping for legacy and source-pack collectors."""

from datetime import UTC, datetime
from urllib.parse import urlsplit

from src.ingestion.europepmc_api import _Text


def is_guardian(source):
    return (
        source["source_id"] == "guardian-content"
        and source["endpoint"] == "https://content.guardianapis.com/search"
    )


def article_identity(url):
    parsed = urlsplit(url)
    if parsed.hostname not in ("www.theguardian.com", "theguardian.com"):
        return None
    import re

    path = parsed.path.strip("/")
    return (
        "guardian:" + path
        if re.search(r"/\d{4}/[a-z]{3}/\d{1,2}/[^/]+", "/" + path)
        else None
    )


def parameters(values, *, cursor, limit, secret, from_ms=None, to_ms=None):
    result = {
        "page": int(cursor or 1),
        "page-size": min(50, limit),
        "show-fields": "headline,byline,thumbnail,short-url,body,trailText,lastModified",
        "show-tags": "contributor",
    }
    if not 1 <= result["page"] <= 100000:
        raise ValueError("invalid Guardian cursor")
    for key, native in [
        ("query", "q"),
        ("q", "q"),
        ("section", "section"),
        ("from_date", "from-date"),
        ("to_date", "to-date"),
    ]:
        if values.get(key):
            result[native] = values[key]
    for stamp, native in [(from_ms, "from-date"), (to_ms, "to-date")]:
        if stamp is not None:
            result[native] = (
                datetime.fromtimestamp(stamp / 1000, UTC).date().isoformat()
            )
    if secret:
        result["api-key"] = secret
    return result


def records(payload, *, limit):
    envelope = payload.get("response", {}) if isinstance(payload, dict) else {}
    if not isinstance(envelope, dict) or not isinstance(envelope.get("results"), list):
        raise ValueError("Guardian response lacks results")  # noqa: TRY004 - external schema failure
    values = envelope["results"]
    if len(values) > limit:
        raise ValueError("Guardian exceeded page budget")
    output = []
    for item in values:
        if not item.get("id") or not item.get("webUrl"):
            raise ValueError("Guardian result lacks identity")
        fields = item.get("fields") or {}
        parser = _Text()
        parser.feed(fields.get("body") or "")
        parser.close()
        body = "".join(parser.parts).strip()
        authors = [
            tag["webTitle"]
            for tag in item.get("tags", [])
            if tag.get("type") == "contributor" and tag.get("webTitle")
        ]
        output.append(
            {
                "id": item["id"],
                "title": fields.get("headline") or item.get("webTitle"),
                "url": item["webUrl"],
                "content": body
                or fields.get("trailText")
                or item.get("webTitle")
                or "",
                "body_html": fields.get("body"),
                "authors": authors,
                "author": ", ".join(authors) or fields.get("byline", ""),
                "published_at": item.get("webPublicationDate"),
                "updated_at": fields.get("lastModified"),
                "source": "The Guardian",
                "service": "guardian",
                "section": item.get("sectionName"),
                "url_to_image": fields.get("thumbnail"),
                "description": fields.get("trailText", ""),
                "reporting_origin": article_identity(item["webUrl"]),
                "content_representation": "full-text-html"
                if body
                else "partial-metadata",
            }
        )
    page = int(envelope.get("currentPage", 1))
    pages = int(envelope.get("pages", page))
    return output, str(page + 1) if values and page < pages else None


def collect_with_preference(
    url, fetchers, *, preference=("api", "rss", "html"), store=None
):
    """Acquire in declared order, retaining partial representations before fallback.

    Fetchers implement existing collection paths and return document mappings.
    A representation is complete only when its coverage explicitly says so.
    """
    import json

    origin = article_identity(url)
    if (
        origin is None
        or not preference
        or len(set(preference)) != len(preference)
        or set(preference) - {"api", "rss", "html"}
    ):
        raise ValueError("invalid Guardian acquisition configuration")
    attempts, retained = [], []
    selected = None
    for route in preference:
        if route not in fetchers:
            attempts.append({"route": route, "outcome": "unavailable"})
            continue
        try:
            document = dict(fetchers[route](url))
            if article_identity(document.get("url", "")) != origin:
                raise ValueError("acquisition returned a different article")
            metadata = dict(document.get("metadata") or {})
            complete = bool(document.get("content")) and metadata.get(
                "content_coverage"
            ) in {"full-text", "full-text-html"}
            metadata.update(
                reporting_origin=origin,
                acquisition_route=route,
                content_coverage=metadata.get("content_coverage")
                if complete
                else "partial",
            )
            metadata["acquisition_provenance_json"] = json.dumps(
                {"route": route, "preference": list(preference), "requested_url": url},
                sort_keys=True,
            )
            document["metadata"] = metadata
            if store is not None:
                result = store.upsert([document])
                if result.invalid:
                    raise ValueError("acquisition document rejected")
            retained.append(document)
            attempts.append(
                {
                    "route": route,
                    "outcome": "full-text" if complete else "partial",
                    "document_id": document["document_id"],
                }
            )
            if selected is None or complete:
                selected = document
            if complete:
                break
        except Exception:  # noqa: BLE001 - failed route permits explicitly configured fallback
            attempts.append({"route": route, "outcome": "failed"})
    return {
        "reporting_origin": origin,
        "preference": list(preference),
        "attempts": attempts,
        "selected": selected,
        "retained": retained,
        "outcome": "unavailable"
        if selected is None
        else selected["metadata"]["content_coverage"],
    }
