"""Bounded native CELLAR, federal-court (RII) and Berlin legal acquisition for the Legal pack.

The parsers are the existing, reviewed ones in
:mod:`src.ingestion.regional_providers` (#1480, #1481, #1482); this module only
adapts them to the source-pack runtime so budgets, retries, quarantine,
checkpoints and projection apply. Each source declares an explicit, bounded
``legal`` selection:

* ``cellar`` - up to 20 CELEX numbers and 1-24 languages, paged SPARQL rows.
  An optional ``text`` selection also fetches the text of up to 10 items of
  the selected manifestation formats from the same host and splits it into
  located passages: ``xhtml-paragraphs`` (one passage per paragraph) or
  ``eu-control-list-annex`` (one passage per control code, e.g. the Annex I
  entries of the dual-use Regulation (EU) 2021/821, located by the code).
* ``rii`` - explicit ``doknr`` identities and/or one bounded index window
  (court and modified-since filters) from the official RII table of contents.
* ``berlin-law`` - explicit documented download or rendered-page URLs on
  gesetze.berlin.de, each with its official ID, format and historical state.
* ``gesetze-im-internet`` - the bounded federal statute set (FL01) located
  through ``gii-toc.xml`` and fetched as per-statute ``xml.zip``; every fetch is
  an *observed* version (#2105, FL03).
* ``rechtsinformationen-bund`` - the federal legal information portal's
  search for each selected statute (exact abbreviation) and up to 20 of its
  expressions as LegalDocML.de, each a *source-stated* version (FL04).
* ``recht-bund`` - digital Federal Law Gazette promulgations (since 2023):
  explicit BGBl citations and/or one bounded listing; only acts that amend a
  selected statute are acquired, the others are recorded as seen (FL05).

Nothing here infers that an instrument is in force: records keep
``is_current_law`` unknown and the Legal store only records sourced dates.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
FEDERAL_CONNECTORS = frozenset({"gesetze-im-internet", "rechtsinformationen-bund", "recht-bund"})
LEGAL_CONNECTORS = frozenset({"cellar", "rii", "berlin-law"}) | FEDERAL_CONNECTORS
MAX_SELECTION = 50
MAX_STATUTES = 20
JURISDICTIONS = {"cellar": "EU", "rii": "DE", "berlin-law": "DE-BE", "gesetze-im-internet": "DE",
                 "rechtsinformationen-bund": "DE", "recht-bund": "DE"}
PROVIDER_CONTRACTS = {
    "cellar": {
        "provider": "cellar",
        "jurisdiction": "EU",
        "access": "Public CELLAR SPARQL endpoint (publications.europa.eu/webapi/rdf/sparql), explicit CELEX selections",
        "formats": ["application/sparql-results+json"],
        "document_classes": ["legislation", "case-law", "corrigenda"],
        "identifiers": ["CELEX", "ELI", "ECLI", "CELLAR work/expression/manifestation/item URIs"],
        "authentication": "none",
        "coverage": "Selected EU works; language expressions and manifestations stay distinct; current law is not inferred",
        "text_retrieval": "optional: XHTML items of selected works on publications.europa.eu, split into located "
                          "passages (unverified-live: item URLs may answer with same-host redirects only)",
        "prior_live_evidence": "docs/development/workflow-review-evidence/cellar-native-2026-09-09.json",
    },
    "rii": {
        "provider": "german-courts",
        "jurisdiction": "DE",
        "access": "Official Rechtsprechung im Internet table of contents (rii-toc.xml) and per-decision ZIP/XML downloads",
        "formats": ["application/xml", "application/zip"],
        "document_classes": ["federal court decisions"],
        "identifiers": ["doknr", "docket number (Aktenzeichen)", "ECLI where published"],
        "authentication": "none",
        "coverage": "Decisions of the federal courts published on rechtsprechung-im-internet.de, not all German case law",
        "prior_live_evidence": "docs/development/workflow-review-evidence/court-streaming-live-2026-09-08.json",
    },
    "berlin-law": {
        "provider": "berlin-law",
        "jurisdiction": "DE-BE",
        "access": "Documented public portal downloads (juris XML ZIP) and rendered judgment pages on gesetze.berlin.de; no unrestricted API",
        "formats": ["application/zip (juris XML)", "text/html (rendered judgment)"],
        "document_classes": ["laws", "regulations", "court decisions", "gazette publications (explicit import)"],
        "identifiers": ["juris doknr (jlr-…/NJRE…)", "GVBl. reference"],
        "authentication": "none",
        "coverage": "Selected official Berlin publications; historical versions remain historical; editorial text is excluded",
        "prior_live_evidence": "docs/development/workflow-review-evidence/berlin-native-2026-09-09.json",
    },
    # Federal statutes feature (#2105). Access decisions and every claim still to verify live:
    # docs/development/federal-statutes-evidence/source-audit.md.
    "gesetze-im-internet": {
        "provider": "gesetze-im-internet",
        "jurisdiction": "DE",
        "access": "Public table of contents (gii-toc.xml) and per-statute xml.zip downloads on "
                  "www.gesetze-im-internet.de; no API key",
        "formats": ["application/xml (gii-toc)", "application/zip (gii-norm XML)"],
        "document_classes": ["federal statutes (current consolidation only)"],
        "identifiers": ["jurabk / amtabk", "gii doknr (BJNR…)", "statute directory (e.g. bgb)"],
        "authentication": "none",
        "validity": "observed: the current text as seen on the fetch date; the 'Stand' note is kept verbatim and "
                    "never turned into a validity interval",
        "access_decision": "implement (unverified-live)",
        "coverage": "The bounded statute set only; not all federal law",
        "prior_live_evidence": None,
    },
    "rechtsinformationen-bund": {
        "provider": "rechtsinformationen-bund",
        "jurisdiction": "DE",
        "access": "Federal legal information portal API (search and LegalDocML.de expressions); trial service at "
                  "testphase.rechtsinformationen.bund.de - verify status, terms and rate limits live",
        "formats": ["application/ld+json (search)", "application/xml (LegalDocML.de / Akoma Ntoso)"],
        "document_classes": ["federal statutes: versions (expressions) with stated validity"],
        "identifiers": ["ELI (work and expression)", "abbreviation"],
        "authentication": "none (verify)",
        "validity": "source_stated: temporalCoverage of each expression",
        "access_decision": "implement behind the federal-statutes feature (unverified-live; trial phase)",
        "coverage": "Statutes of the bounded set for which the portal publishes versions",
        "prior_live_evidence": None,
    },
    "recht-bund": {
        "provider": "recht-bund",
        "jurisdiction": "DE",
        "access": "Digital Federal Law Gazette on www.recht.bund.de (promulgations since 1 January 2023); "
                  "listing and LegalDocML.de document paths are declared in the source and must be verified live",
        "formats": ["application/rss+xml or Atom (listing)", "application/xml (LegalDocML.de promulgation)"],
        "document_classes": ["amendment acts (BGBl. I/II promulgations)"],
        "identifiers": ["BGBl citation (year, part, number)", "ELI eli/bund/bgbl-1/<year>/<number>"],
        "authentication": "none",
        "validity": "promulgation date and the entry-into-force article as published; instructions never applied",
        "access_decision": "implement (unverified-live); historical BGBl before 2023 on bgbl.de: link-only",
        "coverage": "Acts that amend a statute of the bounded set; others are recorded as seen, not acquired",
        "prior_live_evidence": None,
    },
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def legal_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    legal = dict(source.get("legal") or {})
    connector = source["connector"]
    selection = legal.get("selection")
    if connector == "cellar":
        celex = list(dict(selection or {}).get("celex") or [])
        languages = list(dict(selection or {}).get("languages") or [])
        if not 1 <= len(celex) <= 20 or any(not re.fullmatch(r"[0-9A-Z()._-]{5,50}", c) for c in celex):
            raise SourcePackError("unbounded_source", "cellar sources select 1-20 CELEX numbers")
        if not 1 <= len(languages) <= 24 or any(not re.fullmatch(r"[A-Z]{3}", lang) for lang in languages):
            raise SourcePackError("invalid_mapping", "cellar sources select 1-24 three-letter languages")
        text = dict(selection or {}).get("text")
        if text is not None:
            text = dict(text)
            formats = list(text.get("formats") or [])
            if text.get("parser") not in TEXT_PARSERS or not 1 <= len(formats) <= 3 \
                    or any(not re.fullmatch(r"[a-z0-9]{2,12}", f) for f in formats) \
                    or not 1 <= int(text.get("max_items") or 0) <= 10:
                raise SourcePackError("invalid_mapping", "cellar text selections name a parser, 1-3 formats and "
                                                         "1-10 max_items")
    elif connector == "rii":
        decisions = list(dict(selection or {}).get("decisions") or [])
        index = dict(selection or {}).get("index")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", d) for d in decisions):
            raise SourcePackError("invalid_mapping", "rii decisions are official doknr identities")
        if index is not None and not 1 <= int(dict(index).get("limit") or 0) <= MAX_SELECTION:
            raise SourcePackError("unbounded_source", f"rii index windows select 1-{MAX_SELECTION} decisions")
        if not decisions and index is None or len(decisions) > MAX_SELECTION:
            raise SourcePackError("unbounded_source", "rii sources need explicit decisions or one bounded index window")
    elif connector == "berlin-law":
        items = list(selection or [])
        if not 1 <= len(items) <= MAX_SELECTION:
            raise SourcePackError("unbounded_source", f"berlin-law sources select 1-{MAX_SELECTION} publications")
        for item in items:
            if item.get("format") not in {"juris-xml-zip", "rendered-html"} or not item.get("official_id") \
                    or type(item.get("historical")) not in {bool, type(None)}:
                raise SourcePackError(
                    "invalid_mapping",
                    "berlin-law selections name official_id, format (juris-xml-zip|rendered-html) and historical",
                )
    elif connector in FEDERAL_CONNECTORS:
        _federal_declaration(connector, dict(selection or {}))
    return legal


def _statute_selection(selection: Mapping[str, Any], *, require: bool = True) -> list[dict[str, Any]]:
    from src.kb.legal_citations import FEDERAL_STATUTE_SET

    statutes = list(selection.get("statutes") or [])
    if (require and not statutes) or len(statutes) > MAX_STATUTES:
        raise SourcePackError("unbounded_source", f"federal sources select 1-{MAX_STATUTES} statutes")
    output = []
    for statute in statutes:
        statute = dict(statute) if isinstance(statute, Mapping) else {"jurabk": statute}
        jurabk = str(statute.get("jurabk") or "")
        if not re.fullmatch(r"[A-Za-zÄÖÜäöüß][A-Za-zÄÖÜäöüß0-9 -]{0,30}", jurabk):
            raise SourcePackError("invalid_mapping", "federal statute selections name the official abbreviation")
        known = FEDERAL_STATUTE_SET.get(jurabk, {})
        output.append({"jurabk": jurabk, "gii_path": statute.get("gii_path") or known.get("gii_path"),
                       "names": list(statute.get("names") or known.get("names") or []),
                       "title": statute.get("title") or known.get("title"), "eli_work": statute.get("eli_work")})
    return output


def _federal_declaration(connector: str, selection: Mapping[str, Any]) -> None:
    statutes = _statute_selection(selection)
    if connector == "gesetze-im-internet":
        if any(not re.fullmatch(r"[a-z0-9_.-]{1,60}", str(s["gii_path"] or "")) for s in statutes) \
                or not re.fullmatch(r"/[A-Za-z0-9_./-]{1,100}\.xml", str(selection.get("toc_path") or "")):
            raise SourcePackError("invalid_mapping", "gesetze-im-internet selections name toc_path and a gii_path "
                                                     "per statute")
    elif connector == "rechtsinformationen-bund":
        if not 1 <= int(selection.get("max_expressions") or 0) <= 20:
            raise SourcePackError("unbounded_source", "rechtsinformationen-bund selections read 1-20 expressions "
                                                      "per statute")
    else:
        items = list(selection.get("items") or [])
        index = selection.get("index")
        if not items and index is None or len(items) > MAX_SELECTION:
            raise SourcePackError("unbounded_source", "recht-bund sources need explicit items or one bounded index")
        if index is not None and (not 1 <= int(dict(index).get("limit") or 0) <= MAX_SELECTION
                                  or not str(dict(index).get("path") or "").startswith("/")):
            raise SourcePackError("unbounded_source", f"recht-bund listings select 1-{MAX_SELECTION} promulgations")
        for item in items:
            if int(dict(item).get("part") or 0) not in (1, 2) or not str(dict(item).get("number") or "").isdigit():
                raise SourcePackError("invalid_mapping", "recht-bund items name part (1|2), year and number")
            if int(dict(item).get("year") or 0) < 2023:
                raise SourcePackError("invalid_mapping", "the Federal Law Gazette before 2023 is link-only (FL01)")
        template = str(selection.get("document_path") or "")
        if not template.startswith("/") or not all(f"{{{k}}}" in template for k in ("part", "year", "number")):
            raise SourcePackError("invalid_mapping", "recht-bund selections declare a document_path with {part}, "
                                                     "{year} and {number}")
    del statutes


class _LegalAdapter:
    accepts_transport = True
    connector = ""

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.legal = legal_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "legal": {"jurisdiction": JURISDICTIONS[source["connector"]], "selection": self.legal.get("selection")},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "selection": self.legal.get("selection")})

    def _get(self, url: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None):
        host = (urlsplit(url).hostname or "").casefold()
        if host != (urlsplit(self.source["endpoint"]).hostname or "").casefold():
            raise SourcePackError("network_policy", "legal sources fetch only from their declared host")
        response = self.transport(url=url, params=dict(params or {}), headers=dict(headers or {}),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers_ = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.connector} refused access (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.connector} returned HTTP {status}")
        return status, raw

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "legal runs use the pinned selection, not ad-hoc parameters")

    def _cursor(self, cursor: str | None) -> dict[str, Any]:
        if cursor is None:
            return {}
        try:
            state = json.loads(cursor)
        except ValueError as exc:
            raise SourcePackError("cursor_drift", "legal cursor is not a valid checkpoint") from exc
        if not isinstance(state, dict) or state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "legal cursor belongs to a different selection")
        return state

    @staticmethod
    def _wrap(record: Mapping[str, Any], page_info: Mapping[str, Any]) -> dict[str, Any]:
        text = "\n\n".join(part["text"] for part in record.get("sections") or [] if part.get("text"))
        return {
            # One document per published source location: two versions of one
            # work (e.g. historical and current text) are distinct documents.
            "id": (f"{record['provider']}:{record['kind']}:{record['language']}:{record['provider_id']}:"
                   + hashlib.sha256(str(record["source_url"]).encode()).hexdigest()[:12]),
            "title": record["title"],
            "language": record["language"],
            "url": record["source_url"],
            "published_at": record.get("published_at"),
            **({"content": text} if text else {}),
            "legal_record": dict(record),
            "legal_page": dict(page_info),
        }

    def _page(self, records, next_state, raw_bytes, receipt):
        from src.ingestion.source_pack_runtime import RuntimePage

        next_cursor = None if next_state is None else json.dumps({**next_state, "scope": self._scope()},
                                                                 sort_keys=True)
        return RuntimePage(tuple(records), next_cursor, raw_bytes, receipt=receipt)


def _provider_error(exc: Exception) -> SourcePackError:
    code = getattr(exc, "code", "schema_drift")
    mapped = {"schema_drift": "schema_drift", "source_identity": "schema_drift", "input_limit": "response_too_large",
              "unavailable_text": "schema_drift", "unsupported_format": "schema_drift",
              "unsupported_archive": "schema_drift", "archive_limit": "response_too_large",
              "empty_source": "schema_drift"}.get(code, "schema_drift")
    return SourcePackError(mapped, f"{code}: {exc}")


CONTROL_CODE = re.compile(r"^(\d[A-E]\d{3})(?=\b|[.\s])")
_ANNEX = re.compile(r"^ANNEX\s+([IVX]+[a-z]?)\b", re.I)
# Headings EUR-Lex renders as plain paragraphs: annex, part and section
# titles, category titles ("CATEGORY 1 — ...") and product-group titles
# ("1B Test, inspection and production equipment").
_BREAK = re.compile(r"^(?i:CATEGORY\s+\d|ANNEX\b|PART\s+[IVX]+\b|SECTION\s+[A-Z0-9]+\b)|^\d[A-E](?:\s|$)")


def _xhtml_blocks(raw: bytes) -> list[tuple[str, str, str | None]]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(raw, "html.parser")
    for element in soup.select("script,style,nav,footer,header,noscript"):
        element.decompose()
    main = soup.body or soup
    blocks = []
    for element in main.select("h1,h2,h3,h4,p,li,td"):
        if element.find(["p", "li", "td"]):
            continue  # a container's text is emitted by its own paragraphs
        text = " ".join(element.get_text(" ", strip=True).split())
        if text:
            blocks.append((text, element.name, element.get("id")))
    if len(blocks) > 20000:
        raise SourcePackError("response_too_large", "item text has too many paragraphs")
    return blocks


def parse_xhtml_paragraphs(raw: bytes) -> list[dict[str, Any]]:
    """One passage per paragraph, located by its index among headings and paragraphs."""
    return [{"text": text, "locator": {"kind": "xhtml-paragraph", "index_in_headings_and_paragraphs": index,
                                       "tag": tag, "id": ident,
                                       "precision": "element selection; not byte offsets"}}
            for index, (text, tag, ident) in enumerate(_xhtml_blocks(raw))]


def parse_control_list_annex(raw: bytes) -> list[dict[str, Any]]:
    """One passage per control code within its annex (the code and its paragraphs up to the next code or heading).

    Entries are scoped to the annex they appear in (``ANNEX I``, ``ANNEX IV``
    ...): a code repeated in another annex is a separate passage located as
    ``annex-<n>/<code>``, never appended to the Annex I entry. A control entry
    is located by its annex and code so the same entry can be compared across
    editions; other paragraphs (including group, category and annex headings)
    keep their paragraph index. The text is the source text; the category is
    the code's first digit as the annex numbers it, not an interpretation.
    """
    sections: list[dict[str, Any]] = []
    entries: dict[tuple[str | None, str], dict[str, Any]] = {}
    current = None
    annex = None
    for index, (text, tag, ident) in enumerate(_xhtml_blocks(raw)):
        heading = _ANNEX.match(text)
        if heading:
            annex = heading.group(1).lower()
        match = None if heading else CONTROL_CODE.match(text)
        if match:
            code = match.group(1)
            current = entries.get((annex, code))
            if current is None:
                current = {"text": text, "locator": {"kind": "control-entry", "official_norm_id": code,
                                                     "annex": annex, "path": f"annex-{annex or 'none'}/{code}",
                                                     "category": code[0], "product_group": code[1],
                                                     "paragraph_indexes": [index],
                                                     "precision": "control-code grouping of source paragraphs"}}
                entries[(annex, code)] = current
                sections.append(current)
            else:
                current["text"] += "\n" + text
                current["locator"]["paragraph_indexes"].append(index)
            continue
        if current is not None and not _BREAK.match(text) and tag not in {"h1", "h2", "h3", "h4"}:
            current["text"] += "\n" + text
            current["locator"]["paragraph_indexes"].append(index)
            continue
        current = None
        sections.append({"text": text, "locator": {"kind": "xhtml-paragraph", "index_in_headings_and_paragraphs": index,
                                                   "tag": tag, "id": ident,
                                                   "precision": "element selection; not byte offsets"}})
    return sections


TEXT_PARSERS = {"xhtml-paragraphs": parse_xhtml_paragraphs, "eu-control-list-annex": parse_control_list_annex}


class CellarLegalAdapter(_LegalAdapter):
    connector = "cellar"

    def _fetch_texts(self, records: list[dict[str, Any]], text: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Fetch and split the text of selected items (same host only); a missing item stays metadata-only."""
        fetched = []
        for record in records:
            fields = record["fields"]
            if len(fetched) >= int(text["max_items"]) or fields.get("format") not in text["formats"] \
                    or not fields.get("item"):
                continue
            url = "https://" + str(fields["item"]).split("://", 1)[-1]
            status, raw = self._get(url, headers={"Accept": "application/xhtml+xml, text/html"})
            outcome = {"item": fields["item"], "status": status, "response_sha256": hashlib.sha256(raw).hexdigest(),
                       "bytes": len(raw)}
            fetched.append(outcome)
            if status in {404, 410}:
                fields["text_status"] = outcome["outcome"] = "not_found"
                continue
            if status >= 400:
                raise SourcePackError("schema_drift", f"CELLAR item returned HTTP {status}")
            sections = TEXT_PARSERS[text["parser"]](raw)
            if not sections:
                raise SourcePackError("schema_drift", "CELLAR item has no extractable text")
            record["sections"] = sections
            record["native"]["original_sha256"] = outcome["response_sha256"]
            fields["text_parser"] = text["parser"]
            fields["text_status"] = outcome["outcome"] = "captured"
        return fetched

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            cellar_query,
            parse_cellar_results,
        )

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        offset = int(state.get("offset", 0))
        limit = min(int(selection.get("page_size") or 100), int(self.definition["limits"]["max_results"]))
        query = cellar_query(selection["celex"], languages=tuple(selection["languages"]), offset=offset, limit=limit)
        status, raw = self._get(self.source["endpoint"],
                                {"query": query, "format": "application/sparql-results+json"},
                                {"Accept": "application/sparql-results+json"})
        if status >= 400:
            raise SourcePackError("schema_drift", f"CELLAR returned HTTP {status}")
        try:
            payload = json.loads(raw)
            result = parse_cellar_results(payload, celex_ids=selection["celex"],
                                          languages=tuple(selection["languages"]), offset=offset, limit=limit)
        except (ValueError, ProviderError) as exc:
            raise _provider_error(exc) if isinstance(exc, ProviderError) else SourcePackError(
                "schema_drift", "CELLAR returned a non-JSON result") from exc
        info = {"offset": offset, "limit": limit, "query_sha256": _digest(query),
                "response_sha256": hashlib.sha256(raw).hexdigest(), "rows": len(payload["results"]["bindings"]),
                "final_page": result["next_offset"] is None}
        text_bytes = 0
        if selection.get("text"):
            info["texts"] = self._fetch_texts(result["records"], selection["text"])
            text_bytes = sum(int(t.pop("bytes", 0)) for t in info["texts"])
        records = [self._wrap(record, info) for record in result["records"]]
        next_state = None if result["next_offset"] is None else {"offset": result["next_offset"]}
        return self._page(records, next_state, len(raw) + text_bytes, {"status": status, **info})


