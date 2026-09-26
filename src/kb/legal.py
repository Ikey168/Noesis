"""Legal pack store: works, expressions, text versions, passages, citations and as-of selection.

Records arrive as ``noesis-native-regional-v1`` records from the existing CELLAR,
federal-court and Berlin parsers, through the source-pack runtime (or the
explicit Berlin import path) and are projected here:

* **Work** - the legal work or decision in one jurisdiction, keyed by its
  source-native identity (CELLAR work URI, RII ``doknr``, Berlin juris
  ``doknr``) with CELEX/ELI/ECLI, docket and gazette identifiers as supplied.
* **Expression** - one language version. Translations are separate
  expressions and are never assumed to be legally identical.
* **Version** - one manifestation or captured text of an expression, keyed by
  its native manifestation/item or source hash. Changed text is a new version;
  earlier versions are kept, and historical text stays marked historical.
* **Passages** - the official text with its exact XML-path/paragraph locator.
* **Citations** - explicit source relationships only (cites, amends,
  corrects, cited norms, prior instances). Nothing is inferred.
* **Temporal facts** - enactment, publication, entry into force,
  commencement, validity end and decision dates, each as a bitemporal
  assertion in the shared ``kb_temporal_assertions`` store (valid time from
  the source, observation time from acquisition).

``select_as_of`` returns candidate versions with their evidence; missing or
conflicting commencement evidence stays ``unknown``/``ambiguous``. Nothing here
asserts current legal force, and legal-effect assessment is a human review
step outside this store.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

READ_SCOPE = "knowledge:legal:read"
WRITE_SCOPE = "knowledge:legal:write"
WORK_CONTRACT = "noesis-legal-work-v1"
VERSION_CONTRACT = "noesis-legal-version-v1"
SELECTION_CONTRACT = "noesis-legal-version-selection-v1"
COMPARISON_CONTRACT = "noesis-legal-version-comparison-v1"
DEFAULT_NAMESPACE = "global"
REGIONAL_CONTRACT = "noesis-native-regional-v1"
PROVIDER_JURISDICTIONS = {"cellar": "EU", "german-courts": "DE", "berlin-law": "DE-BE"}
FACT_KINDS = ("enactment", "document", "publication", "entry_into_force", "commencement", "validity_end",
              "decision")
DOSSIER_RELATIONS = frozenset({"enacted_as", "amends", "transposes", "implements", "related_procedure"})
REVIEW_BOUNDARY = ("Source facts and version selection are not a determination of legal effect or current force; "
                   "that assessment needs human legal review.")

_DDL = """
CREATE TABLE IF NOT EXISTS legal_works (
  work_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, jurisdiction TEXT NOT NULL,
  work_kind TEXT NOT NULL, record_kind TEXT NOT NULL, native_id TEXT NOT NULL, identifiers_json TEXT NOT NULL,
  issuing_body TEXT, title TEXT, created_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_expressions (
  expression_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, language TEXT NOT NULL,
  native_expression TEXT, title TEXT
);
CREATE TABLE IF NOT EXISTS legal_versions (
  version_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, expression_id TEXT NOT NULL,
  native_version TEXT NOT NULL, text_sha256 TEXT, record_json TEXT NOT NULL, historical BOOLEAN,
  content_coverage TEXT NOT NULL, passage_count INTEGER NOT NULL, document_id TEXT, source_id TEXT,
  first_run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_passages (
  version_id TEXT NOT NULL, ordinal INTEGER NOT NULL, locator_key TEXT NOT NULL, locator_json TEXT NOT NULL,
  text TEXT NOT NULL, PRIMARY KEY(version_id, ordinal)
);
CREATE TABLE IF NOT EXISTS legal_citations (
  citation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, version_id TEXT NOT NULL,
  relation TEXT NOT NULL, target_raw TEXT NOT NULL, target_work_id TEXT, basis TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_facts (
  fact_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, work_id TEXT NOT NULL, version_id TEXT NOT NULL,
  fact_kind TEXT NOT NULL, value TEXT NOT NULL, basis TEXT NOT NULL, temporal_id TEXT,
  observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_dossier_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, dossier_id TEXT NOT NULL, work_id TEXT NOT NULL,
  relation TEXT NOT NULL, evidence TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS legal_selection_outcomes (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, page_key TEXT NOT NULL,
  outcome TEXT NOT NULL, detail_json TEXT NOT NULL, PRIMARY KEY(run_id, source_id, page_key)
);
"""


class LegalError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def _authorize(namespace: str, scopes, required: str, *, write: bool) -> None:
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & set(scopes):
        raise LegalError("unauthorized", f"{required} and namespace access are required")


def _day(value: Any) -> str | None:
    text = str(value or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else None


def _ms(day: str) -> int:
    return int(datetime.fromisoformat(day).timestamp() * 1000) if day >= "1970-01-01" else 0


def _celex_kind(celex: str | None) -> str:
    sector = str(celex or "")[:1]
    return {"6": "decision", "3": "normative", "5": "preparatory", "0": "consolidated"}.get(sector, "normative")


def _locator_key(locator: Mapping[str, Any]) -> str:
    parts = [locator.get("official_norm_id"), locator.get("path"), locator.get("paragraph_number"),
             locator.get("index_in_headings_and_paragraphs")]
    return "|".join("" if p is None else str(p) for p in parts)


class LegalStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------- projection

    def _work(self, namespace: str, record: Mapping[str, Any], run_id: str) -> tuple[str, str]:
        provider, fields = record["provider"], dict(record.get("fields") or {})
        jurisdiction = PROVIDER_JURISDICTIONS[provider]
        if provider == "cellar":
            native_id = str(fields.get("work") or "")
            kind = _celex_kind(fields.get("celex"))
            identifiers = {"celex": fields.get("celex"), "eli": list(fields.get("eli_identifiers") or []),
                           "ecli": list(fields.get("ecli_identifiers") or []), "cellar_work": native_id}
            body = None
        elif provider == "german-courts":
            native_id = str(record["provider_id"])
            kind = "decision"
            identifiers = {"doknr": native_id, "docket_number": fields.get("docket_number"),
                           "ecli": [fields["ecli"]] if fields.get("ecli") else []}
            body = fields.get("court")
        else:
            native_id = str(record["provider_id"])
            kind = "decision" if record["kind"] == "court-decision" else "gazette" \
                if record["kind"] == "gazette" else "normative"
            identifiers = {"official_id": native_id, "gazette_reference": fields.get("gazette_reference"),
                           "case_number": fields.get("case_number"),
                           "ecli": [fields["ecli"]] if fields.get("ecli") else []}
            body = fields.get("court")
        if not native_id:
            raise LegalError("invalid_record", "legal record lacks a native work identity")
        work_id = "legal-work:" + _digest([namespace, provider, jurisdiction, native_id])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [work_id, namespace, provider, jurisdiction, kind, record["kind"], native_id,
             _canonical({k: v for k, v in identifiers.items() if v not in (None, "", [])}), body,
             record.get("title"), run_id, self.now()])
        return work_id, jurisdiction

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str | None,
                documents: Mapping[str, str] | None = None, observed_at_ms: int | None = None) -> dict[str, int]:
        """Project regional legal records idempotently (a replay adds nothing)."""

        from src.kb.temporal import record_temporal_assertion

        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = {"works": 0, "versions": 0, "passages": 0, "citations": 0, "facts": 0}
        before = self.conn.execute("SELECT count(*) FROM legal_works WHERE namespace=?", [namespace]).fetchone()[0]
        self.conn.execute("BEGIN")
        try:
            for item in records:
                record = dict(item.get("legal_record") or item)
                if record.get("contract") != REGIONAL_CONTRACT or record.get("provider") not in PROVIDER_JURISDICTIONS:
                    raise LegalError("invalid_record", "page record is not a supported legal source record")
                fields = dict(record.get("fields") or {})
                work_id, jurisdiction = self._work(namespace, record, run_id)
                native_expression = fields.get("expression") or f"{record['provider_id']}@{record['language']}"
                expression_id = "legal-expression:" + _digest([work_id, record["language"], native_expression])[:24]
                self.conn.execute("INSERT OR IGNORE INTO legal_expressions VALUES (?,?,?,?,?,?)",
                                  [expression_id, namespace, work_id, record["language"], native_expression,
                                   record.get("title")])
                native = dict(record.get("native") or {})
                text_sha = native.get("xml_sha256") or native.get("original_sha256")
                native_version = str(fields.get("item") or fields.get("manifestation") or text_sha
                                     or record["provider_id"])
                sections = list(record.get("sections") or [])
                version_id = "legal-version:" + _digest([expression_id, native_version, text_sha])[:24]
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO legal_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING version_id",
                    [version_id, namespace, work_id, expression_id, native_version, text_sha,
                     _canonical({k: v for k, v in record.items() if k not in {"sections", "native"}}),
                     fields.get("historical"), "captured-text" if sections else "metadata-only", len(sections),
                     (documents or {}).get(str(item.get("id"))), source_id, run_id, observed]).fetchall()
                if not inserted:
                    continue
                counts["versions"] += 1
                for ordinal, section in enumerate(sections):
                    locator = dict(section.get("locator") or {})
                    self.conn.execute("INSERT INTO legal_passages VALUES (?,?,?,?,?)",
                                      [version_id, ordinal, _locator_key(locator), _canonical(locator),
                                       section["text"]])
                counts["passages"] += len(sections)
                for citation in self._citations(record):
                    citation_id = "legal-citation:" + _digest([version_id, citation])[:24]
                    self.conn.execute("INSERT OR IGNORE INTO legal_citations VALUES (?,?,?,?,?,?,?,?)",
                                      [citation_id, namespace, work_id, version_id, citation["relation"],
                                       citation["target"], None, citation["basis"]])
                    counts["citations"] += 1
                for kind, value in self._facts(record):
                    temporal_id = record_temporal_assertion(
                        self.conn, domain="legal", backing="namespace", assertion_kind="observation",
                        assertion_id=f"{work_id}:{kind}:{value}",
                        payload={"work_id": work_id, "version_id": version_id, "fact": kind, "value": value,
                                 "jurisdiction": jurisdiction, "namespace": namespace},
                        observed_at_ms=observed, valid_from_ms=_ms(value), valid_time_precision="day",
                        source_document_id=(documents or {}).get(str(item.get("id"))), visibility="public",
                        temporal_provenance={"basis": "source-reported", "provider": record["provider"]})
                    fact_id = "legal-fact:" + _digest([version_id, kind, value])[:24]
                    self.conn.execute("INSERT OR IGNORE INTO legal_facts VALUES (?,?,?,?,?,?,?,?,?)",
                                      [fact_id, namespace, work_id, version_id, kind, value, "source-reported",
                                       temporal_id, observed])
                    counts["facts"] += 1
            self._resolve_citation_targets(namespace)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        after = self.conn.execute("SELECT count(*) FROM legal_works WHERE namespace=?", [namespace]).fetchone()[0]
        counts["works"] = int(after) - int(before)
        return counts

    @staticmethod
    def _citations(record: Mapping[str, Any]) -> list[dict[str, str]]:
        fields = dict(record.get("fields") or {})
        output = []
        for relation in record.get("relationships") or []:
            if not isinstance(relation, Mapping) or not relation.get("target"):
                continue
            name = str(relation.get("relation") or "").rsplit("#", 1)[-1]
            output.append({"relation": {"work_cites_work": "cites",
                                        "resource_legal_amends_resource_legal": "amends",
                                        "resource_legal_corrects_resource_legal": "corrects"}.get(name, name or "related"),
                           "target": str(relation["target"]),
                           "basis": str(relation.get("basis") or "source-relationship")})
        for norm in re.split(r"\s*,\s*", str(fields.get("cited_norms") or "")):
            if norm.strip():
                output.append({"relation": "cites_norm", "target": norm.strip(), "basis": "court-metadata-norm"})
        if fields.get("prior_instances"):
            output.append({"relation": "prior_instance", "target": str(fields["prior_instances"]),
                           "basis": "court-metadata-vorinstanz"})
        return output

    @staticmethod
    def _facts(record: Mapping[str, Any]) -> list[tuple[str, str]]:
        fields = dict(record.get("fields") or {})
        facts: list[tuple[str, str]] = []
        for kind, keys in (("enactment", ("enactment_date",)), ("commencement", ("effective_from",)),
                           ("validity_end", ("effective_until",)), ("decision", ("decision_date",))):
            for key in keys:
                if _day(fields.get(key)):
                    facts.append((kind, _day(fields[key])))
        for kind, key in (("entry_into_force", "effective_dates"), ("publication", "publication_dates"),
                          ("document", "document_dates")):
            for value in fields.get(key) or []:
                if _day(value):
                    facts.append((kind, _day(value)))
        if record["provider"] == "cellar":
            # CELLAR's single effective_from is derived from effective_dates; keep one fact kind.
            facts = [(k, v) for k, v in facts if k != "commencement"]
        if record["provider"] == "berlin-law" and record.get("published_at") and not fields.get("enactment_date"):
            facts.append(("publication", _day(record["published_at"])))
        return sorted(set(facts))

    def _resolve_citation_targets(self, namespace: str) -> None:
        """Link explicit citation targets to known works by exact native identity only."""

        self.conn.execute(
            "UPDATE legal_citations SET target_work_id=w.work_id FROM legal_works w "
            "WHERE legal_citations.namespace=? AND w.namespace=legal_citations.namespace "
            "AND legal_citations.target_work_id IS NULL AND w.native_id=legal_citations.target_raw",
            [namespace])

    def record_outcome(self, namespace: str, run_id: str, source_id: str, receipt: Mapping[str, Any]) -> None:
        outcome = receipt.get("outcome")
        if not outcome:
            return
        position = receipt.get("selection_index", receipt.get("queue_index"))
        key = f"{position}:{receipt.get('decision') or receipt.get('official_id') or ''}"
        self.conn.execute("INSERT OR IGNORE INTO legal_selection_outcomes VALUES (?,?,?,?,?,?)",
                          [namespace, run_id, source_id, key, outcome, _canonical(dict(receipt))])

    # ----------------------------------------------------------------- reads

    def _work_row(self, namespace: str, work_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT work_id, provider, jurisdiction, work_kind, record_kind, native_id, identifiers_json, "
            "issuing_body, title FROM legal_works WHERE namespace=? AND work_id=?", [namespace, work_id]).fetchone()
        if row is None:
            raise LegalError("not_found", "legal work is not visible in this namespace")
        return {"contract": WORK_CONTRACT, "work_id": row[0], "provider": row[1], "jurisdiction": row[2],
                "work_kind": row[3], "record_kind": row[4], "native_id": row[5], "identifiers": _load(row[6], {}),
                "issuing_body": row[7], "title": row[8]}

    def lookup(self, namespace: str, *, scopes, identifier: str | None = None, title: str | None = None,
               citation: str | None = None, jurisdiction: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Exact identifier, title or citation lookup; never substitutes another jurisdiction."""

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        if not any((identifier, title, citation)):
            raise LegalError("invalid_request", "give an identifier, title or citation")
        if (title or citation) and not jurisdiction:
            raise LegalError("jurisdiction_required", "title and citation lookups need a jurisdiction (EU, DE, DE-BE)")
        rows = self.conn.execute(
            "SELECT work_id, jurisdiction, identifiers_json, title, native_id FROM legal_works WHERE namespace=? "
            "ORDER BY jurisdiction, work_id", [namespace]).fetchall()
        matches = []
        for work_id, jur, identifiers_json, work_title, native_id in rows:
            identifiers = _load(identifiers_json, {})
            flat = {str(native_id)} | {str(v) for v in identifiers.values() if isinstance(v, str)} | {
                str(x) for v in identifiers.values() if isinstance(v, list) for x in v}
            hit = None
            if identifier and identifier.strip() in flat:
                hit = "identifier"
            elif title and title.casefold() in str(work_title or "").casefold():
                hit = "title"
            elif citation:
                cited = self.conn.execute(
                    "SELECT 1 FROM legal_citations WHERE work_id=? AND strpos(lower(target_raw), lower(?))>0 LIMIT 1",
                    [work_id, citation]).fetchone()
                hit = "citation" if cited else None
            if hit:
                matches.append((jur, work_id, hit))
        in_scope = [m for m in matches if not jurisdiction or m[0] == jurisdiction]
        other = sorted({m[0] for m in matches if jurisdiction and m[0] != jurisdiction})
        works = [{**self._work_row(namespace, work_id), "matched_by": hit} for _, work_id, hit in in_scope[:limit]]
        status = ("found" if len({w["work_id"] for w in works}) == 1 else "ambiguous" if works
                  else "not_found_in_jurisdiction" if other else "not_covered")
        return {"contract": WORK_CONTRACT, "namespace": namespace, "status": status, "jurisdiction": jurisdiction,
                "works": works, "other_jurisdictions_with_matches": other, "truncated": len(in_scope) > limit,
                "coverage_notice": "Only selected, acquired sources are searched; absence is not absence of law."}

    def versions(self, namespace: str, work_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT v.version_id, e.expression_id, e.language, v.native_version, v.text_sha256, v.historical, "
            "v.content_coverage, v.passage_count, v.document_id, v.first_run_id, v.observed_at_ms "
            "FROM legal_versions v JOIN legal_expressions e ON e.expression_id=v.expression_id "
            "WHERE v.namespace=? AND v.work_id=? ORDER BY e.language, v.observed_at_ms, v.version_id",
            [namespace, work_id]).fetchall()
        return [{"contract": VERSION_CONTRACT, **dict(zip(
            ("version_id", "expression_id", "language", "native_version", "text_sha256", "historical",
             "content_coverage", "passage_count", "document_id", "first_run_id", "observed_at_ms"), row)),
            "facts": self.facts(namespace, row[0])} for row in rows]

    def facts(self, namespace: str, version_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT fact_kind, value, basis, temporal_id, observed_at_ms FROM legal_facts "
            "WHERE namespace=? AND version_id=? ORDER BY fact_kind, value", [namespace, version_id]).fetchall()
        return [dict(zip(("fact", "value", "basis", "temporal_id", "observed_at_ms"), row)) for row in rows]

    def inspect(self, namespace: str, work_id: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        work = self._work_row(namespace, work_id)
        citations = self.conn.execute(
            "SELECT DISTINCT relation, target_raw, target_work_id, basis FROM legal_citations "
            "WHERE namespace=? AND work_id=? ORDER BY relation, target_raw", [namespace, work_id]).fetchall()
        cited_by = self.conn.execute(
            "SELECT DISTINCT work_id, relation FROM legal_citations WHERE namespace=? AND target_work_id=? "
            "ORDER BY work_id", [namespace, work_id]).fetchall()
        links = self.conn.execute(
            "SELECT dossier_id, relation, evidence, principal_id FROM legal_dossier_links "
            "WHERE namespace=? AND work_id=? ORDER BY dossier_id", [namespace, work_id]).fetchall()
        return {**work, "versions": self.versions(namespace, work_id),
                "citations": [dict(zip(("relation", "target", "target_work_id", "basis"), c)) for c in citations],
                "cited_by": [{"work_id": c[0], "relation": c[1]} for c in cited_by],
                "procedure_links": [{**dict(zip(("dossier_id", "relation", "evidence", "principal_id"), link)),
                                     "notice": "Legislative procedure is context, not the authoritative legal text."}
                                    for link in links],
                "review_boundary": REVIEW_BOUNDARY}

    def passages(self, namespace: str, version_id: str, *, scopes, locator: str | None = None,
                 contains: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Exact passages of one version: by locator fragment or exact (case-insensitive) substring."""

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        row = self.conn.execute(
            "SELECT work_id, historical, content_coverage FROM legal_versions WHERE namespace=? AND version_id=?",
            [namespace, version_id]).fetchone()
        if row is None:
            raise LegalError("not_found", "legal version is not visible in this namespace")
        limit = max(1, min(int(limit), 100))
        clauses, params = [], [version_id]
        if locator:
            clauses.append("AND strpos(locator_key, ?)>0")
            params.append(locator)
        if contains:
            clauses.append("AND strpos(lower(text), lower(?))>0")
            params.append(contains)
        rows = self.conn.execute(
            f"SELECT ordinal, locator_json, text FROM legal_passages WHERE version_id=? {' '.join(clauses)} "
            "ORDER BY ordinal LIMIT ?", [*params, limit + 1]).fetchall()
        return {"version_id": version_id, "work_id": row[0], "historical": row[1], "content_coverage": row[2],
                "retrieval_mode": "exact-locator-or-substring",
                "status": "found" if rows else "no_matching_passage" if row[2] == "captured-text"
                else "metadata_only_version",
                "passages": [{"ordinal": r[0], "locator": _load(r[1], {}), "text": r[2]} for r in rows[:limit]],
                "truncated": len(rows) > limit,
                "notice": "Text is the source passage; any interpretation is separate and not part of this result."}

    def select_as_of(self, namespace: str, work_id: str, as_of: str, *, scopes,
                     language: str | None = None) -> dict[str, Any]:
        """Candidate versions valid on ``as_of`` with the sourced evidence for each; never guesses."""

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        try:
            day = date.fromisoformat(as_of).isoformat()
        except ValueError as exc:
            raise LegalError("invalid_request", "as_of must be YYYY-MM-DD") from exc
        work = self._work_row(namespace, work_id)
        versions = self.versions(namespace, work_id)
        other_languages = sorted({v["language"] for v in versions if language and v["language"] != language})
        if language:
            versions = [v for v in versions if v["language"] == language]
        candidates = []
        for version in versions:
            facts = {}
            for fact in version["facts"]:
                facts.setdefault(fact["fact"], []).append(fact["value"])
            starts = facts.get("commencement") or facts.get("entry_into_force") or []
            ends = facts.get("validity_end") or []
            if work["work_kind"] == "decision":
                decided = facts.get("decision") or facts.get("document") or []
                state = ("applies" if decided and min(decided) <= day else "after_as_of" if decided
                         else "date_unknown")
                basis = {"decision": decided}
            elif not starts:
                state, basis = "commencement_unknown", {}
            elif len(set(starts)) > 1 and min(starts) <= day < max(starts):
                state, basis = "ambiguous_commencement", {"commencement_candidates": sorted(set(starts))}
            elif min(starts) > day:
                state, basis = "not_yet_commenced", {"commencement": sorted(set(starts))}
            elif ends and min(ends) <= day:
                state, basis = "ended", {"commencement": sorted(set(starts)), "validity_end": sorted(set(ends))}
            else:
                state, basis = "applies", {"commencement": sorted(set(starts)), "validity_end": sorted(set(ends))}
            candidates.append({"version_id": version["version_id"], "expression_id": version["expression_id"],
                               "language": version["language"],
                               "historical": version["historical"], "state": state, "evidence": basis,
                               "observed_at_ms": version["observed_at_ms"]})
        applying = [c for c in candidates if c["state"] == "applies"]
        undetermined = [c for c in candidates if c["state"] in {"commencement_unknown", "ambiguous_commencement",
                                                                  "date_unknown"}]
        # Manifestations (formats) of one expression carry the same text and
        # evidence; they are equivalent, not competing versions.
        equivalent: list[str] = []
        if len(applying) > 1 and len({c["expression_id"] for c in applying}) == 1 \
                and len({_canonical(c["evidence"]) for c in applying}) == 1:
            equivalent = [c["version_id"] for c in applying[1:]]
            applying = applying[:1]
        if len(applying) == 1 and not undetermined:
            status, selected = "selected", applying[0]["version_id"]
        elif applying or undetermined:
            status, selected = "ambiguous" if len(applying) > 1 or (applying and undetermined) else "unknown", None
        else:
            status, selected = "none_applies", None
        return {"contract": SELECTION_CONTRACT, "namespace": namespace, "work_id": work_id, "as_of": day,
                "language": language, "status": status, "selected_version_id": selected,
                "equivalent_manifestations": equivalent, "candidates": candidates,
                "other_language_expressions": other_languages,
                "translation_notice": "Language expressions are separate versions and are not assumed legally identical.",
                "review_boundary": REVIEW_BOUNDARY}

    def compare_versions(self, namespace: str, left_version_id: str, right_version_id: str, *,
                         scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        sides = []
        for version_id in (left_version_id, right_version_id):
            row = self.conn.execute(
                "SELECT v.work_id, e.language FROM legal_versions v JOIN legal_expressions e "
                "ON e.expression_id=v.expression_id WHERE v.namespace=? AND v.version_id=?",
                [namespace, version_id]).fetchone()
            if row is None:
                raise LegalError("not_found", "legal version is not visible in this namespace")
            passages = self.conn.execute(
                "SELECT locator_key, text, locator_json FROM legal_passages WHERE version_id=? ORDER BY ordinal",
                [version_id]).fetchall()
            sides.append((row, {p[0]: (p[1], _load(p[2], {})) for p in passages}))
        (left_row, left), (right_row, right) = sides
        if left_row[0] != right_row[0]:
            raise LegalError("different_works", "version comparison needs two versions of the same work")
        changes = []
        for key in sorted(set(left) | set(right)):
            before, after = left.get(key), right.get(key)
            if before and after and before[0] == after[0]:
                continue
            changes.append({"locator": (after or before)[1],
                            "change": "added" if before is None else "removed" if after is None else "changed",
                            "before": None if before is None else before[0],
                            "after": None if after is None else after[0]})
        return {"contract": COMPARISON_CONTRACT, "namespace": namespace, "work_id": left_row[0],
                "left_version_id": left_version_id, "right_version_id": right_version_id,
                "languages": [left_row[1], right_row[1]],
                "cross_language": left_row[1] != right_row[1],
                "changes": changes, "unchanged_passages": len(set(left) & set(right)) - sum(
                    1 for c in changes if c["change"] == "changed"),
                "notice": ("Passage differences are source changes, not a legal-effect assessment."
                           + (" Different languages are compared textually only." if left_row[1] != right_row[1]
                              else ""))}

    def link_dossier(self, namespace: str, dossier_id: str, work_id: str, relation: str, evidence: str, *,
                     scopes, principal_id: str) -> dict[str, Any]:
        """Link a political legislative dossier to an enacted work without copying either store."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if relation not in DOSSIER_RELATIONS:
            raise LegalError("invalid_relation", f"relation must be one of {sorted(DOSSIER_RELATIONS)}")
        if not str(evidence or "").strip():
            raise LegalError("invalid_request", "cite the evidence for the procedure-to-instrument link")
        self._work_row(namespace, work_id)
        try:
            exists = self.conn.execute(
                "SELECT 1 FROM legislative_dossiers WHERE namespace=? AND dossier_id=? LIMIT 1",
                [namespace, dossier_id]).fetchone()
        except Exception:  # noqa: BLE001 - dossier store not initialized
            exists = None
        if not exists:
            raise LegalError("dossier_not_found", "the legislative dossier is not visible in this namespace")
        link_id = "legal-dossier-link:" + _digest([namespace, dossier_id, work_id, relation])[:24]
        self.conn.execute("INSERT OR IGNORE INTO legal_dossier_links VALUES (?,?,?,?,?,?,?,?)",
                          [link_id, namespace, dossier_id, work_id, relation, evidence.strip(), principal_id,
                           self.now()])
        return {"link_id": link_id, "dossier_id": dossier_id, "work_id": work_id, "relation": relation}

    def selection_outcomes(self, namespace: str, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT source_id, page_key, outcome, detail_json FROM legal_selection_outcomes WHERE namespace=? "
            "AND run_id=? ORDER BY source_id, page_key", [namespace, run_id]).fetchall()
        return [{"source_id": r[0], "key": r[1], "outcome": r[2], "detail": _load(r[3], {})} for r in rows]


class LegalProjector:
    """Source-pack runtime projector for ``noesis-legal-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = LegalStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("legal") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        document_ids = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): str(d["document_id"])
                        for d in documents}
        self.store.record_outcome(namespace, run_id, source["source_id"], page_receipt)
        return self.store.project(namespace, records, run_id=run_id, source_id=source["source_id"],
                                  documents=document_ids)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        outcomes = [o for o in self.store.selection_outcomes(self._namespace(source), run_id)
                    if o["source_id"] == source["source_id"]]
        return {"status": status, "not_found": sum(o["outcome"] == "not_found" for o in outcomes),
                "returned": sum(o["outcome"] == "returned" for o in outcomes)}


def readiness(conn: Any, *, pack_id: str = "legal-research") -> dict[str, Any]:
    """Per provider/jurisdiction readiness; only installed sources are listed as capabilities."""

    from src.ingestion.legal_sources import PROVIDER_CONTRACTS
    from src.ingestion.source_packs import _digest as source_digest
    from src.kb.legal_retrieval import retrieval_modes

    try:
        row = conn.execute(
            "SELECT c.enabled,v.manifest_json FROM source_pack_current c JOIN source_pack_versions v "
            "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?", [pack_id]).fetchone()
    except Exception:  # noqa: BLE001 - runtime tables absent until first install
        row = None
    enabled = bool(row and row[0])
    manifest = _load(row[1], {}) if row else {}
    by_connector = {s["connector"]: s for s in manifest.get("sources") or []}
    providers = {}
    for connector, contract in PROVIDER_CONTRACTS.items():
        source = by_connector.get(connector)
        blockers = []
        if source is None:
            blockers.append({"code": "source_not_installed", "severity": "blocking"})
        elif not enabled:
            blockers.append({"code": "pack_disabled", "severity": "blocking"})
        if source is not None:
            policy = source["license"]
            terms_hash = source_digest({"terms_url": policy["terms_url"], "redistribution": policy["redistribution"]})
            try:
                accepted = bool(conn.execute(
                    "SELECT 1 FROM source_pack_license_acceptance WHERE pack_id=? AND source_id=? AND license_id=? "
                    "AND terms_hash=?", [pack_id, source["source_id"], policy["id"], terms_hash]).fetchone())
            except Exception:  # noqa: BLE001
                accepted = False
            if not accepted:
                blockers.append({"code": "license_not_accepted", "source_id": source["source_id"],
                                 "severity": "blocking-live"})
        providers[connector] = {
            "jurisdiction": contract["jurisdiction"], "installed": source is not None,
            "fixture": "ready" if source is not None and enabled else "blocked",
            "live": "ready" if source is not None and enabled and not blockers else "blocked",
            "coverage": contract["coverage"], "blockers": blockers,
            "pack_live_acceptance": "outstanding",
            "prior_adapter_live_evidence": contract["prior_live_evidence"],
        }
    return {"pack_id": pack_id, "installed": row is not None, "enabled": enabled, "providers": providers,
            "retrieval_modes": retrieval_modes(), "review_boundary": REVIEW_BOUNDARY}
