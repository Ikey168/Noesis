"""DataCite native DOI metadata, including related-resource provenance."""

from urllib.parse import urlsplit, parse_qs
from src.ingestion.europepmc_api import _Text


def parameters(request, *, cursor, limit):
    values = dict(request.get("parameters") or {})
    allowed = {
        "query",
        "client-id",
        "provider-id",
        "affiliation-id",
        "affiliation-country",
        "resource-type-id",
        "doi",
    }
    if set(values) - allowed:
        raise ValueError("unsupported DataCite parameters")
    if "doi" in values:
        doi = str(values.pop("doi")).removeprefix("https://doi.org/")
        if not doi or any(c in doi for c in ('"', "\\", "\n", "\r")):
            raise ValueError("invalid DOI")
        values["query"] = 'doi:"' + doi + '"'
    if request.get("from_ms") is not None or request.get("to_ms") is not None:
        raise ValueError(
            "DataCite date-range translation is not implemented; use an explicit native query"
        )
    return {
        **values,
        "page[size]": min(int(limit), 1000),
        "page[cursor]": cursor or "1",
        "affiliation": "true",
        "publisher": "true",
        "detail": "true",
    }


def records(payload, *, cursor, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("DataCite response lacks data array")
    if len(payload["data"]) > limit:
        raise ValueError("DataCite exceeded page budget")
    result = []
    for item in payload["data"]:
        attrs = item.get("attributes", {})
        doi = str(attrs.get("doi") or item.get("id") or "").lower()
        if not doi.startswith("10.") or "/" not in doi:
            raise ValueError("DataCite record lacks DOI")
        titles = attrs.get("titles") or []
        title = str(titles[0].get("title") or "") if titles else ""
        description = next(
            (
                d.get("description", "")
                for d in attrs.get("descriptions") or []
                if d.get("descriptionType") == "Abstract"
            ),
            "",
        )
        parser = _Text()
        parser.feed(description)
        parser.close()
        abstract = "".join(parser.parts).strip()
        result.append(
            {
                "id": doi,
                "doi": doi,
                "url": "https://doi.org/" + doi,
                "title": title,
                "content": abstract or title,
                "content_representation": "plain-text-abstract"
                if abstract
                else "title-only",
                "authors": [
                    str(
                        c.get("name")
                        or " ".join(
                            str(c.get(k) or "") for k in ("givenName", "familyName")
                        ).strip()
                    )
                    for c in attrs.get("creators") or []
                ],
                "provider_publication_date": attrs.get("publicationYear"),
                "published_at": None,
                "related_identifiers": attrs.get("relatedIdentifiers") or [],
                "rights": attrs.get("rightsList") or [],
                "landing_page": attrs.get("url"),
                "native_record": item,
            }
        )
    next_link = (payload.get("links") or {}).get("next")
    next_cursor = None
    if next_link:
        link = urlsplit(next_link)
        if (
            link.scheme != "https"
            or link.hostname != "api.datacite.org"
            or link.path != "/dois"
        ):
            raise ValueError("unexpected DataCite pagination origin")
        next_cursor = parse_qs(link.query).get("page[cursor]", [None])[0]
        if not next_cursor:
            raise ValueError("DataCite next page lacks cursor")
    if not result or next_cursor == cursor:
        next_cursor = None
    return result, next_cursor