class RiiDecisionAdapter(_LegalAdapter):
    connector = "rii"
    INDEX_PATH = "/rii-toc.xml"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            parse_court_download,
            parse_court_index,
        )

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        base = self.source["endpoint"].rstrip("/")
        if "queue" not in state:
            queue = list(selection.get("decisions") or [])
            index_info = None
            if selection.get("index"):
                window = dict(selection["index"])
                status, raw = self._get(base + self.INDEX_PATH)
                if status >= 400:
                    raise SourcePackError("schema_drift", f"court index returned HTTP {status}")
                try:
                    parsed = parse_court_index(raw, offset=0, limit=int(window["limit"]), court=window.get("court"),
                                               since=window.get("since"),
                                               max_index_bytes=int(self.definition["limits"]["max_bytes"]))
                except ProviderError as exc:
                    raise _provider_error(exc) from exc
                for row in parsed["records"]:
                    identity = re.search(r"jb-([A-Za-z0-9_-]+)\.zip$", row["url"]).group(1)
                    if identity not in queue:
                        queue.append(identity)
                index_info = {"index_sha256": parsed["index_sha256"], "matched": parsed["total_selected"],
                              "selected": len(parsed["records"])}
                queue = queue[:MAX_SELECTION]
                return self._page([], {"queue": queue, "i": 0} if queue else None, len(raw),
                                  {"status": status, "index": index_info, "queue_size": len(queue)})
            state = {"queue": queue[:MAX_SELECTION], "i": 0}
        queue, index = list(state["queue"]), int(state["i"])
        if index >= len(queue):
            return self._page([], None, 0, {"status": 200, "queue_size": len(queue)})
        identity = queue[index]
        status, raw = self._get(f"{base}/jportal/docs/bsjrs/jb-{identity}.zip")
        info = {"decision": identity, "queue_index": index, "queue_size": len(queue),
                "response_sha256": hashlib.sha256(raw).hexdigest(), "final_page": index + 1 >= len(queue)}
        records, outcome = [], "returned"
        if status in {404, 410}:
            outcome = "not_found"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"court download returned HTTP {status}")
        else:
            try:
                record = parse_court_download(raw)
            except (ProviderError, zipfile.BadZipFile) as exc:
                raise (_provider_error(exc) if isinstance(exc, ProviderError)
                       else SourcePackError("schema_drift", "court download is not a valid archive")) from exc
            if record["provider_id"] != identity:
                raise SourcePackError("schema_drift", "court response returned another decision")
            records = [self._wrap(record, info)]
        next_state = None if index + 1 >= len(queue) else {"queue": queue, "i": index + 1}
        return self._page(records, next_state, len(raw), {"status": status, "outcome": outcome, **info})


