"""Mathematics in Research: literature, OEIS sequences, formal declarations, objects and links.

Projected from ``noesis-math-record-v1`` records (zbMATH Open, OEIS, formal
library files) without collapsing providers:

* **Literature** and **sequences** keep every observed record revision with
  its provider id and identifiers (Zbl, DOI, arXiv, A-number).
* **Formal snapshots** are keyed by library + immutable commit. Declarations
  keep file, line span, statement text and hash; module imports are explicit
  dependencies, statement-token references are *lexical* dependencies (the
  scan does not elaborate). Every declaration query names its commit.
* **Objects** are source-backed expressions, definitions, theorem statements,
  proofs and notation aliases with an exact source span, method and
  confidence. Formal declarations are parsed objects; text extraction yields
  candidates. Normalized strings serve search only, never equivalence.
* **Links** are explicit (a citation or shared identifier from a source) or
  candidates (names, statements, expressions) that a reviewer accepts or
  rejects. An accepted candidate remains a reviewed candidate, not an identity.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

READ_SCOPE = "knowledge:mathematics:read"
WRITE_SCOPE = "knowledge:mathematics:write"
REVIEW_SCOPE = "knowledge:mathematics:review"
SEARCH_CONTRACT = "noesis-math-search-v1"
DECLARATION_CONTRACT = "noesis-formal-declaration-v1"
LINK_CONTRACT = "noesis-math-link-v1"
DIFF_CONTRACT = "noesis-formal-snapshot-diff-v1"
REFERENCES_CONTRACT = "noesis-formal-references-v1"
OBJECT_KINDS = ("expression", "definition", "theorem", "proof", "notation-alias")
EXPLICIT = ("explicit-citation", "explicit-identifier")
CANDIDATE = ("candidate-name", "candidate-statement", "candidate-expression")
TEXT_METHOD = "text-pattern:1.0.0"
_THEOREM_KINDS = {"theorem", "lemma", "corollary", "proposition"}
_DEFINITION_KINDS = {"def", "definition", "abbrev", "abbreviation", "fun", "function", "primrec", "fixpoint",
                     "structure", "class", "inductive", "record"}
_STOP = {"about", "their", "which", "there", "these", "those", "numbers", "number", "natural", "theorem", "lemma",
         "sequence", "sequences", "proof", "proofs", "representation", "representations", "formal", "fixture"}

_DDL = """
CREATE TABLE IF NOT EXISTS math_literature (
  provider TEXT NOT NULL, provider_id TEXT NOT NULL, record_sha TEXT NOT NULL, record_json TEXT NOT NULL,
  run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(provider, provider_id, record_sha)
);
CREATE TABLE IF NOT EXISTS math_identifiers (
  subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, scheme TEXT NOT NULL, value TEXT NOT NULL,
  PRIMARY KEY(subject_kind, subject_id, scheme, value)
);
CREATE TABLE IF NOT EXISTS math_sequences (
  a_number TEXT NOT NULL, record_sha TEXT NOT NULL, record_json TEXT NOT NULL, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(a_number, record_sha)
);
CREATE TABLE IF NOT EXISTS formal_snapshots (
  library TEXT NOT NULL, commit_sha TEXT NOT NULL, system TEXT NOT NULL, repository TEXT NOT NULL,
  license TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(library, commit_sha)
);
CREATE TABLE IF NOT EXISTS formal_files (
  library TEXT NOT NULL, commit_sha TEXT NOT NULL, path TEXT NOT NULL, module TEXT NOT NULL,
  file_sha256 TEXT NOT NULL, imports_json TEXT NOT NULL, method TEXT NOT NULL, run_id TEXT NOT NULL,
  PRIMARY KEY(library, commit_sha, path)
);
CREATE TABLE IF NOT EXISTS formal_declarations (
  library TEXT NOT NULL, commit_sha TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL, path TEXT NOT NULL,
  module TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, statement TEXT NOT NULL,
  statement_sha256 TEXT NOT NULL, doc TEXT, PRIMARY KEY(library, commit_sha, name)
);
CREATE TABLE IF NOT EXISTS formal_dependencies (
  library TEXT NOT NULL, commit_sha TEXT NOT NULL, from_name TEXT NOT NULL, to_name TEXT NOT NULL,
  kind TEXT NOT NULL, method TEXT NOT NULL, PRIMARY KEY(library, commit_sha, from_name, to_name, kind)
);
CREATE TABLE IF NOT EXISTS math_objects (
  object_id TEXT PRIMARY KEY, kind TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref_json TEXT NOT NULL,
  exact_text TEXT NOT NULL, normalized_text TEXT NOT NULL, method TEXT NOT NULL, confidence DOUBLE,
  alias_of TEXT, notation TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS math_links (
  link_id TEXT PRIMARY KEY, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL, object_kind TEXT NOT NULL,
  object_id TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL, confidence DOUBLE,
  state TEXT NOT NULL, reviewed_by TEXT, review_note TEXT, created_at_ms BIGINT NOT NULL, reviewed_at_ms BIGINT
);
"""

_ISABELLE = {"Rightarrow": "⇒", "Longrightarrow": "⟹", "longleftrightarrow": "⟷", "forall": "∀", "exists": "∃",
             "le": "≤", "ge": "≥", "noteq": "≠", "in": "∈", "notin": "∉", "and": "∧", "or": "∨", "not": "¬",
             "Sum": "∑", "Prod": "∏", "times": "×", "lambda": "λ", "subseteq": "⊆", "union": "∪", "inter": "∩",
             "rightarrow": "→", "equiv": "≡"}
_LATEX = {"sum": "∑", "prod": "∏", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠", "in": "∈",
          "forall": "∀", "exists": "∃", "to": "→", "rightarrow": "→", "Rightarrow": "⇒", "cdot": "·", "times": "×",
          "land": "∧", "lor": "∨", "neg": "¬", "lambda": "λ", "infty": "∞"}


class MathematicsError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require(scopes: Iterable[str], scope: str) -> None:
    if scope not in set(scopes):
        raise MathematicsError("unauthorized", f"{scope} is required")


def normalize_notation(text: str) -> str:
    """Search-only normal form: Isabelle symbols and common LaTeX macros to Unicode, NFC, collapsed spaces."""

    value = re.sub(r"\\<([A-Za-z]+)>", lambda m: _ISABELLE.get(m.group(1), m.group(0)), text or "")
    value = re.sub(r"\\mathbb\{([A-Z])\}", lambda m: {"N": "ℕ", "Z": "ℤ", "Q": "ℚ", "R": "ℝ", "C": "ℂ"}.get(
        m.group(1), m.group(0)), value)
    value = re.sub(r"\\([A-Za-z]+)", lambda m: _LATEX.get(m.group(1), m.group(0)), value)
    value = unicodedata.normalize("NFC", value).replace("$", "")  # NFKC would fold ℕ into N
    return re.sub(r"\s+", " ", value).strip()


def _name_tokens(name: str) -> set[str]:
    short = name.rsplit(".", 1)[-1]
    parts = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", short.replace("_", " "))
    return {p.casefold() for p in parts if len(p) >= 3}


def _words(text: str) -> set[str]:
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().casefold()
    return {w for w in re.findall(r"[a-z]{3,}", folded) if w not in _STOP}


def _statement_tokens(text: str) -> set[str]:
    return {t.casefold().replace("_", "") for t in re.findall(r"[A-Za-z_][\w']*", normalize_notation(text))
            if len(t) > 1}


def _jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


class MathStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ projection

    def observe_page(self, records: Sequence[Mapping[str, Any]], *, run_id: str) -> dict[str, int]:
        observed = self.now()
        counts = {"literature": 0, "sequences": 0, "formal_files": 0, "declarations": 0}
        snapshots: set[tuple[str, str]] = set()
        self.conn.execute("BEGIN")
        try:
            for item in records:
                record = dict(item.get("math_record") or {})
                kind = record.get("kind")
                if kind == "literature":
                    counts["literature"] += self._literature(record, run_id, observed)
                elif kind == "sequence":
                    counts["sequences"] += self._sequence(record, run_id, observed)
                elif kind == "formal-file":
                    added = self._formal_file(record, run_id, observed)
                    counts["formal_files"] += int(added >= 0)
                    counts["declarations"] += max(added, 0)
                    snapshots.add((record["library"], record["commit"]))
            for library, commit in sorted(snapshots):
                self._lexical_dependencies(library, commit)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def _literature(self, record: Mapping[str, Any], run_id: str, observed: int) -> int:
        sha = _digest({k: v for k, v in record.items() if k != "raw_sha256"})
        inserted = self.conn.execute("INSERT OR IGNORE INTO math_literature VALUES (?,?,?,?,?,?) RETURNING provider",
                                     [record["provider"], record["provider_id"], sha, _canonical(record), run_id,
                                      observed]).fetchall()
        subject = f"{record['provider']}:{record['provider_id']}"
        for scheme, values in dict(record.get("identifiers") or {}).items():
            for value in values:
                self.conn.execute("INSERT OR IGNORE INTO math_identifiers VALUES ('literature',?,?,?)",
                                  [subject, scheme, str(value).casefold()])
        return len(inserted)

    def _sequence(self, record: Mapping[str, Any], run_id: str, observed: int) -> int:
        sha = _digest({k: v for k, v in record.items() if k != "raw_sha256"})
        self.conn.execute("INSERT OR IGNORE INTO math_identifiers VALUES ('sequence',?,'oeis',?)",
                          [record["provider_id"], record["provider_id"].casefold()])
        return len(self.conn.execute("INSERT OR IGNORE INTO math_sequences VALUES (?,?,?,?,?) RETURNING a_number",
                                     [record["provider_id"], sha, _canonical(record), run_id, observed]).fetchall())

    def _formal_file(self, record: Mapping[str, Any], run_id: str, observed: int) -> int:
        library, commit = record["library"], record["commit"]
        self.conn.execute("INSERT OR IGNORE INTO formal_snapshots VALUES (?,?,?,?,?,?)",
                          [library, commit, record["system"], record["repository"], record["license"], observed])
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO formal_files VALUES (?,?,?,?,?,?,?,?) RETURNING path",
            [library, commit, record["path"], record["module"], record["file_sha256"],
             _canonical(record.get("imports") or []), record["method"], run_id]).fetchall()
        if not inserted:
            return -1
        for target in record.get("imports") or []:
            self.conn.execute("INSERT OR IGNORE INTO formal_dependencies VALUES (?,?,?,?,'module-import',?)",
                              [library, commit, record["module"], target, "explicit-import"])
        count = 0
        for decl in record.get("declarations") or []:
            count += len(self.conn.execute(
                "INSERT OR IGNORE INTO formal_declarations VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING name",
                [library, commit, decl["name"], decl["kind"], record["path"], decl["module"], decl["line_start"],
                 decl["line_end"], decl["statement"], decl["statement_sha256"], decl.get("doc")]).fetchall())
        return count

    def _lexical_dependencies(self, library: str, commit: str) -> None:
        rows = self.conn.execute("SELECT name, statement FROM formal_declarations WHERE library=? AND commit_sha=?",
                                 [library, commit]).fetchall()
        names = {row[0] for row in rows}
        by_short: dict[str, list[str]] = {}
        for name in names:
            parts = name.split(".")
            for size in range(1, len(parts)):
                by_short.setdefault(".".join(parts[-size:]), []).append(name)
        self.conn.execute("DELETE FROM formal_dependencies WHERE library=? AND commit_sha=? AND kind='lexical-reference'",
                          [library, commit])
        for name, statement in rows:
            short = name.rsplit(".", 1)[-1]
            body = re.sub(r"^\S+\s+" + re.escape(short) + r"\b", "", statement, count=1)
            targets = set()
            for token in set(re.findall(r"[A-Za-z_][\w.']*", body)):
                token = token.rstrip(".")
                if token in names:
                    targets.add(token)
                elif len(by_short.get(token, [])) == 1:
                    targets.add(by_short[token][0])
            for target in sorted(targets - {name}):
                self.conn.execute("INSERT OR IGNORE INTO formal_dependencies VALUES (?,?,?,?,'lexical-reference',?)",
                                  [library, commit, name, target, "lexical-token-match"])

    # --------------------------------------------------------------- reading

    def _latest(self, table: str, key: str, value: str) -> dict[str, Any] | None:
        row = self.conn.execute(f"SELECT record_json, observed_at_ms FROM {table} WHERE {key}=? "
                                "ORDER BY observed_at_ms DESC, record_sha LIMIT 1", [value]).fetchone()
        return None if row is None else {**json.loads(row[0]), "observed_at_ms": row[1]}

    def literature(self, provider: str, provider_id: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute("SELECT record_json, observed_at_ms, run_id FROM math_literature WHERE provider=? AND "
                                 "provider_id=? ORDER BY observed_at_ms", [provider, provider_id]).fetchall()
        if not rows:
            raise MathematicsError("not_found", "literature record not acquired")
        current = json.loads(rows[-1][0])
        subject = f"{provider}:{provider_id}"
        return {"subject": {"kind": "literature", "id": subject}, "record": current,
                "revisions": [{"observed_at_ms": r[1], "run_id": r[2], "revision": json.loads(r[0]).get("revision")}
                              for r in rows],
                "links": self.links("literature", subject, scopes=scopes)["links"]}

    def sequence(self, a_number: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute("SELECT record_json, observed_at_ms FROM math_sequences WHERE a_number=? "
                                 "ORDER BY observed_at_ms", [a_number]).fetchall()
        if not rows:
            raise MathematicsError("not_found", "sequence not acquired")
        current = json.loads(rows[-1][0])
        return {"subject": {"kind": "sequence", "id": a_number}, "record": current,
                "revisions": [{"observed_at_ms": r[1], "revision": json.loads(r[0]).get("revision"),
                               "time": json.loads(r[0]).get("time")} for r in rows],
                "links": self.links("sequence", a_number, scopes=scopes)["links"]}

    def _snapshot(self, library: str, commit: str) -> dict[str, Any]:
        if not re.match(r"^[0-9a-f]{40}$", commit or ""):
            raise MathematicsError("revision_required", "formal queries pin a full 40-hex commit")
        row = self.conn.execute("SELECT system, repository, license, observed_at_ms FROM formal_snapshots WHERE "
                                "library=? AND commit_sha=?", [library, commit]).fetchone()
        if row is None:
            raise MathematicsError("not_found", "formal snapshot not acquired")
        return {"library": library, "commit": commit, "system": row[0], "repository": row[1], "license": row[2],
                "observed_at_ms": row[3]}

    def _declaration_row(self, snapshot: Mapping[str, Any], row: Sequence[Any]) -> dict[str, Any]:
        name, kind, path, module, start, end, statement, sha, doc = row
        return {"contract": DECLARATION_CONTRACT, "library": snapshot["library"], "system": snapshot["system"],
                "repository": snapshot["repository"], "commit": snapshot["commit"], "license": snapshot["license"],
                "name": name, "kind": kind, "path": path, "module": module, "line_start": start, "line_end": end,
                "statement": statement, "statement_sha256": sha, "doc": doc, "method": "lexical-declaration-scan",
                "object_status": "parsed-formal-declaration",
                "permalink": f"https://github.com/{snapshot['repository']}/blob/{snapshot['commit']}/{path}"
                             f"#L{start}-L{end}"}

    def declaration(self, library: str, name: str, commit: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        snapshot = self._snapshot(library, commit)
        row = self.conn.execute("SELECT name, kind, path, module, line_start, line_end, statement, statement_sha256, "
                                "doc FROM formal_declarations WHERE library=? AND commit_sha=? AND name=?",
                                [library, commit, name]).fetchone()
        if row is None:
            raise MathematicsError("not_found", "declaration not in this snapshot")
        result = self._declaration_row(snapshot, row)
        subject = f"{library}@{commit}:{name}"
        result["dependencies"] = self.dependencies(library, commit, name, scopes=scopes)
        result["links"] = self.links("declaration", subject, scopes=scopes)["links"]
        return result

    def dependencies(self, library: str, commit: str, name: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        self._snapshot(library, commit)
        out = self.conn.execute("SELECT to_name, kind, method FROM formal_dependencies WHERE library=? AND "
                                "commit_sha=? AND from_name=? ORDER BY kind, to_name", [library, commit, name]).fetchall()
        into = self.conn.execute("SELECT from_name, kind, method FROM formal_dependencies WHERE library=? AND "
                                 "commit_sha=? AND to_name=? ORDER BY kind, from_name", [library, commit, name]).fetchall()
        return {"library": library, "commit": commit, "name": name,
                "uses": [{"name": r[0], "kind": r[1], "method": r[2]} for r in out],
                "used_by": [{"name": r[0], "kind": r[1], "method": r[2]} for r in into],
                "notice": "module-import edges are explicit; lexical-reference edges come from statement tokens and "
                          "are not elaborated proof dependencies."}

    def compare_snapshots(self, library: str, from_commit: str, to_commit: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        self._snapshot(library, from_commit)
        self._snapshot(library, to_commit)

        def load(commit):
            rows = self.conn.execute("SELECT name, statement_sha256, statement, path, module FROM formal_declarations "
                                     "WHERE library=? AND commit_sha=?", [library, commit]).fetchall()
            return {r[0]: r for r in rows}

        def imports(commit):
            return {r[0]: set(json.loads(r[1])) for r in self.conn.execute(
                "SELECT module, imports_json FROM formal_files WHERE library=? AND commit_sha=?",
                [library, commit]).fetchall()}

        old, new = load(from_commit), load(to_commit)
        removed = sorted(set(old) - set(new))
        added = sorted(set(new) - set(old))
        changed = sorted(n for n in set(old) & set(new) if old[n][1] != new[n][1])
        renames = []
        for gone in removed:
            left = _statement_tokens(old[gone][2]) - _name_tokens(gone)
            best = max(((_jaccard(left, _statement_tokens(new[a][2]) - _name_tokens(a)), a) for a in added
                        if new[a][4] == old[gone][4]), default=(0.0, None))
            if best[1] and best[0] >= 0.3:
                renames.append({"from": gone, "to": best[1], "similarity": round(best[0], 3),
                                "basis": "statement-token similarity within the same module (candidate)"})
        old_imports, new_imports = imports(from_commit), imports(to_commit)
        import_changes = []
        for module in sorted(set(old_imports) | set(new_imports)):
            before, after = old_imports.get(module, set()), new_imports.get(module, set())
            if before != after:
                import_changes.append({"module": module, "added": sorted(after - before),
                                       "removed": sorted(before - after)})
        return {"contract": DIFF_CONTRACT, "library": library, "from_commit": from_commit, "to_commit": to_commit,
                "added": added, "removed": removed, "changed": changed, "rename_candidates": renames,
                "import_changes": import_changes,
                "unchanged": len(set(old) & set(new)) - len(changed)}

    def export_references(self, library: str, commit: str, names: Sequence[str], *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        snapshot = self._snapshot(library, commit)
        references = []
        for name in sorted(set(names)):
            decl = self.declaration(library, name, commit, scopes=scopes)
            references.append({k: decl[k] for k in ("name", "kind", "path", "line_start", "line_end",
                                                    "statement_sha256", "permalink")})
        body = {"contract": REFERENCES_CONTRACT, "library": library, "repository": snapshot["repository"],
                "commit": commit, "system": snapshot["system"], "license": snapshot["license"],
                "references": references}
        return {**body, "bundle_sha256": _digest(body)}

    # --------------------------------------------------------------- objects

    def record_object(self, kind: str, source_kind: str, source_ref: Mapping[str, Any], exact_text: str, *,
                      method: str, confidence: float | None, scopes, principal_id: str,
                      alias_of: str | None = None, notation: str | None = None) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if kind not in OBJECT_KINDS or not str(exact_text).strip():
            raise MathematicsError("invalid_object", f"kind must be one of {OBJECT_KINDS} with exact source text")
        if source_kind not in {"formal", "text", "oeis", "manual"}:
            raise MathematicsError("invalid_object", "source_kind is formal, text, oeis or manual")
        if kind == "notation-alias" and not (alias_of and notation):
            raise MathematicsError("invalid_object", "a notation alias names the object it aliases and its notation")
        if confidence is not None and not 0 <= float(confidence) <= 1:
            raise MathematicsError("invalid_object", "confidence is between 0 and 1")
        object_id = "mobj:" + _digest({"kind": kind, "source_ref": source_ref, "text": exact_text,
                                       "alias_of": alias_of})[:24]
        self.conn.execute("INSERT OR IGNORE INTO math_objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [object_id, kind, source_kind, _canonical(source_ref), exact_text,
                           normalize_notation(exact_text), method, confidence, alias_of, notation, principal_id,
                           self.now()])
        return self.object(object_id, scopes={READ_SCOPE})

    def object(self, object_id: str, *, scopes) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        row = self.conn.execute("SELECT * FROM math_objects WHERE object_id=?", [object_id]).fetchone()
        if row is None:
            raise MathematicsError("not_found", "object not recorded")
        keys = ("object_id", "kind", "source_kind", "source_ref", "exact_text", "normalized_text", "method",
                "confidence", "alias_of", "notation", "created_by", "created_at_ms")
        value = dict(zip(keys, row, strict=True))
        value["source_ref"] = json.loads(value["source_ref"])
        value["status"] = "parsed" if value["source_kind"] == "formal" else "candidate" \
            if value["method"] == TEXT_METHOD else "recorded"
        value["normalized_text_use"] = "search only; not a claim of semantic equivalence"
        value["aliases"] = [r[0] for r in self.conn.execute("SELECT object_id FROM math_objects WHERE alias_of=?",
                                                              [object_id]).fetchall()]
        return value

    def extract_text_objects(self, provider: str, provider_id: str, *, scopes, principal_id: str) -> dict[str, Any]:
        """Candidate expressions ($...$) and named results ("X's theorem") from literature text fields."""

        _require(scopes, WRITE_SCOPE)
        record = self.literature(provider, provider_id, scopes={READ_SCOPE})["record"]
        created = []
        for field in ("title", "abstract"):
            text = record.get(field) or ""
            for match in re.finditer(r"\$([^$]+)\$", text):
                created.append(self.record_object(
                    "expression", "text", {"provider": provider, "provider_id": provider_id, "field": field,
                                           "start": match.start(1), "end": match.end(1)},
                    match.group(1), method=TEXT_METHOD, confidence=0.9, scopes=scopes,
                    principal_id=principal_id)["object_id"])
            for match in re.finditer(r"\b([A-Z][\w\-]+(?:['’]s)?)\s+(theorem|lemma|conjecture|identity|formula)\b",
                                     text, re.IGNORECASE):
                created.append(self.record_object(
                    "theorem", "text", {"provider": provider, "provider_id": provider_id, "field": field,
                                        "start": match.start(), "end": match.end()},
                    match.group(0), method=TEXT_METHOD, confidence=0.6, scopes=scopes,
                    principal_id=principal_id)["object_id"])
        return {"objects": sorted(set(created)), "method": TEXT_METHOD,
                "notice": "text-extracted objects are candidates with exact spans, distinct from formal declarations"}

    # ----------------------------------------------------------------- links

    def _link(self, subject: tuple[str, str], obj: tuple[str, str], basis: str, evidence: Mapping[str, Any],
              confidence: float | None) -> str | None:
        if subject == obj:
            return None
        link_id = "mlink:" + _digest({"s": subject, "o": obj, "basis": basis})[:24]
        state = "explicit" if basis in EXPLICIT else "candidate"
        inserted = self.conn.execute("INSERT OR IGNORE INTO math_links VALUES (?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL) "
                                     "RETURNING link_id", [link_id, *subject, *obj, basis, _canonical(evidence),
                                                           confidence, state, self.now()]).fetchall()
        return link_id if inserted else None

    def _identifier_subjects(self, scheme: str, value: str) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT subject_id FROM math_identifiers WHERE subject_kind="
                                                "'literature' AND scheme=? AND value=?",
                                                [scheme, str(value).casefold()]).fetchall()]

    def _current_literature(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for provider, provider_id in self.conn.execute("SELECT DISTINCT provider, provider_id FROM math_literature "
                                                       "ORDER BY 1, 2").fetchall():
            out[f"{provider}:{provider_id}"] = self._latest_literature(provider, provider_id)
        return out

    def _latest_literature(self, provider: str, provider_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT record_json FROM math_literature WHERE provider=? AND provider_id=? "
                                "ORDER BY observed_at_ms DESC, record_sha LIMIT 1", [provider, provider_id]).fetchone()
        return json.loads(row[0])

    def _current_sequences(self) -> dict[str, dict[str, Any]]:
        return {a: self._latest("math_sequences", "a_number", a) for (a,) in self.conn.execute(
            "SELECT DISTINCT a_number FROM math_sequences ORDER BY 1").fetchall()}

    def _latest_declarations(self) -> list[tuple[str, dict[str, Any]]]:
        rows = self.conn.execute(
            "SELECT s.library, s.commit_sha FROM formal_snapshots s WHERE s.observed_at_ms = (SELECT max(observed_at_ms) "
            "FROM formal_snapshots t WHERE t.library=s.library) ORDER BY 1").fetchall()
        out = []
        for library, commit in rows:
            snapshot = self._snapshot(library, commit)
            for row in self.conn.execute("SELECT name, kind, path, module, line_start, line_end, statement, "
                                         "statement_sha256, doc FROM formal_declarations WHERE library=? AND "
                                         "commit_sha=? ORDER BY name", [library, commit]).fetchall():
                out.append((f"{library}@{commit}:{row[0]}", self._declaration_row(snapshot, row)))
        return out

    def propose_links(self, *, scopes, principal_id: str, max_candidates_per_subject: int = 5) -> dict[str, Any]:
        """Explicit links from source citations/identifiers; bounded name/statement/expression candidates."""

        _require(scopes, WRITE_SCOPE)
        del principal_id
        explicit, candidates = [], []
        literature = self._current_literature()
        for subject, record in literature.items():
            for ref in record.get("references") or []:
                for scheme in ("zbmath", "doi"):
                    for target in self._identifier_subjects(scheme, ref.get(scheme) or ""):
                        link = self._link(("literature", subject), ("literature", target), "explicit-citation",
                                          {"source": subject, "reference": ref, "matched": scheme}, None)
                        explicit += [link] if link else []
        for a_number, record in self._current_sequences().items():
            for scheme, values in dict(record.get("cited_identifiers") or {}).items():
                for value in values:
                    for target in self._identifier_subjects(scheme, value):
                        link = self._link(("sequence", a_number), ("literature", target), "explicit-citation",
                                          {"source": f"oeis:{a_number}", "identifier": {scheme: value},
                                           "revision": record.get("revision")}, None)
                        explicit += [link] if link else []
        declarations = self._latest_declarations()
        for subject, decl in declarations:
            for scheme, pattern in (("doi", r"10\.\d{4,9}/[^\s)\]]+"), ("arxiv", r"arXiv:(\d{4}\.\d{4,5})")):
                for value in re.findall(pattern, decl.get("doc") or ""):
                    for target in self._identifier_subjects(scheme, value):
                        link = self._link(("declaration", subject), ("literature", target), "explicit-identifier",
                                          {"doc": decl["doc"], scheme: value, "commit": decl["commit"]}, None)
                        explicit += [link] if link else []
        # Name candidates: distinctive title words that appear as declaration name tokens.
        for subject, record in literature.items():
            title_words = _words(record.get("title") or "")
            scored = []
            for decl_id, decl in declarations:
                shared = {w for w in title_words if any(t == w or t.startswith(w) for t in _name_tokens(decl["name"]))}
                if shared and decl["kind"] in _THEOREM_KINDS | _DEFINITION_KINDS:
                    score = round(min(0.6, 0.2 + 0.1 * len(shared) + (0.1 if decl["kind"] in _THEOREM_KINDS else 0)), 2)
                    scored.append((score, decl_id, sorted(shared)))
            per_library: dict[str, int] = {}
            for score, decl_id, shared in sorted(scored, key=lambda x: (-x[0], x[1])):
                library = decl_id.split("@", 1)[0]
                per_library[library] = per_library.get(library, 0) + 1
                if per_library[library] > max_candidates_per_subject:
                    continue
                link = self._link(("literature", subject), ("declaration", decl_id), "candidate-name",
                                  {"shared_words": shared, "title": record.get("title")}, score)
                candidates += [link] if link else []
        for a_number, record in self._current_sequences().items():
            name_words = _words(record.get("name") or "")
            scored = []
            for decl_id, decl in declarations:
                if decl["kind"] not in _DEFINITION_KINDS:
                    continue
                tokens = _name_tokens(decl["name"])
                shared = sorted({w for w in name_words for t in tokens if t == w or (len(t) >= 3 and w.startswith(t))})
                matched = {t for t in tokens if any(w == t or w.startswith(t) for w in name_words)}
                if shared:
                    scored.append((round(0.3 * len(matched) / len(tokens), 2), decl_id, shared))
            for score, decl_id, shared in sorted(scored, key=lambda x: (-x[0], x[1]))[:max_candidates_per_subject]:
                link = self._link(("sequence", a_number), ("declaration", decl_id), "candidate-name",
                                  {"shared_words": shared, "sequence_name": record.get("name")}, score)
                candidates += [link] if link else []
        # Statement candidates across libraries.
        by_library: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for decl_id, decl in declarations:
            if decl["kind"] in _THEOREM_KINDS:
                by_library.setdefault(decl["library"], []).append((decl_id, decl))
        libraries = sorted(by_library)
        theorem_names = [_name_tokens(d["name"]) for items in by_library.values() for _, d in items]
        frequency: dict[str, int] = {}
        for tokens in theorem_names:
            for token in tokens:
                frequency[token] = frequency.get(token, 0) + 1
        common = {t for t, n in frequency.items() if n >= max(3, len(theorem_names) // 4)}
        for index, left_library in enumerate(libraries):
            for right_library in libraries[index + 1:]:
                for left_id, left in by_library[left_library]:
                    scored = []
                    left_names = _name_tokens(left["name"]) - common
                    left_statement = _statement_tokens(left["statement"]) - _name_tokens(left["name"])
                    for right_id, right in by_library[right_library]:
                        shared = left_names & (_name_tokens(right["name"]) - common)
                        if not shared:
                            continue
                        names = _jaccard(left_names, _name_tokens(right["name"]) - common)
                        statements = _jaccard(left_statement,
                                              _statement_tokens(right["statement"]) - _name_tokens(right["name"]))
                        score = round(min(0.5, 0.5 * names + 0.5 * statements), 2)
                        if score >= 0.3:
                            scored.append((score, right_id, sorted(shared), round(statements, 2)))
                    for score, right_id, shared, statements in sorted(scored, key=lambda x: (-x[0], x[1]))[:2]:
                        right = dict(declarations)[right_id]
                        link = self._link(("declaration", left_id), ("declaration", right_id), "candidate-statement",
                                          {"shared_name_tokens": shared, "statement_token_overlap": statements,
                                           "left_statement": left["statement"][:300],
                                           "right_statement": right["statement"][:300]}, score)
                        candidates += [link] if link else []
        expressions: dict[str, list[str]] = {}
        for object_id, normalized in self.conn.execute("SELECT object_id, normalized_text FROM math_objects WHERE "
                                                       "kind='expression'").fetchall():
            expressions.setdefault(normalized, []).append(object_id)
        for normalized, ids in sorted(expressions.items()):
            for left, right in zip(sorted(ids), sorted(ids)[1:], strict=False):
                link = self._link(("object", left), ("object", right), "candidate-expression",
                                  {"normalized_text": normalized}, 0.5)
                candidates += [link] if link else []
        return {"explicit": explicit, "candidates": candidates,
                "notice": "candidates are suggestions from names, statements or normalized expressions; they are "
                          "not equivalence until a reviewer accepts them, and remain reviewed candidates after."}

    def review_link(self, link_id: str, decision: str, *, scopes, principal_id: str, note: str = "") -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        if decision not in {"accepted", "rejected"}:
            raise MathematicsError("invalid_decision", "decision is accepted or rejected")
        row = self.conn.execute("SELECT state FROM math_links WHERE link_id=?", [link_id]).fetchone()
        if row is None:
            raise MathematicsError("not_found", "link not found")
        if row[0] == "explicit":
            raise MathematicsError("not_reviewable", "explicit links come from sources; correct the source instead")
        self.conn.execute("UPDATE math_links SET state=?, reviewed_by=?, review_note=?, reviewed_at_ms=? "
                          "WHERE link_id=?", [decision, principal_id, note, self.now(), link_id])
        return self._link_row(self.conn.execute("SELECT * FROM math_links WHERE link_id=?", [link_id]).fetchone())

    @staticmethod
    def _link_row(row: Sequence[Any]) -> dict[str, Any]:
        (link_id, sk, sid, ok, oid, basis, evidence, confidence, state, reviewer, note, created, reviewed) = row
        return {"contract": LINK_CONTRACT, "link_id": link_id, "subject": {"kind": sk, "id": sid},
                "object": {"kind": ok, "id": oid}, "basis": basis, "evidence": json.loads(evidence),
                "confidence": confidence, "state": state, "reviewed_by": reviewer, "review_note": note,
                "created_at_ms": created, "reviewed_at_ms": reviewed,
                "asserts_identity": False}

    def links(self, kind: str, subject_id: str, *, scopes, include_rejected: bool = False) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute("SELECT * FROM math_links WHERE (subject_kind=? AND subject_id=?) OR (object_kind=? "
                                 "AND object_id=?) ORDER BY CASE state WHEN 'explicit' THEN 0 WHEN 'accepted' THEN 1 "
                                 "WHEN 'candidate' THEN 2 ELSE 3 END, confidence DESC NULLS LAST, basis, link_id",
                                 [kind, subject_id, kind, subject_id]).fetchall()
        return {"links": [self._link_row(r) for r in rows if include_rejected or r[8] != "rejected"]}

    # ---------------------------------------------------------------- search

    def search(self, query: str, *, scopes, kinds: Sequence[str] | None = None, limit: int = 20) -> dict[str, Any]:
        """Literature, sequences and (latest-snapshot) formal declarations with sources, revisions and links.

        ``limit`` applies per kind; results are grouped literature, sequences, declarations.
        """

        _require(scopes, READ_SCOPE)
        from src.ingestion.math_sources import PROVIDER_CONTRACTS

        kinds = set(kinds or ("literature", "sequence", "declaration"))
        words = _words(query)
        terms = [t.strip() for t in query.split(",")] if re.fullmatch(r"\s*-?\d+(\s*,\s*-?\d+){2,}\s*", query) else []
        a_number = query.strip().upper() if re.fullmatch(r"\s*A\d{6}\s*", query) else None
        results = []
        if "literature" in kinds:
            for subject, record in self._current_literature().items():
                text_words = _words(" ".join([record.get("title") or "", " ".join(record.get("authors") or []),
                                              " ".join(record.get("msc") or [])]))
                hits = words & text_words or ({query.strip()} & set(record.get("msc") or []))
                if hits:
                    results.append({"kind": "literature", "id": subject, "score": len(hits), "record": record,
                                    "revision": record.get("revision"),
                                    "access": PROVIDER_CONTRACTS["zbmath-open"]["terms"]})
        if "sequence" in kinds:
            for number, record in self._current_sequences().items():
                joined = ",".join(record.get("terms") or [])
                hit = (a_number == number) or bool(terms and ",".join(terms) in joined) or bool(
                    words & _words(record.get("name") or ""))
                if hit:
                    results.append({"kind": "sequence", "id": number, "score": 3 if a_number else 2 if terms else 1,
                                    "record": record, "revision": record.get("revision"),
                                    "access": PROVIDER_CONTRACTS["oeis"]["terms"]})
        if "declaration" in kinds:
            for decl_id, decl in self._latest_declarations():
                tokens = _name_tokens(decl["name"])
                hits = {w for w in words if any(t == w or (len(t) >= 3 and w.startswith(t)) for t in tokens)}
                if hits:
                    results.append({"kind": "declaration", "id": decl_id, "score": len(hits) + (
                        0.5 if decl["kind"] in _THEOREM_KINDS else 0), "record": decl, "revision": decl["commit"],
                        "access": f"{decl['license']} ({decl['repository']} at {decl['commit'][:12]})"})
        order = {"literature": 0, "sequence": 1, "declaration": 2}
        results.sort(key=lambda r: (order[r["kind"]], -r["score"], r["id"]))
        cap, seen, page = max(1, min(int(limit), 100)), dict.fromkeys(order, 0), []
        for item in results:  # the limit applies per kind so no source type crowds out the others
            seen[item["kind"]] += 1
            if seen[item["kind"]] <= cap:
                page.append(item)
        for item in page:
            item["links"] = [link for link in self.links(item["kind"], item["id"], scopes=scopes)["links"]]
        return {"contract": SEARCH_CONTRACT, "query": query, "total": len(results), "results": page,
                "notice": "Formal results pin the library commit; links show whether they are explicit or reviewed "
                          "or unreviewed candidates. A paper theorem and a formal theorem are never merged."}


class MathProjector:
    def __init__(self, conn: Any) -> None:
        self.store = MathStore(conn)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, source, documents, page_receipt, principal_id
        return self.store.observe_page(records, run_id=run_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, source, principal_id
        return {"status": status}
