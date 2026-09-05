"""Versioned authored reports with stable assertion and evidence identities."""

import json
import time

from src.kb.research_projects import _hash, _json

READ_SCOPE = "knowledge:reports:read"
WRITE_SCOPE = "knowledge:reports:write"
CONTRACT = "noesis-authored-report-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS authored_reports(
 report_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 request_hash TEXT NOT NULL,revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS authored_report_revisions(
 report_id TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(report_id,revision));
"""


class ReportError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _text(value, field, limit=100000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ReportError("invalid_report", f"{field} must be nonempty text within {limit} characters")
    return value


def validate_content(content):
    if not isinstance(content, dict) or set(content) != {"title", "sections", "snapshot", "bibliography", "limitations"}:
        raise ReportError("invalid_report", "report requires title, sections, snapshot, bibliography, and limitations")
    _text(content["title"], "title", 1000)
    snapshot = content["snapshot"]
    if not isinstance(snapshot, dict) or set(snapshot) != {"id", "generations"}:
        raise ReportError("invalid_report", "snapshot requires a stable id and namespace generations")
    _text(snapshot["id"], "snapshot id", 1000)
    if not isinstance(snapshot["generations"], dict) or not snapshot["generations"] or len(snapshot["generations"]) > 100:
        raise ReportError("invalid_report", "snapshot requires one to 100 namespace generations")
    for namespace, generation in snapshot["generations"].items():
        _text(namespace, "namespace", 1000)
        if type(generation) is not int or generation < 0:
            raise ReportError("invalid_report", "snapshot generations must be nonnegative integers")
    if not isinstance(content["limitations"], list) or len(content["limitations"]) > 1000:
        raise ReportError("invalid_report", "limitations must be a bounded list")
    for value in content["limitations"]:
        _text(value, "limitation")
    bibliography = content["bibliography"]
    if not isinstance(bibliography, list) or len(bibliography) > 10000:
        raise ReportError("invalid_report", "bibliography must be a bounded list")
    citation_ids = set()
    for item in bibliography:
        if not isinstance(item, dict) or set(item) != {"id", "text"}:
            raise ReportError("invalid_report", "bibliography entries require stable id and authored text")
        _text(item["id"], "citation id", 1000)
        _text(item["text"], "citation text")
        if item["id"] in citation_ids:
            raise ReportError("invalid_report", "duplicate bibliography id")
        citation_ids.add(item["id"])
    if not isinstance(content["sections"], list) or not 1 <= len(content["sections"]) <= 1000:
        raise ReportError("invalid_report", "report requires one to 1000 sections")
    identities = set()
    assertions = 0
    for section in content["sections"]:
        if not isinstance(section, dict) or set(section) != {"id", "title", "assertions"}:
            raise ReportError("invalid_report", "section requires id, title, and assertions")
        for key in ("id", "title"):
            _text(section[key], "section " + key, 1000)
        if section["id"] in identities:
            raise ReportError("invalid_report", "section and assertion ids must be unique")
        identities.add(section["id"])
        if not isinstance(section["assertions"], list):
            raise ReportError("invalid_report", "assertions must be a list")
        assertions += len(section["assertions"])
        if assertions > 10000:
            raise ReportError("invalid_report", "report exceeds 10000 assertions")
        for assertion in section["assertions"]:
            if not isinstance(assertion, dict) or set(assertion) != {"id", "text", "kind", "dependencies", "citations"}:
                raise ReportError("invalid_report", "assertion requires id, text, kind, dependencies, and citations")
            _text(assertion["id"], "assertion id", 1000)
            _text(assertion["text"], "assertion text")
            if assertion["id"] in identities:
                raise ReportError("invalid_report", "section and assertion ids must be unique")
            identities.add(assertion["id"])
            if assertion["kind"] not in {"sourced", "commentary"}:
                raise ReportError("invalid_report", "assertions must explicitly be sourced or commentary")
            if not isinstance(assertion["citations"], list) or len(assertion["citations"]) > 1000 or any(not isinstance(c, str) or c not in citation_ids for c in assertion["citations"]):
                raise ReportError("invalid_report", "assertion cites an unavailable bibliography entry")
            dependencies = assertion["dependencies"]
            if not isinstance(dependencies, list) or len(dependencies) > 1000:
                raise ReportError("invalid_report", "dependencies must be bounded")
            if assertion["kind"] == "sourced" and not dependencies:
                raise ReportError("invalid_report", "sourced assertions require evidence dependencies")
            for dep in dependencies:
                if not isinstance(dep, dict) or set(dep) != {"kind", "id", "revision", "namespace", "locator"}:
                    raise ReportError("invalid_report", "dependency requires kind, id, revision, namespace, locator")
                if dep["kind"] not in {"claim", "calculation", "source", "entity", "artifact"}:
                    raise ReportError("invalid_report", "unsupported dependency kind")
                for key in ("id", "revision", "namespace"):
                    _text(dep[key], "dependency " + key, 1000)
                if not isinstance(dep["locator"], dict) or set(dep["locator"]) - {"document_id", "revision_id", "start", "end", "page", "section"}:
                    raise ReportError("invalid_report", "unsupported dependency locator")
                for key, value in dep["locator"].items():
                    if key in {"start", "end", "page"}:
                        if type(value) is not int or value < 0:
                            raise ReportError("invalid_report", "locator coordinates must be nonnegative integers")
                    else:
                        _text(value, "locator " + key, 1000)
                if dep["locator"].get("end", 0) < dep["locator"].get("start", 0):
                    raise ReportError("invalid_report", "locator end precedes start")
    if len(_json(content).encode()) > 16 * 1024 * 1024:
        raise ReportError("invalid_report", "report exceeds 16 MiB")
    return json.loads(_json(content))


class AuthoredReportStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state, principal_id, scopes, *, write=False):
        if "operator" in scopes and principal_id:
            return
        if not principal_id or (WRITE_SCOPE if write else READ_SCOPE) not in scopes or state["owner"] != principal_id:
            raise ReportError("unauthorized", "current report scope and ownership are required")
        namespace = state["namespace"]
        if f"namespace:{namespace}:write" not in scopes and (write or f"namespace:{namespace}:read" not in scopes):
            raise ReportError("unauthorized", "current report namespace access is required")
        dependencies = set(state["content"]["snapshot"]["generations"])
        dependencies.update(dep["namespace"] for section in state["content"]["sections"] for assertion in section["assertions"] for dep in assertion["dependencies"])
        if any(f"namespace:{ns}:read" not in scopes and f"namespace:{ns}:write" not in scopes for ns in dependencies):
            raise ReportError("unauthorized", "current evidence namespace access is required")

    def _state(self, namespace, report_id, revision=None):
        row = self.conn.execute("""SELECT r.content_json FROM authored_reports p JOIN authored_report_revisions r
            ON r.report_id=p.report_id WHERE p.report_id=? AND p.namespace=? AND r.revision=coalesce(?,p.revision)""",
            [report_id, namespace, revision]).fetchone()
        if not row:
            raise ReportError("report_unavailable", "report revision is unavailable")
        return json.loads(row[0])

    def inspect(self, namespace, report_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, report_id)
        self._authorize(current, principal_id, scopes)
        state = self._state(namespace, report_id, revision) if revision is not None else current
        self._authorize(state, principal_id, scopes)
        return state

    def _abort(self, exc):
        import duckdb
        self.conn.execute("ROLLBACK")
        if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
            raise ReportError("revision_conflict", "concurrent report update; inspect and retry") from exc
        raise exc

    def create(self, namespace, request_key, content, *, principal_id, scopes):
        _text(namespace, "namespace", 1000)
        _text(request_key, "request key", 1000)
        state = {"contract": CONTRACT, "report_id": "report:" + _hash([namespace, principal_id, request_key])[:32],
                 "namespace": namespace, "owner": principal_id, "revision": 1, "content": validate_content(content)}
        self._authorize(state, principal_id, scopes, write=True)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash FROM authored_reports WHERE report_id=?", [state["report_id"]]).fetchone()
        if prior:
            if digest != prior[0]:
                raise ReportError("idempotency_conflict", "request key already identifies a different report")
            current = self._state(namespace, state["report_id"])
            self._authorize(current, principal_id, scopes, write=True)
            return {**current, "idempotent": True}
        state["updated_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO authored_reports VALUES (?,?,?,?,1)", [state["report_id"], namespace, principal_id, digest])
            self.conn.execute("INSERT INTO authored_report_revisions VALUES (?,1,?)", [state["report_id"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def revise(self, namespace, report_id, expected_revision, content, *, principal_id, scopes):
        content = validate_content(content)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, report_id)
            self._authorize(state, principal_id, scopes, write=True)
            state.update(content=content, revision=expected_revision + 1, updated_at_ms=self.now())
            self._authorize(state, principal_id, scopes, write=True)
            changed = self.conn.execute("UPDATE authored_reports SET revision=revision+1 WHERE report_id=? AND revision=? RETURNING revision", [report_id, expected_revision]).fetchone()
            if not changed:
                raise ReportError("revision_conflict", "report changed; inspect the current revision")
            self.conn.execute("INSERT INTO authored_report_revisions VALUES (?,?,?)", [report_id, state["revision"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def export(self, namespace, report_id, *, principal_id, scopes, revision=None):
        state = self.inspect(namespace, report_id, revision=revision, principal_id=principal_id, scopes=scopes)
        content = state["content"]
        lines = ["# " + content["title"], ""]
        for section in content["sections"]:
            lines.extend(["## " + section["title"], ""])
            for assertion in section["assertions"]:
                prefix = "[Author commentary] " if assertion["kind"] == "commentary" else "[Source-linked; support not independently verified] "
                lines.extend([prefix + assertion["text"] + "".join(" [" + c + "]" for c in assertion["citations"]), ""])
        lines.extend(["## Known limitations", "", *["- " + value for value in content["limitations"]], "", "## Bibliography", ""])
        lines.extend("[" + item["id"] + "] " + item["text"] for item in content["bibliography"])
        return {"contract": "noesis-report-export-v1", "report": state, "sha256": _hash(state),
                "markdown": "\n".join(lines), "bibliography": content["bibliography"],
                "limitations": ["Integrity hash is not signer authentication", "Source support and snapshot availability are not certified"]}

    def reopen(self, namespace, request_key, package, *, principal_id, scopes):
        if not isinstance(package, dict) or package.get("contract") != "noesis-report-export-v1" or package.get("sha256") != _hash(package.get("report")):
            raise ReportError("invalid_export", "report export integrity verification failed")
        state = package["report"]
        self._authorize(state, principal_id, scopes)
        return self.create(namespace, request_key, state["content"], principal_id=principal_id, scopes=scopes)
