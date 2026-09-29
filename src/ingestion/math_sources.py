"""Bounded mathematics acquisition for Research: zbMATH Open, OEIS and formal-library snapshots.

Three native connectors, each over a pinned selection (never a crawl):

* ``zbmath`` reads zbMATH Open API v1 documents by id and keeps provider ids,
  authors, MSC classifications, identifiers (Zbl, DOI, arXiv) and references.
* ``oeis`` reads OEIS entries by A-number (``fmt=json``) and keeps the terms,
  name, references, links, keywords and the entry revision/time.
* ``formal-library`` reads selected files of one library at one immutable
  commit (40-hex) from a raw-content host and scans declarations lexically
  (Lean 4, Isabelle, Coq): names, kinds, statement spans and module imports.
  The scan is not elaboration: declaration-level dependencies found from
  statement tokens are labelled as lexical, and module imports are explicit.

Records never collapse across providers; linking happens later, in the store,
from explicit identifiers or as reviewable candidates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-math-record-v1"
SCAN_METHOD = "lexical-declaration-scan:1.0.0"
FIXTURE_SECRET = None
MAX_ITEMS = 50
MAX_TERMS = 200
COMMIT = re.compile(r"^[0-9a-f]{40}$")
A_NUMBER = re.compile(r"^A\d{6}$")
ZBMATH_ID = re.compile(r"^\d{1,12}$")
SYSTEMS = ("lean", "isabelle", "coq")
PROVIDER_CONTRACTS = {
    "zbmath-open": {
        "documentation": "https://api.zbmath.org/", "access": "zbMATH Open REST API v1, /document/{id} (JSON)",
        "authentication": "none", "terms": "zbMATH Open data under CC BY-SA 4.0 (reviews may carry other terms; "
                                          "the connector keeps metadata, classifications and references only)",
        "identifiers": ["zbMATH document id", "Zbl number", "DOI", "arXiv id"], "status": "unverified-live",
        "rate_limits": "not documented numerically; the bounded profile reads at most 50 pinned documents"},
    "oeis": {
        "documentation": "https://oeis.org/wiki/JSON_Format,_Compressed_Files", "access": "search?q=id:A......&fmt=json",
        "authentication": "none", "terms": "OEIS End-User License Agreement (CC BY-SA 4.0 for entries)",
        "identifiers": ["A-number"], "update": "entries carry a revision number and last-modified time",
        "status": "unverified-live"},
    "formal-library": {
        "documentation": "raw.githubusercontent.com/{repository}/{commit}/{path}",
        "access": "selected files at a pinned 40-hex commit; no repository mirroring",
        "authentication": "none", "libraries": {
            "mathlib4": {"system": "lean", "license": "Apache-2.0"},
            "afp": {"system": "isabelle", "license": "per entry (BSD or LGPL); recorded per source"},
            "coq": {"system": "coq", "license": "LGPL-2.1 (stdlib); parser supported, no pack source yet"}},
        "status": "validated-live-bounded",
        "note": "GitHub raw content was reachable from the build environment; see "
                "docs/development/mathematics-evidence/"},
    "arxiv-crossref-openalex": {"status": "reused", "reason": "existing research-discovery sources and scholarly "
                                                               "adapters cover these; mathematics adds no second route"},
    "mathscinet": {"status": "not-implemented", "reason": "licensed access only; optional and not a v1 dependency"},
}


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


# ----------------------------------------------------------------- scanning

_LEAN_DECL = re.compile(
    r"^(?:@\[[^\]]*\]\s*)*(?:(?:private|protected|noncomputable|partial|nonrec|unsafe|public)\s+)*"
    r"(theorem|lemma|def|abbrev|structure|class|instance|inductive|axiom|opaque)\b\s*([^\s:({\[]*)")
_ISA_DECL = re.compile(r"^\s*(theorem|lemma|corollary|proposition|definition|fun|function|primrec|abbreviation|"
                       r"inductive)\s+([A-Za-z_][\w']*)")
_COQ_DECL = re.compile(r"^\s*(Theorem|Lemma|Corollary|Proposition|Definition|Fixpoint|Inductive|Record)\s+"
                       r"([A-Za-z_][\w']*)")
_ISA_STOP = re.compile(r"^\s*(by|proof|using|apply|unfolding|sorry|oops|done|including)\b")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _lean(lines: list[str], module: str) -> tuple[list[str], list[dict[str, Any]]]:
    imports, decls, scopes = [], [], []
    for index, line in enumerate(lines):
        stripped = line.strip()
        found = re.match(r"^(?:public\s+|meta\s+)*import\s+(\S+)", stripped)
        if found:
            imports.append(found.group(1))
            continue
        scope = re.match(r"^(namespace|section)\b\s*(\S*)", stripped)
        if scope:
            scopes.append((scope.group(1), scope.group(2)))
            continue
        if re.match(r"^end\b", stripped) and scopes:
            scopes.pop()
            continue
        decl = _LEAN_DECL.match(stripped)
        if not decl:
            continue
        kind, name = decl.group(1), decl.group(2)
        if not name or kind == "instance" and not name:
            name = f"instance@{index + 1}"
        prefix = [n for k, n in scopes if k == "namespace" and n]
        full = name[len("_root_."):] if name.startswith("_root_.") else ".".join([*prefix, name])
        header, end = [], index
        for offset in range(index, min(len(lines), index + 40)):
            text = lines[offset]
            cut = min((pos for pos in (text.find(":="), text.find(" where")) if pos >= 0), default=-1)
            header.append(text if cut < 0 else text[:cut])
            end = offset
            if cut >= 0 or (offset > index and text.lstrip().startswith("|")):
                if offset > index and text.lstrip().startswith("|"):
                    header.pop()
                    end = offset - 1
                break
        doc = _lean_doc(lines, index)
        decls.append({"name": full, "kind": kind, "line_start": index + 1, "line_end": end + 1,
                      "statement": "\n".join(header).strip(), "doc": doc, "module": module})
    return imports, decls


def _lean_doc(lines: list[str], index: int) -> str | None:
    cursor = index - 1
    while cursor >= 0 and re.match(r"^\s*@\[", lines[cursor]):
        cursor -= 1
    if cursor < 0 or not lines[cursor].rstrip().endswith("-/"):
        return None
    start = cursor
    while start >= 0 and "/--" not in lines[start]:
        start -= 1
    if start < 0:
        return None
    return " ".join(line.strip() for line in lines[start:cursor + 1]).removeprefix("/--").removesuffix("-/").strip()[:500]


def _isabelle(lines: list[str], module: str) -> tuple[list[str], list[dict[str, Any]]]:
    text = "\n".join(lines)
    header = re.search(r"\btheory\s+(\S+)\s+imports\s+(.*?)\bbegin\b", text, re.DOTALL)
    theory = header.group(1) if header else module
    imports = [item.strip('"') for item in header.group(2).split()] if header else []
    decls = []
    for index, line in enumerate(lines):
        decl = _ISA_DECL.match(line)
        if not decl:
            continue
        body, end = [], index
        for offset in range(index, min(len(lines), index + 40)):
            if offset > index and (_ISA_STOP.match(lines[offset]) or _ISA_DECL.match(lines[offset])
                                   or not lines[offset].strip()):
                break
            body.append(lines[offset])
            end = offset
            if lines[offset].rstrip().endswith(" where") or re.search(r'"\s*(by|using)\b', lines[offset]):
                break
        joined = "\n".join(body)
        statement = "\n".join(_QUOTED.findall(joined)) or joined.strip()
        decls.append({"name": f"{theory}.{decl.group(2)}", "kind": decl.group(1), "line_start": index + 1,
                      "line_end": end + 1, "statement": statement, "doc": None, "module": theory})
    return imports, decls


def _coq(lines: list[str], module: str) -> tuple[list[str], list[dict[str, Any]]]:
    imports, decls = [], []
    for index, line in enumerate(lines):
        found = re.match(r"^\s*(?:From\s+\S+\s+)?Require\s+(?:Import\s+|Export\s+)?(.+?)\.\s*$", line)
        if found:
            imports.extend(found.group(1).split())
            continue
        decl = _COQ_DECL.match(line)
        if not decl:
            continue
        body, end = [], index
        for offset in range(index, min(len(lines), index + 40)):
            body.append(lines[offset])
            end = offset
            if re.search(r"\.\s*$", lines[offset]) or ":=" in lines[offset]:
                break
        statement = "\n".join(body).split(":=")[0].strip().rstrip(".")
        decls.append({"name": f"{module}.{decl.group(2)}", "kind": decl.group(1).lower(), "line_start": index + 1,
                      "line_end": end + 1, "statement": statement, "doc": None, "module": module})
    return imports, decls


SCANNERS = {"lean": _lean, "isabelle": _isabelle, "coq": _coq}


def module_name(system: str, path: str) -> str:
    stem = path.rsplit(".", 1)[0]
    if system == "lean":
        return stem.replace("/", ".")
    return stem.rsplit("/", 1)[-1]


def scan_file(system: str, path: str, text: str) -> dict[str, Any]:
    if system not in SCANNERS:
        raise SourcePackError("invalid_mapping", f"formal system must be one of {SYSTEMS}")
    lines = text.splitlines()
    imports, decls = SCANNERS[system](lines, module_name(system, path))
    for decl in decls:
        decl["statement_sha256"] = _sha(decl["statement"].encode())
    return {"module": module_name(system, path), "imports": imports, "declarations": decls, "lines": len(lines)}


# ----------------------------------------------------------------- adapters


class _PinnedAdapter:
    """One page per pinned item; the cursor is bound to the selection."""

    accepts_transport = True
    block = ""

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.config = dict(self.source.get(self.block) or {})
        self.work = self._work()
        if not 1 <= len(self.work) <= MAX_ITEMS:
            raise SourcePackError("unbounded_source", f"{self.block} sources pin 1-{MAX_ITEMS} items")
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            self.block: {"items": len(self.work)},
        }

    def _work(self) -> list[str]:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _get(self, url: str) -> tuple[int, bytes]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        if urlsplit(url).hostname != urlsplit(self.source["endpoint"]).hostname:
            raise SourcePackError("network_policy", "requests stay on the declared host")
        response = self.transport(url=url, params={}, headers={"Accept": "application/json"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider rate limit reached",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        return status, raw

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "mathematics runs use the pinned selection")
        scope = _digest({"endpoint": self.source["endpoint"], "work": self.work, "config": self.config})
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != scope:
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.work):
            return RuntimePage((), None, 0, receipt={"status": 200})
        item = self.work[index]
        status, raw, records, outcome = self._page(item)
        more = index + 1 < len(self.work)
        return RuntimePage(tuple(records), json.dumps({"i": index + 1, "scope": scope}, sort_keys=True) if more
                           else None, len(raw), receipt={"status": status, "outcome": outcome, "item": item,
                                                         "response_sha256": _sha(raw), "work_index": index,
                                                         "work_size": len(self.work)})

    def _page(self, item: str) -> tuple[int, bytes, list[dict[str, Any]], str]:
        raise NotImplementedError


def _json(raw: bytes, provider: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise SourcePackError("schema_drift", f"{provider} returned non-JSON content") from exc


def _identifiers_from_links(links: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for link in links:
        kind = str(link.get("type") or "").casefold()
        value = str(link.get("identifier") or link.get("url") or "").strip()
        if kind in {"doi", "arxiv"} and value:
            found.setdefault(kind, []).append(value.removeprefix("https://doi.org/").casefold()
                                              if kind == "doi" else value.removeprefix("arXiv:"))
    return found


class ZbmathAdapter(_PinnedAdapter):
    block = "zbmath"

    def _work(self) -> list[str]:
        ids = [str(v) for v in self.config.get("document_ids") or []]
        if any(not ZBMATH_ID.match(v) for v in ids):
            raise SourcePackError("invalid_mapping", "zbMATH document ids are numeric")
        return ids

    def _page(self, item):
        status, raw = self._get(f"{self.source['endpoint'].rstrip('/')}/document/{item}")
        if status == 404:
            return status, raw, [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"zbMATH returned HTTP {status}")
        try:
            result = dict(_json(raw, "zbMATH")["result"])
            if str(result["id"]) != item:
                raise SourcePackError("schema_drift", "zbMATH returned another document")
        except (KeyError, TypeError) as exc:
            raise SourcePackError("schema_drift", "zbMATH response lacks result.id") from exc
        title = dict(result.get("title") or {}).get("title") if isinstance(result.get("title"), dict) \
            else result.get("title")
        authors = [a.get("name") for a in dict(result.get("contributors") or {}).get("authors") or [] if a.get("name")]
        identifiers = _identifiers_from_links(result.get("links") or [])
        if result.get("identifier"):
            identifiers["zbl"] = [str(result["identifier"])]
        identifiers["zbmath"] = [item]
        references = []
        for ref in result.get("references") or []:
            zb = dict(ref.get("zbmath") or {}).get("document_id")
            references.append({"zbmath": str(zb) if zb else None, "doi": (ref.get("doi") or "").casefold() or None,
                               "text": (ref.get("text") or "")[:500]})
        record = {"contract": RECORD_CONTRACT, "kind": "literature", "provider": "zbmath-open", "provider_id": item,
                  "title": title, "authors": authors, "year": result.get("year"),
                  "msc": sorted({m.get("code") for m in result.get("msc") or [] if m.get("code")}),
                  "identifiers": {k: sorted(set(v)) for k, v in identifiers.items()}, "references": references,
                  "source": dict(result.get("source") or {}).get("source") if isinstance(result.get("source"), dict)
                  else result.get("source"),
                  "revision": result.get("datestamp"), "raw_sha256": _sha(raw)}
        return status, raw, [{"id": f"zbmath:{item}", "title": title or f"zbMATH {item}", "language": "en",
                              "url": f"https://zbmath.org/?q=an:{quote(item)}", "math_record": record}], "returned"


class OeisAdapter(_PinnedAdapter):
    block = "oeis"

    def _work(self) -> list[str]:
        numbers = [str(v) for v in self.config.get("a_numbers") or []]
        if any(not A_NUMBER.match(v) for v in numbers):
            raise SourcePackError("invalid_mapping", "OEIS ids are A-numbers (A000045)")
        return numbers

    def _page(self, item):
        status, raw = self._get(f"{self.source['endpoint'].rstrip('/')}/search?q=id:{item}&fmt=json")
        if status >= 400:
            raise SourcePackError("schema_drift", f"OEIS returned HTTP {status}")
        payload = _json(raw, "OEIS")
        results = payload if isinstance(payload, list) else (dict(payload or {}).get("results") or [])
        entry = next((r for r in results if f"A{int(r.get('number', -1)):06d}" == item), None)
        if entry is None:
            return status, raw, [], "not_found"
        try:
            terms = [t.strip() for t in str(entry["data"]).split(",") if t.strip()]
            if any(not re.match(r"^-?\d+$", t) for t in terms):
                raise ValueError("non-integer term")
        except (KeyError, ValueError) as exc:
            raise SourcePackError("schema_drift", "OEIS entry lacks integer data") from exc
        links = [str(v) for v in entry.get("link") or []]
        dois = sorted({m.casefold() for line in [*links, *(entry.get("reference") or [])]
                       for m in re.findall(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/[^\s\"<>]+)", str(line), re.IGNORECASE)})
        zbl = sorted({m for line in [*links, *(entry.get("reference") or [])]
                      for m in re.findall(r"zbmath\.org/\?q=an:(\d+)", str(line))})
        record = {"contract": RECORD_CONTRACT, "kind": "sequence", "provider": "oeis", "provider_id": item,
                  "name": entry.get("name"), "terms": terms[:MAX_TERMS], "terms_truncated": len(terms) > MAX_TERMS,
                  "references": [str(v) for v in entry.get("reference") or []], "links": links,
                  "cited_identifiers": {"doi": dois, "zbmath": zbl},
                  "keywords": sorted(str(entry.get("keyword") or "").split(",")) if entry.get("keyword") else [],
                  "formula": [str(v) for v in entry.get("formula") or []][:20],
                  "revision": entry.get("revision"), "time": entry.get("time"), "raw_sha256": _sha(raw)}
        return status, raw, [{"id": f"oeis:{item}", "title": f"{item} {entry.get('name') or ''}".strip(),
                              "language": "en", "url": f"https://oeis.org/{item}", "math_record": record}], "returned"


class FormalLibraryAdapter(_PinnedAdapter):
    block = "formal"

    def _work(self) -> list[str]:
        cfg = self.config
        if cfg.get("system") not in SYSTEMS or not COMMIT.match(str(cfg.get("commit") or "")):
            raise SourcePackError("invalid_mapping", "formal sources pin a system and a full 40-hex commit")
        if not re.match(r"^[\w.-]+/[\w.-]+$", str(cfg.get("repository") or "")) or not cfg.get("library") \
                or not cfg.get("license"):
            raise SourcePackError("invalid_mapping", "formal sources name a library, owner/repository and license")
        paths = [str(p) for p in cfg.get("paths") or []]
        if any(".." in p or p.startswith("/") for p in paths):
            raise SourcePackError("invalid_mapping", "formal paths are repository-relative")
        return paths

    def _page(self, item):
        cfg = self.config
        url = f"{self.source['endpoint'].rstrip('/')}/{cfg['repository']}/{cfg['commit']}/{item}"
        status, raw = self._get(url)
        if status == 404:
            return status, raw, [], "not_found"
        if status >= 400:
            raise SourcePackError("schema_drift", f"formal library host returned HTTP {status}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourcePackError("schema_drift", "formal source file is not UTF-8") from exc
        scanned = scan_file(cfg["system"], item, text)
        record = {"contract": RECORD_CONTRACT, "kind": "formal-file", "provider": "formal-library",
                  "provider_id": f"{cfg['library']}@{cfg['commit']}:{item}", "library": cfg["library"],
                  "system": cfg["system"], "repository": cfg["repository"], "commit": cfg["commit"], "path": item,
                  "license": cfg["license"], "file_sha256": _sha(raw), "method": SCAN_METHOD, **scanned}
        return status, raw, [{"id": f"formal:{cfg['library']}:{cfg['commit'][:12]}:{item}",
                              "title": f"{cfg['library']} {item} @ {cfg['commit'][:12]}", "language": "en",
                              "url": f"https://github.com/{cfg['repository']}/blob/{cfg['commit']}/{item}",
                              "math_record": record}], "returned"


ADAPTERS = {"zbmath": ZbmathAdapter, "oeis": OeisAdapter, "formal-library": FormalLibraryAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Pages are keyed by URL path plus query; bodies are JSON values or text."""

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        page = by_key.get(parts.path + (f"?{parts.query}" if parts.query else ""))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        body = page.get("body")
        content = json.dumps(body).encode() if isinstance(body, (dict, list)) else str(body or "").encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
