"""DataCite native DOI metadata, including related-resource provenance."""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

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
        "author",
        "version",
    }
    if set(values) - allowed:
        raise ValueError("unsupported DataCite parameters")
    operation = request.get("operation", "search")
    query = values.pop("query", None)
    if operation in {"doi", "author", "version"} and query is not None:
        if operation in values and values[operation] != query:
            raise ValueError("conflicting DataCite operation parameters")
        values[operation] = query
        query = None
    if operation in {"doi", "author", "version"} and operation not in values:
        raise ValueError("DataCite operation requires a query value")
    clauses = ["(" + str(query) + ")"] if query else []
    for key, field in (
        ("doi", "doi"),
        ("author", "creators.name"),
        ("version", "version"),
    ):
        if key not in values:
            continue
        value = str(values.pop(key))
        if key == "doi":
            value = value.removeprefix("https://doi.org/").lower()
            if not value.startswith("10.") or "/" not in value:
                raise ValueError("invalid DOI")
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("invalid DataCite query value")
        value = value.replace(chr(92), chr(92) * 2).replace('"', chr(92) + '"')
        clauses.append(field + ':"' + value + '"')
    if request.get("from_ms") is not None or request.get("to_ms") is not None:

        def bound(key):
            value = request.get(key)
            return (
                "*"
                if value is None
                else datetime.fromtimestamp(int(value) / 1000, UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )

        # Backfills track metadata updates, not a guessed publication day from a year.
        clauses.append("updated:[" + bound("from_ms") + " TO " + bound("to_ms") + "]")
    if clauses:
        values["query"] = " AND ".join(clauses)
    return {
        **values,
        "page[size]": min(int(limit), 1000),
        "page[cursor]": cursor or "1",
        "affiliation": "true",
        "publisher": "true",
        "detail": "true",
    }


# Preserve DataCite's directed predicates, including future vocabulary additions.
# They express bibliographic relationships, never independent factual support.
def related_resources(doi, attrs):
    links = []
    for related in attrs.get("relatedIdentifiers") or []:
        target = related.get("relatedIdentifier")
        identifier_type = related.get("relatedIdentifierType")
        predicate = related.get("relationType")
        if not all(
            isinstance(v, str) and v.strip()
            for v in (target, identifier_type, predicate)
        ):
            raise ValueError(
                "DataCite related identifier lacks target, type or predicate"
            )
        if identifier_type.upper() == "DOI":
            target = target.removeprefix("https://doi.org/").lower()
        link = {
            "source_identifier": doi,
            "source_identifier_type": "DOI",
            "predicate": predicate,
            "target_identifier": target,
            "target_identifier_type": identifier_type,
            "provider": "datacite",
            "provider_record_url": "https://api.datacite.org/dois/" + doi,
            "assertion_kind": "bibliographic-relationship",
            "native": related,
        }
        if link not in links:
            links.append(link)
    return links


def records(payload, *, cursor, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("DataCite response lacks data array")  # noqa: TRY004 - native response schema failure
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
                "updated_at": attrs.get("updated"),
                "version": attrs.get("version"),
                "related_identifiers": attrs.get("relatedIdentifiers") or [],
                "related_resources": related_resources(doi, attrs),
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