class BerlinLegalAdapter(_LegalAdapter):
    connector = "berlin-law"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            parse_berlin_juris_html,
            parse_berlin_juris_xml,
        )

        self._check(request)
        state = self._cursor(cursor)
        items = list(self.legal["selection"])
        index = int(state.get("i", 0))
        if index >= len(items):
            return self._page([], None, 0, {"status": 200})
        item = dict(items[index])
        status, raw = self._get(item["url"])
        info = {"official_id": item["official_id"], "format": item["format"], "selection_index": index,
                "selection_size": len(items), "response_sha256": hashlib.sha256(raw).hexdigest(),
                "final_page": index + 1 >= len(items)}
        records, outcome = [], "returned"
        if status in {404, 410}:
            outcome = "not_found"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"Berlin portal returned HTTP {status}")
        else:
            try:
                if item["format"] == "juris-xml-zip":
                    xml = _single_xml_member(raw)
                    parsed = parse_berlin_juris_xml(xml, source_url=item["url"], historical=item.get("historical"))
                else:
                    parsed = parse_berlin_juris_html(raw, source_url=item["url"], official_id=item["official_id"])
            except ProviderError as exc:
                raise _provider_error(exc) from exc
            for record in parsed:
                if record["provider_id"] != item["official_id"]:
                    raise SourcePackError("schema_drift", "Berlin publication returned another official ID")
            records = [self._wrap(record, info) for record in parsed]
        next_state = None if index + 1 >= len(items) else {"i": index + 1}
        return self._page(records, next_state, len(raw), {"status": status, "outcome": outcome, **info})


