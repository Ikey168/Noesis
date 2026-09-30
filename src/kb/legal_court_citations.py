"""Docket entries and opinions linked to cited provisions and decisions by exact citation (#2218, CJ08).

Citations are parsed exactly from the text as published - docket-entry
descriptions and opinion paragraphs - never by topic similarity:

* **Statutes** ``42 U.S.C. § 1983``, ``15 U.S.C. §45(a)``, ``42 USC 1983``
  (key ``usc:<title>:<section>``, subsection kept as the locator);
* **Regulations** ``5 C.F.R. § 2635.101`` (key ``cfr:<title>:<part.section>``);
* **Case law** reporter citations (``990 F.4th 12``, ``999 F. Supp. 4th 201``,
  ``123 U.S. 456``) and CourtListener's own ``opinions_cited`` links, which
  resolve to opinion-cluster works by cluster/opinion id.

The German statutory citation parser of the federal-statutes feature
(:mod:`src.kb.legal_citations`, #2105) covers German citation syntax; the
CourtListener selection is US federal, so US Code and CFR citations are parsed
here with the same rules: exact syntax only, character offsets kept, and an
unknown or unacquired target stays an **unresolved** citation with its source
text. Statutory targets resolve to Legal works at provision level: a work whose
identifiers list the provision key (``usc_provisions``) or the title
(``usc_title``/``cfr_title``), recording the provision path. Each link records
the citing docket or opinion revision and its locator. Nothing characterises
the citing relationship (followed, distinguished, overruled).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from src.kb.courts_justice import (
    READ_SCOPE,
    WRITE_SCOPE,
    authorize,
    canonical,
    digest,
    load,
    table_exists,
)

LINK_CONTRACT = "noesis-court-citation-link-v1"
_USC = re.compile(r"(?<![\w.])(\d{1,2})\s+U\.?\s?S\.?\s?C\.?(?:\s?A\.?)?\s*(?:§§?|[Ss]ec(?:tion|\.)?)?\s*"
                  r"(\d+[a-z]?(?:-\d+)?)((?:\([0-9A-Za-z]{1,4}\))*)")
_CFR = re.compile(r"(?<![\w.])(\d{1,2})\s+C\.?\s?F\.?\s?R\.?\s*(?:§§?|[Pp]art)?\s*(\d+(?:\.\d+)?)((?:\([0-9a-z]{1,4}\))*)")
_REPORTERS = (r"U\.\s?S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|F\.\s?Supp\.(?:\s?(?:2d|3d|4th))?|"
              r"F\.(?:\s?(?:2d|3d|4th))?|F\.\s?App'x")
_CASE = re.compile(r"(?<![\w.])(\d{1,4})\s+(" + _REPORTERS + r")\s+(\d{1,5})(?!\d)")
_DDL = """
CREATE TABLE IF NOT EXISTS legal_court_citation_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citing_kind TEXT NOT NULL, citing_record_key TEXT NOT NULL,
  citing_revision_id TEXT NOT NULL, citing_work_id TEXT NOT NULL, citing_version_id TEXT, locator_json TEXT NOT NULL,
  citation_kind TEXT NOT NULL, raw TEXT NOT NULL, normalized TEXT NOT NULL, target_key TEXT NOT NULL,
  target_work_id TEXT, target_provision TEXT, status TEXT NOT NULL, basis TEXT NOT NULL
);
"""


def normalize_reporter(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).casefold()


def parse_us_citations(text: Any) -> list[dict[str, Any]]:
    """Exact statute, regulation and reporter citations with their offsets, in text order."""
    text = str(text or "")
    found = []
    for match in _USC.finditer(text):
        title, section, sub = match.group(1), match.group(2), match.group(3)
        found.append({"kind": "statute", "raw": match.group(0).strip(), "start": match.start(), "end": match.end(),
                      "normalized": f"{title} U.S.C. § {section}{sub}", "key": f"usc:{title}:{section}",
                      "title": title, "provision": f"§ {section}{sub}"})
    for match in _CFR.finditer(text):
        title, section, sub = match.group(1), match.group(2), match.group(3)
        found.append({"kind": "regulation", "raw": match.group(0).strip(), "start": match.start(),
                      "end": match.end(), "normalized": f"{title} C.F.R. § {section}{sub}",
                      "key": f"cfr:{title}:{section}", "title": title, "provision": f"§ {section}{sub}"})
    for match in _CASE.finditer(text):
        reporter = re.sub(r"\s+", " ", match.group(2)).strip()
        normalized = f"{match.group(1)} {reporter} {match.group(3)}"
        found.append({"kind": "case", "raw": match.group(0).strip(), "start": match.start(), "end": match.end(),
                      "normalized": normalized, "key": "reporter:" + normalize_reporter(normalized)})
    return sorted(found, key=lambda c: (c["start"], c["kind"]))


class CourtCitations:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _works(self, namespace: str) -> list[tuple[str, dict[str, Any], str]]:
        rows = self.conn.execute("SELECT work_id, identifiers_json, work_kind FROM legal_works WHERE namespace=? "
                                 "ORDER BY work_id", [namespace]).fetchall()
        return [(r[0], load(r[1], {}), r[2]) for r in rows]

    def _resolve(self, citation: dict[str, Any], works) -> tuple[str | None, str | None, str]:
        if citation["kind"] in {"statute", "regulation"}:
            prefix = "usc" if citation["kind"] == "statute" else "cfr"
            exact = [w for w, ids, _ in works if citation["key"] in (ids.get(f"{prefix}_provisions") or [])]
            titled = [w for w, ids, _ in works if str(ids.get(f"{prefix}_title") or "") == citation["title"]]
            chosen = exact or titled
            if len(chosen) == 1:
                return chosen[0], citation["provision"], ("provision identifier" if exact else "title identifier")
            return None, citation["provision"], "no single acquired Legal work carries this provision"
        key = citation["key"]
        matches = [w for w, ids, kind in works if kind == "decision" and key in {
            "reporter:" + normalize_reporter(c) for c in ids.get("citations") or []}]
        if len(matches) == 1:
            return matches[0], None, "reporter citation"
        return None, None, "no single acquired decision carries this reporter citation"

    def _insert(self, namespace, citing_kind, record_key, revision_id, work_id, version_id, locator, citation,
                works) -> bool:
        target, provision, basis = self._resolve(citation, works)
        link_id = "court-citation:" + digest([namespace, revision_id, canonical(locator), citation["start"],
                                              citation["raw"]])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO legal_court_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "RETURNING link_id",
            [link_id, namespace, citing_kind, record_key, revision_id, work_id, version_id,
             canonical({**locator, "start": citation["start"], "end": citation["end"]}), citation["kind"],
             citation["raw"], citation["normalized"], citation["key"], target, provision,
             "resolved" if target else "unresolved", f"exact citation parse; {basis}"]).fetchall()
        if inserted and version_id:
            relation = "cites_norm" if citation["kind"] != "case" else "cites"
            self.conn.execute("INSERT OR IGNORE INTO legal_citations VALUES (?,?,?,?,?,?,?,?)",
                              ["legal-citation:" + digest([version_id, link_id])[:24], namespace, work_id,
                               version_id, relation, citation["normalized"], target, "exact-citation-parse"])
        return bool(inserted)

    def link(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Parse every acquired docket entry and opinion paragraph; idempotent, and re-resolves open links."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        works = self._works(namespace)
        created = 0
        if table_exists(self.conn, "legal_docket_revisions"):
            rows = self.conn.execute(
                "SELECT r.record_key, r.revision_id, r.work_id, r.version_id, e.entry_number, e.description FROM "
                "legal_docket_entries e JOIN legal_docket_revisions r USING(revision_id) WHERE r.namespace=? "
                "ORDER BY r.record_key, r.revision_no, e.ordinal", [namespace]).fetchall()
            for key, revision, work, version, number, description in rows:
                for citation in parse_us_citations(description):
                    created += self._insert(namespace, "docket-entry", key, revision, work, version,
                                            {"entry_number": number, "field": "description"}, citation, works)
        if table_exists(self.conn, "legal_opinion_revisions"):
            rows = self.conn.execute(
                "SELECT r.record_key, r.revision_id, r.work_id, o.opinion_id, o.version_id, p.ordinal, p.text, "
                "p.locator_json FROM legal_opinions o JOIN legal_opinion_revisions r USING(revision_id) JOIN "
                "legal_passages p ON p.version_id=o.version_id WHERE r.namespace=? ORDER BY r.record_key, "
                "r.revision_no, o.opinion_id, p.ordinal", [namespace]).fetchall()
            for key, revision, work, _opinion, version, _ordinal, text, locator in rows:
                for citation in parse_us_citations(text):
                    created += self._insert(namespace, "opinion", key, revision, work, version, load(locator, {}),
                                            citation, works)
            # CourtListener's own opinions_cited links: resolved by opinion id to the cluster work carrying it.
            cited_rows = self.conn.execute(
                "SELECT r.record_key, r.revision_id, r.work_id, o.opinion_id, o.version_id, o.opinions_cited_json "
                "FROM legal_opinions o JOIN legal_opinion_revisions r USING(revision_id) WHERE r.namespace=?",
                [namespace]).fetchall()
            for key, revision, work, opinion_id, version, cited in cited_rows:
                for target_opinion in load(cited, []):
                    targets = [w for w, ids, kind in works if kind == "decision"
                               and str(target_opinion) in (ids.get("courtlistener_opinion_ids") or [])]
                    citation = {"kind": "case", "raw": f"opinions_cited: opinion {target_opinion}", "start": -1,
                                "end": -1, "normalized": f"CourtListener opinion {target_opinion}",
                                "key": f"courtlistener-opinion:{target_opinion}"}
                    link_id = "court-citation:" + digest([namespace, revision, opinion_id, target_opinion])[:24]
                    self.conn.execute(
                        "INSERT OR IGNORE INTO legal_court_citation_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [link_id, namespace, "opinion", key, revision, work, version,
                         canonical({"opinion_id": opinion_id, "field": "opinions_cited"}), "case", citation["raw"],
                         citation["normalized"], citation["key"], targets[0] if len(targets) == 1 else None, None,
                         "resolved" if len(targets) == 1 else "unresolved",
                         "CourtListener opinions_cited link by opinion id"])
        # Targets acquired after a link was made are resolved now (exact identity only).
        for link_id, key, raw in self.conn.execute(
                "SELECT link_id, target_key, raw FROM legal_court_citation_links WHERE namespace=? AND "
                "status='unresolved' AND target_key NOT LIKE 'courtlistener-opinion:%'", [namespace]).fetchall():
            parsed = [c for c in parse_us_citations(raw) if c["key"] == key]
            if parsed:
                target, provision, basis = self._resolve(parsed[0], works)
                if target:
                    self.conn.execute("UPDATE legal_court_citation_links SET target_work_id=?, target_provision=?, "
                                      "status='resolved', basis=? WHERE link_id=?",
                                      [target, provision, f"exact citation parse; {basis}", link_id])
        return {"created": created, "links": self.links(namespace)}

    _COLUMNS = ("link_id", "citing_kind", "citing_record_key", "citing_revision_id", "citing_work_id",
                "citing_version_id", "locator_json", "citation_kind", "raw", "normalized", "target_key",
                "target_work_id", "target_provision", "status", "basis")

    def links(self, namespace: str, *, status: str | None = None, citing_record_key: str | None = None
              ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "legal_court_citation_links"):
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(self._COLUMNS)} FROM legal_court_citation_links WHERE namespace=? AND (? IS NULL OR "
            "status=?) AND (? IS NULL OR citing_record_key=?) ORDER BY citing_record_key, citing_revision_id, link_id",
            [namespace, status, status, citing_record_key, citing_record_key]).fetchall()
        out = []
        for row in rows:
            item = dict(zip(self._COLUMNS, row))
            item["locator"] = load(item.pop("locator_json"), {})
            out.append({"contract": LINK_CONTRACT, **item})
        return out

    def links_to(self, namespace: str, target_key: str) -> list[dict[str, Any]]:
        return [link for link in self.links(namespace) if link["target_key"] == target_key]

    def list_links(self, namespace: str, *, scopes: Iterable[str], status: str | None = None,
                   citing_record_key: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        links = self.links(namespace, status=status, citing_record_key=citing_record_key)
        return {"links": links, "unresolved": [link for link in links if link["status"] == "unresolved"],
                "notice": "exact citation links only; nothing is linked by topic and the citing relationship is not "
                          "characterised"}
