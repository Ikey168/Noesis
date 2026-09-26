"""Bounded EPO Open Patent Services (OPS) acquisition for patent publications.

One provider path only: EPO OPS v3.2 REST (XML), authenticated with OAuth
client credentials (``consumer key:secret`` from a secret reference). Espacenet
is the same EPO data behind a UI and WIPO PATENTSCOPE's machine interface is a
paid service, so neither is implemented (see ``PROVIDER_CONTRACTS``).

A source selects up to 25 publications in docdb form (``EP.1000000.A1``) and
the parts to read (``biblio``, ``family``, ``legal``, ``claims``). Each runtime
page reads one (publication, part), so budgets, retries and checkpoints apply.
A part the provider does not hold (OPS 404) is a per-part outcome, not a
failure; missing legal status stays explicitly missing. Nothing here infers
validity, enforceability or freedom to operate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-patent-part-v1"
PARTS = ("biblio", "family", "legal", "claims")
MAX_PUBLICATIONS = 25
NS = {"ops": "http://ops.epo.org", "ex": "http://www.epo.org/exchange", "ft": "http://www.epo.org/fulltext"}
PROVIDER_CONTRACTS = {
    "epo-ops": {
        "documentation": "https://www.epo.org/en/searching-for-patents/data/web-services/ops",
        "access": "OPS v3.2 REST: published-data biblio/claims, INPADOC family, legal events (XML)",
        "authentication": "OAuth2 client credentials (consumer key and secret from an EPO developer account)",
        "volume_terms": "fair-use weekly quota; throttling signalled by HTTP 403 with X-Throttling-Control",
        "identifiers": ["docdb publication number (country.number.kind)", "application number", "INPADOC family ID"],
        "status": "unverified-live",
    },
    "espacenet": {"status": "not-implemented", "reason": "same EPO data as OPS behind a search UI; no second route"},
    "wipo-patentscope": {"status": "not-implemented",
                         "reason": "PATENTSCOPE web services are a paid subscription; no permitted free stable "
                                   "machine interface was established for the required records"},
}
_DOCDB = re.compile(r"^[A-Z]{2}\.[0-9A-Z]{1,15}\.[A-Z][0-9]?$")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _date(value: Any) -> str | None:
    text = str(value or "").strip()
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}" if re.fullmatch(r"\d{8}", text) else None


def _text(node) -> str:
    return " ".join("".join(node.itertext()).split()) if node is not None else ""


def _doc_id(node) -> dict[str, Any] | None:
    if node is None:
        return None
    doc = node.find("ex:document-id[@document-id-type='docdb']", NS)
    if doc is None:  # elements are falsy without children; never use `or` here
        doc = node.find("ex:document-id", NS)
    if doc is None:
        return None
    values = {tag: _text(doc.find(f"ex:{tag}", NS)) or None for tag in ("country", "doc-number", "kind", "date")}
    number = ".".join(v for v in (values["country"], values["doc-number"], values["kind"]) if v)
    return {"docdb": number or None, "country": values["country"], "number": values["doc-number"],
            "kind": values["kind"], "date": _date(values["date"]), "format": doc.get("document-id-type")}


def patent_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    patent = dict(source.get("patent") or {})
    publications = list(patent.get("publications") or [])
    parts = list(patent.get("parts") or PARTS)
    if not 1 <= len(publications) <= MAX_PUBLICATIONS or any(not _DOCDB.match(p) for p in publications):
        raise SourcePackError("unbounded_source", f"patent sources select 1-{MAX_PUBLICATIONS} docdb publications")
    if not parts or set(parts) - set(PARTS):
        raise SourcePackError("invalid_mapping", f"patent parts are drawn from {PARTS}")
    return {**patent, "publications": publications, "parts": parts}


def parse_biblio(root) -> dict[str, Any]:
    document = root.find(".//ex:exchange-document", NS)
    if document is None:
        raise SourcePackError("schema_drift", "OPS biblio lacks an exchange-document")
    biblio = document.find("ex:bibliographic-data", NS)
    publication = _doc_id(biblio.find("ex:publication-reference", NS)) if biblio is not None else None
    if not publication or not publication["docdb"]:
        raise SourcePackError("schema_drift", "OPS biblio lacks a docdb publication reference")

    def parties(kind):
        names = []
        for node in biblio.findall(f"ex:parties/ex:{kind}s/ex:{kind}", NS):
            names.append({"sequence": node.get("sequence"), "format": node.get("data-format"),
                          "name": _text(node.find(f"ex:{kind}-name/ex:name", NS))})
        return names

    citations = []
    for index, citation in enumerate(biblio.findall("ex:references-cited/ex:citation", NS)):
        patcit, nplcit = citation.find("ex:patcit", NS), citation.find("ex:nplcit", NS)
        if patcit is not None:
            citations.append({"kind": "patent", "target": _doc_id(patcit), "cited_phase": citation.get("cited-phase"),
                              "locator": f"references-cited/citation[{index + 1}]"})
        elif nplcit is not None:
            text = _text(nplcit.find("ex:text", NS)) or _text(nplcit)
            doi = re.search(r"\b10\.\d{4,9}/\S+\b", text)
            citations.append({"kind": "non-patent", "text": text, "doi": doi.group(0).rstrip(".,;") if doi else None,
                              "cited_phase": citation.get("cited-phase"),
                              "locator": f"references-cited/citation[{index + 1}]"})
    return {
        "publication": publication,
        "family_id": document.get("family-id"),
        "application": _doc_id(biblio.find("ex:application-reference", NS)),
        "priorities": [p for p in (_doc_id(node) for node in biblio.findall("ex:priority-claims/ex:priority-claim", NS))
                       if p],
        "titles": [{"language": node.get("lang"), "value": _text(node)}
                   for node in biblio.findall("ex:invention-title", NS)],
        "abstracts": [{"language": node.get("lang"), "value": _text(node)}
                      for node in document.findall("ex:abstract", NS)],
        "applicants": parties("applicant"),
        "inventors": parties("inventor"),
        "classifications": sorted({_text(node) for node in biblio.findall(
            "ex:classifications-ipcr/ex:classification-ipcr/ex:text", NS) if _text(node)}),
        "citations": citations,
    }


def parse_family(root) -> dict[str, Any]:
    members = []
    for node in root.findall(".//ops:family-member", NS):
        publication = _doc_id(node.find("ex:publication-reference", NS))
        if publication and publication["docdb"]:
            members.append({"publication": publication, "family_id": node.get("family-id"),
                            "application": _doc_id(node.find("ex:application-reference", NS))})
    family = root.find(".//ops:patent-family", NS)
    if family is None:
        raise SourcePackError("schema_drift", "OPS family lacks ops:patent-family")
    return {"members": members, "total": int(family.get("total-result-count") or len(members))}


def parse_legal(root) -> dict[str, Any]:
    events = []
    for index, node in enumerate(root.findall(".//ops:legal", NS)):
        dates = [_date(v) for v in re.findall(r"\b(\d{8})\b", _text(node))]
        events.append({"code": node.get("code"), "description": node.get("desc"), "influence": node.get("infl"),
                       "date": next((d for d in dates if d), None), "text": _text(node.find("ops:pre", NS)) or None,
                       "locator": f"ops:legal[{index + 1}]"})
    return {"events": events}


def parse_claims(root) -> dict[str, Any]:
    claims = []
    for block in root.findall(".//ex:claims", NS) + root.findall(".//ft:claims", NS):
        language = block.get("lang")
        for index, claim in enumerate(block.findall("ex:claim", NS) + block.findall("ft:claim", NS)):
            text = _text(claim)
            if text:
                claims.append({"language": language, "number": index + 1, "text": text})
    return {"claims": claims}


PARSERS = {"biblio": parse_biblio, "family": parse_family, "legal": parse_legal, "claims": parse_claims}
PATHS = {
    "biblio": "/rest-services/published-data/publication/docdb/{pub}/biblio",
    "family": "/rest-services/family/publication/docdb/{pub}",
    "legal": "/rest-services/legal/publication/docdb/{pub}",
    "claims": "/rest-services/published-data/publication/docdb/{pub}/claims",
}


def _default_transport(max_bytes: int) -> Callable[..., Mapping[str, Any]]:
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter

    def transport(*, url, params, headers, timeout, method="GET", body=None):
        if method == "GET":
            return HTTPSPageAdapter._request(url=url, params=params, headers=headers, timeout=timeout,
                                             max_bytes=max_bytes)
        import urllib.error
        import urllib.request

        from src.ingestion.source_packs import _validate_endpoint

        _validate_endpoint(url, "epo-ops-token")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return {"status": response.status, "headers": dict(response.headers),
                        "content": response.read(max_bytes + 1)}
        except urllib.error.HTTPError as exc:
            try:
                return {"status": exc.code, "headers": dict(exc.headers or {}), "content": b""}
            finally:
                exc.close()
        except urllib.error.URLError as exc:
            raise SourcePackError("source_unavailable", "OPS token endpoint is unavailable") from exc

    return transport


class EpoOpsAdapter:
    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        self.source = json.loads(json.dumps(source))
        self.patent = patent_declaration(self.source)
        self.transport = transport or _default_transport(int(source["budgets"]["max_bytes"]))
        self.secret = secret
        self.token: str | None = None
        self.work = [(pub, part) for pub in self.patent["publications"] for part in self.patent["parts"]]
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "patent": {"publications": len(self.patent["publications"]), "parts": self.patent["parts"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "work": self.work})

    def _authenticate(self) -> str:
        if self.token:
            return self.token
        if not self.secret or ":" not in self.secret:
            raise SourcePackError("authentication_failed", "OPS needs a 'key:secret' consumer credential")
        base = self.source["endpoint"].rstrip("/")
        response = self.transport(
            url=base + "/auth/accesstoken", params={}, method="POST", body=b"grant_type=client_credentials",
            headers={"Authorization": "Basic " + base64.b64encode(self.secret.encode()).decode(),
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        try:
            token = json.loads(content).get("access_token") if status == 200 else None
        except (ValueError, AttributeError):
            token = None
        if not token:
            raise SourcePackError("authentication_failed", f"OPS token request failed (HTTP {status})")
        self.token = str(token)
        return self.token

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"} or dict(
                request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "patent runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "patent cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.work):
            return RuntimePage((), None, 0, receipt={"status": 200})
        publication, part = self.work[index]
        url = self.source["endpoint"].rstrip("/") + PATHS[part].format(pub=publication)
        if urlsplit(url).hostname != urlsplit(self.source["endpoint"]).hostname:
            raise SourcePackError("network_policy", "OPS requests stay on the declared host")
        response = self.transport(url=url, params={}, headers={"Authorization": f"Bearer {self._authenticate()}",
                                                               "Accept": "application/xml"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        throttle = str(headers.get("x-throttling-control") or "")
        if status == 429 or (status == 403 and ("overloaded" in throttle or "black" in throttle
                                                 or "quota" in raw.decode(errors="ignore").casefold())):
            raise SourcePackError("rate_limited", "OPS throttled the request",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            self.token = None
            raise SourcePackError("authentication_failed", f"OPS refused access (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"OPS returned HTTP {status}")
        info = {"publication": publication, "part": part, "response_sha256": hashlib.sha256(raw).hexdigest(),
                "work_index": index, "work_size": len(self.work)}
        records, outcome = [], "returned"
        if status == 404:
            outcome = "not_available"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"OPS returned HTTP {status}")
        else:
            # Optional-sources dependency: imported where XML is parsed so that
            # installs without it can still load the connector registry.
            from defusedxml import ElementTree as ET

            try:
                root = ET.fromstring(raw, forbid_dtd=True, forbid_entities=True, forbid_external=True)
            except ET.ParseError as exc:
                raise SourcePackError("schema_drift", "OPS returned malformed XML") from exc
            parsed = PARSERS[part](root)
            record = {"contract": RECORD_CONTRACT, "provider": "epo-ops", "publication": publication, "part": part,
                      "data": parsed, "raw_sha256": hashlib.sha256(raw).hexdigest(),
                      "fetched_from": url}
            title = next((t["value"] for t in parsed.get("titles") or [] if t["language"] == "en"), None) \
                or f"{publication} {part}"
            records = [{"id": f"epo:{publication}:{part}", "title": title, "language": "en",
                        "url": f"https://worldwide.espacenet.com/patent/search?q=pn%3D{publication.replace('.', '')}",
                        "published_at": dict(parsed.get("publication") or {}).get("date"),
                        "patent_record": record}]
        more = index + 1 < len(self.work)
        next_cursor = json.dumps({"i": index + 1, "scope": self._scope()}, sort_keys=True) if more else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt={"status": status, "outcome": outcome,
                                                                         **info})


FIXTURE_SECRET = "fixture-key:fixture-secret"
ADAPTERS = {"epo-ops": EpoOpsAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    by_path = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, method="GET", body=None):
        del params, timeout, body
        path = urlsplit(url).path
        if method == "POST":
            if not headers.get("Authorization", "").startswith("Basic "):
                return {"status": 401, "headers": {}, "content": b""}
            return {"status": 200, "headers": {}, "content": json.dumps({"access_token": "fixture-token"}).encode()}
        if headers.get("Authorization") != "Bearer fixture-token":
            return {"status": 401, "headers": {}, "content": b""}
        page = by_path.get(path)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {path}")
        body_ = page.get("body")
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": (body_ or "").encode()}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = EpoOpsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                            secret="fixture-key:fixture-secret")
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
