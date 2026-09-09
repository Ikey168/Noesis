"""Native scholarly metadata requests; abstracts are never labelled full text."""

from datetime import datetime, timezone
from urllib.parse import urlsplit
from src.ingestion.europepmc_api import _Text


def provider(source):
    endpoint = urlsplit(source["endpoint"])
    if source["source_id"] == "datacite-dois" and endpoint.hostname == "api.datacite.org" and endpoint.path.rstrip("/") == "/dois":
        return "datacite"
    for name, host in [
        ("crossref", "api.crossref.org"),
        ("openalex", "api.openalex.org"),
    ]:
        if (
            source["source_id"] == name + "-works"
            and endpoint.hostname == host
            and endpoint.path.rstrip("/") == "/works"
        ):
            return name
    return None


def parameters(name, request, *, cursor, limit, contact=None, secret=None):
    if name == "datacite":
        from src.ingestion.datacite_api import parameters as datacite_parameters
        return datacite_parameters(request, cursor=cursor, limit=limit)
    values = dict(request.get("parameters") or {})
    filters = []
    result = {}
    query = values.pop("query", values.pop("search", None))
    operation = request.get("operation", "search")
    if query and operation in {"doi", "author", "venue", "concept"}:
        values.setdefault(operation, query)
    elif query:
        result["query" if name == "crossref" else "search"] = query
    mapping = {
        "doi": "doi",
        "author": "query.author" if name == "crossref" else "authorships.author.id",
        "venue": "query.container-title"
        if name == "crossref"
        else "primary_location.source.id",
        "concept": "concepts.id",
    }
    for key, target in mapping.items():
        value = values.pop(key, None)
        if value is not None:
            if name == "crossref" and target.startswith("query."):
                result[target] = value
            else:
                if "," in str(value) or "|" in str(value):
                    raise ValueError("identifier contains filter delimiters")
                filters.append(
                    target + ":" + str(value).removeprefix("https://doi.org/")
                )
    for key, native in [
        ("from_ms", "from"),
        ("to_ms", "until" if name == "crossref" else "to"),
    ]:
        if request.get(key) is not None:
            date = (
                datetime.fromtimestamp(int(request[key]) / 1000, timezone.utc)
                .date()
                .isoformat()
            )
            filters.append(
                native
                + ("-pub-date:" if name == "crossref" else "_publication_date:")
                + date
            )
    for key, native in [
        ("from_date", "from"),
        ("to_date", "until" if name == "crossref" else "to"),
    ]:
        value = values.pop(key, None)
        if value is not None:
            from datetime import date

            checked = date.fromisoformat(value).isoformat()
            filters.append(
                native
                + ("-pub-date:" if name == "crossref" else "_publication_date:")
                + checked
            )
    # Native filters remain available, but pagination/credentials are adapter-owned.
    if values.get("filter"):
        filters.append(str(values.pop("filter")))
    for control in ("limit", "rows", "per_page", "cursor", "api_key", "mailto"):
        values.pop(control, None)
    if values:
        raise ValueError("unsupported scholarly request parameters")
    if filters:
        result["filter"] = ",".join(filters)
    result.update(cursor=cursor or "*")
    result["rows" if name == "crossref" else "per_page"] = min(
        limit, 1000 if name == "crossref" else 200
    )
    if name == "crossref" and contact:
        result["mailto"] = contact
    if name == "openalex" and secret:
        result["api_key"] = secret
    return result


def records(name, payload, *, cursor, limit):
    if name == "datacite":
        from src.ingestion.datacite_api import records as datacite_records
        return datacite_records(payload, cursor=cursor, limit=limit)
    envelope = (
        payload.get("message")
        if name == "crossref" and isinstance(payload, dict)
        else payload
    )
    key = "items" if name == "crossref" else "results"
    if not isinstance(envelope, dict) or not isinstance(envelope.get(key), list):
        raise ValueError("native scholarly response lacks record collection")
    values = envelope[key]
    if len(values) > limit:
        raise ValueError("provider exceeded requested page budget")
    output = []
    for item in values:
        if not isinstance(item, dict) or not item.get(
            "DOI" if name == "crossref" else "id"
        ):
            raise ValueError("native scholarly record lacks stable identifier")
        doi = (
            str(item.get("DOI") or item.get("doi") or "")
            .removeprefix("https://doi.org/")
            .lower()
        )
        if name == "crossref":
            title = item.get("title") or []
            if not isinstance(title, list):
                raise ValueError("Crossref title must be an array")
            title = str(title[0]) if title else ""
            parser = _Text()
            parser.feed(item.get("abstract") or "")
            parser.close()
            abstract = "".join(parser.parts).strip()
            authors = [
                " ".join(str(a.get(k) or "") for k in ("given", "family")).strip()
                or a.get("name", "")
                for a in item.get("author", [])
            ]
            parts = (item.get("published") or item.get("issued") or {}).get(
                "date-parts", [[]]
            )[0]
            date = (
                "-".join(str(v).zfill(4 if i == 0 else 2) for i, v in enumerate(parts))
                or None
            )
            identity, url = doi, "https://doi.org/" + doi
        else:
            title = item.get("title") or item.get("display_name") or ""
            inverted = item.get("abstract_inverted_index") or {}
            positions = {}
            for word, offsets in inverted.items():
                for offset in offsets:
                    if (
                        not isinstance(offset, int)
                        or offset < 0
                        or offset > 100000
                        or offset in positions
                    ):
                        raise ValueError("invalid abstract position")
                    positions[offset] = word
            if positions and set(positions) != set(range(len(positions))):
                raise ValueError("abstract positions are not contiguous")
            abstract = " ".join(positions[i] for i in range(len(positions)))
            authors = [
                a.get("author", {}).get("display_name", "")
                for a in item.get("authorships", [])
            ]
            date = item.get("publication_date")
            identity, url = item["id"], item["id"]
        output.append(
            {
                **item,
                "id": identity,
                "doi": doi or None,
                "url": url,
                "title": title,
                "authors": authors,
                "published_at": date if date and len(date) == 10 else None,
                "provider_publication_date": date,
                "content": abstract or title,
                "abstract_available": bool(abstract),
                "content_representation": "plain-text-abstract"
                if abstract
                else "title-only",
            }
        )
    next_cursor = (
        envelope.get("next-cursor")
        if name == "crossref"
        else (envelope.get("meta") or {}).get("next_cursor")
    )
    if not values or len(values) < limit or next_cursor == cursor:
        next_cursor = None
    return output, next_cursor