def _single_xml_member(raw: bytes) -> bytes:
    if not raw.startswith(b"PK"):
        return raw
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1 or not members[0].filename.endswith(".xml") or members[0].file_size > 20_000_000 \
                or ".." in members[0].filename or members[0].filename.startswith("/"):
            raise SourcePackError("schema_drift", "Berlin download must contain exactly one XML member")
        with archive.open(members[0]) as stream:
            return stream.read(20_000_001)


def _format_error(exc: Exception) -> SourcePackError:
    code = getattr(exc, "code", "schema_drift")
    mapped = {"input_limit": "response_too_large", "unsupported_archive": "schema_drift"}.get(code, "schema_drift")
    return SourcePackError(mapped, f"{code}: {exc}")


class _FederalAdapter(_LegalAdapter):
    """Shared paging for federal sources: one queued unit (statute or promulgation) per page."""

    def _same_host(self, url: str) -> str:
        """An absolute https URL on the declared host (relative paths and http links on that host are upgraded)."""
        parts = urlsplit(url)
        base = urlsplit(self.source["endpoint"])
        if not parts.netloc:
            return f"{base.scheme}://{base.netloc}{parts.path}" + (f"?{parts.query}" if parts.query else "")
        if (parts.hostname or "").casefold() != (base.hostname or "").casefold():
            raise SourcePackError("network_policy", "federal sources fetch only from their declared host")
        return f"https://{parts.netloc}{parts.path}" + (f"?{parts.query}" if parts.query else "")

    def _statutes(self) -> list[dict[str, Any]]:
        return _statute_selection(self.legal["selection"])


class GiiStatuteAdapter(_FederalAdapter):
    connector = "gesetze-im-internet"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.federal_law_formats import FormatError, parse_gii_statute, parse_gii_toc

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        statutes = self._statutes()
        if "queue" not in state:
            status, raw = self._get(self._same_host(selection["toc_path"]))
            if status >= 400:
                raise SourcePackError("schema_drift", f"gii table of contents returned HTTP {status}")
            try:
                toc = parse_gii_toc(raw)
            except FormatError as exc:
                raise _format_error(exc) from exc
            by_path = {item["path"]: item for item in toc}
            queue = []
            for statute in statutes:
                item = by_path.get(str(statute["gii_path"]).casefold())
                if item is not None:
                    self._same_host(item["link"])  # a link to another host is refused, never followed
                queue.append({"jurabk": statute["jurabk"], "link": item["link"] if item else None})
            info = {"toc_sha256": hashlib.sha256(raw).hexdigest(), "toc_items": len(toc),
                    "selected": len(queue), "in_toc": sum(1 for q in queue if q["link"])}
            return self._page([], {"queue": queue, "i": 0}, len(raw), {"status": status, "toc": info})
        queue, index = list(state["queue"]), int(state["i"])
        if index >= len(queue):
            return self._page([], None, 0, {"status": 200})
        entry = queue[index]
        info = {"official_id": entry["jurabk"], "selection_index": index, "selection_size": len(queue),
                "final_page": index + 1 >= len(queue)}
        next_state = None if index + 1 >= len(queue) else {"queue": queue, "i": index + 1}
        if not entry["link"]:
            return self._page([], next_state, 0, {"status": 404, "outcome": "not_found", "reason": "not_in_toc",
                                                  **info})
        url = self._same_host(entry["link"])
        status, raw = self._get(url)
        info["response_sha256"] = hashlib.sha256(raw).hexdigest()
        if status in {404, 410}:
            return self._page([], next_state, len(raw), {"status": status, "outcome": "not_found", **info})
        if status >= 400:
            raise SourcePackError("schema_drift", f"gii statute download returned HTTP {status}")
        try:
            record = parse_gii_statute(raw, source_url=url, jurabk=entry["jurabk"])
        except (FormatError, zipfile.BadZipFile) as exc:
            raise (_format_error(exc) if isinstance(exc, FormatError)
                   else SourcePackError("schema_drift", "statute download is not a valid archive")) from exc
        return self._page([self._wrap(record, info)], next_state, len(raw), {"status": status, "outcome": "returned",
                                                                              **info})


class RisStatuteAdapter(_FederalAdapter):
    connector = "rechtsinformationen-bund"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.federal_law_formats import FormatError, parse_legaldocml_statute, parse_ris_search

        self._check(request)
        state = self._cursor(cursor)
        statutes = self._statutes()
        index = int(state.get("i", 0))
        if index >= len(statutes):
            return self._page([], None, 0, {"status": 200})
        statute = statutes[index]
        limit = int(self.legal["selection"]["max_expressions"])
        status, raw = self._get(self.source["endpoint"], {"searchTerm": statute["jurabk"], "size": limit},
                                {"Accept": "application/ld+json, application/json"})
        info = {"official_id": statute["jurabk"], "selection_index": index, "selection_size": len(statutes),
                "search_sha256": hashlib.sha256(raw).hexdigest(), "final_page": index + 1 >= len(statutes)}
        next_state = None if index + 1 >= len(statutes) else {"i": index + 1}
        if status >= 400:
            raise SourcePackError("schema_drift", f"legal information portal search returned HTTP {status}")
        try:
            expressions = parse_ris_search(raw, jurabk=statute["jurabk"], eli_work=statute.get("eli_work"))
        except FormatError as exc:
            raise _format_error(exc) from exc
        records, total, fetched = [], len(raw), []
        for expression in expressions[:limit]:
            url = self._same_host(expression["xml_url"])
            doc_status, doc = self._get(url, headers={"Accept": "application/xml"})
            total += len(doc)
            fetched.append({"eli": expression["eli_expression"], "status": doc_status,
                            "response_sha256": hashlib.sha256(doc).hexdigest()})
            if doc_status in {404, 410}:
                continue
            if doc_status >= 400:
                raise SourcePackError("schema_drift", f"LegalDocML expression returned HTTP {doc_status}")
            try:
                record = parse_legaldocml_statute(doc, expression=expression, jurabk=statute["jurabk"],
                                                  source_url=url)
            except FormatError as exc:
                raise _format_error(exc) from exc
            records.append(self._wrap(record, {**info, "response_sha256": fetched[-1]["response_sha256"]}))
        info.update({"expressions_listed": len(expressions), "expressions": fetched,
                     "truncated": len(expressions) > limit})
        return self._page(records, next_state, total, {"status": status,
                                                       "outcome": "returned" if records else "not_found", **info})


class BgblActAdapter(_FederalAdapter):
    connector = "recht-bund"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.federal_law_formats import (
            DIGITAL_BGBL_FROM_YEAR,
            FormatError,
            parse_bgbl_act,
            parse_bgbl_feed,
        )
        from src.kb.legal_citations import bgbl_key

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        if "queue" not in state:
            queue = [{"part": int(i["part"]), "year": int(i["year"]), "number": int(i["number"])}
                     for i in selection.get("items") or []]
            info: dict[str, Any] = {}
            raw = b""
            if selection.get("index"):
                window = dict(selection["index"])
                status, raw = self._get(self._same_host(window["path"]))
                if status >= 400:
                    raise SourcePackError("schema_drift", f"promulgation listing returned HTTP {status}")
                try:
                    listed = parse_bgbl_feed(raw)
                except FormatError as exc:
                    raise _format_error(exc) from exc
                historical = [e["key"] for e in listed if e["year"] < DIGITAL_BGBL_FROM_YEAR]
                for entry in listed[:int(window["limit"])]:
                    item = {"part": entry["part"], "year": entry["year"], "number": entry["number"]}
                    if entry["year"] >= DIGITAL_BGBL_FROM_YEAR and item not in queue:
                        queue.append(item)
                info = {"listing_sha256": hashlib.sha256(raw).hexdigest(), "listed": len(listed),
                        "link_only_historical": historical}
            queue = queue[:MAX_SELECTION]
            if raw:
                return self._page([], {"queue": queue, "i": 0} if queue else None, len(raw),
                                  {"status": 200, "index": info, "queue_size": len(queue)})
            state = {"queue": queue, "i": 0}
        queue, index = list(state["queue"]), int(state["i"])
        if index >= len(queue):
            return self._page([], None, 0, {"status": 200, "queue_size": len(queue)})
        item = queue[index]
        key = bgbl_key(item["part"], item["year"], number=item["number"])
        url = self._same_host(selection["document_path"].format(**item))
        status, raw = self._get(url, headers={"Accept": "application/xml"})
        info = {"official_id": key, "queue_index": index, "queue_size": len(queue),
                "response_sha256": hashlib.sha256(raw).hexdigest(), "final_page": index + 1 >= len(queue)}
        next_state = None if index + 1 >= len(queue) else {"queue": queue, "i": index + 1}
        if status in {404, 410}:
            return self._page([], next_state, len(raw), {"status": status, "outcome": "not_found", **info})
        if status >= 400:
            raise SourcePackError("schema_drift", f"promulgation returned HTTP {status}")
        try:
            record = parse_bgbl_act(raw, key=key, statutes=self._statutes(), source_url=url)
        except FormatError as exc:
            raise _format_error(exc) from exc
        if not record["fields"]["touched_statutes"]:
            # Seen but not acquired: the act amends no statute of the bounded set.
            return self._page([], next_state, len(raw), {"status": status, "outcome": "seen_not_acquired",
                                                         "title": record["title"], **info})
        return self._page([self._wrap(record, info)], next_state, len(raw),
                          {"status": status, "outcome": "returned", **info})


FIXTURE_SECRET = None
ADAPTERS = {"cellar": CellarLegalAdapter, "rii": RiiDecisionAdapter, "berlin-law": BerlinLegalAdapter,
            "gesetze-im-internet": GiiStatuteAdapter, "rechtsinformationen-bund": RisStatuteAdapter,
            "recht-bund": BgblActAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay native envelopes keyed by URL path (+ SPARQL offset for CELLAR)."""

    def key_of(url: str, params: Mapping[str, Any]) -> str:
        parts = urlsplit(url)
        offset = re.search(r"OFFSET (\d+)\s*$", str(params.get("query") or ""))
        return parts.path + ("?" + parts.query if parts.query else "") + (f"#offset={offset.group(1)}" if offset else "")

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        page = by_key.get(key_of(url, params))
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key_of(url, params)}")
        body = page.get("body")
        if page.get("body_encoding") == "base64":
            import base64

            content = base64.b64decode(body)
        elif isinstance(body, str):
            content = body.encode()
        elif body is None:
            content = b""
        else:
            content = json.dumps(body, ensure_ascii=False).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])))
    operation = min(source["operations"])
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": operation, "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
