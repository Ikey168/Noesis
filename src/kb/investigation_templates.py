"""Versioned investigation templates over the existing project lifecycle."""

import json
import re
from string import Template

from src.kb.research_projects import (
    ResearchProjectError,
    ResearchProjectStore,
    _hash,
    _json,
    _strings,
)

CONTRACT = "noesis-investigation-template-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS investigation_templates(
 template_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 revision BIGINT NOT NULL, request_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS investigation_template_revisions(
 template_id TEXT NOT NULL, revision BIGINT NOT NULL, state_json TEXT NOT NULL,
 PRIMARY KEY(template_id,revision));
"""


def _validate(value):
    fields = {
        "name",
        "description",
        "parameters",
        "questions",
        "success_criteria",
        "scope",
        "source_packs",
        "report_outline",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or len(_json(value).encode()) > 65536
    ):
        raise ResearchProjectError(
            "invalid_template", "a complete template of at most 64 KiB is required"
        )
    for key in ("name", "description"):
        if not isinstance(value[key], str) or not 1 <= len(value[key].strip()) <= 2000:
            raise ResearchProjectError(
                "invalid_template", "bounded name and description required"
            )
    params = value["parameters"]
    if not isinstance(params, dict) or len(params) > 30:
        raise ResearchProjectError(
            "invalid_template", "at most thirty named parameter descriptions required"
        )
    for key, description in params.items():
        if (
            not re.fullmatch("[a-z][a-z0-9_]{0,63}", key)
            or not isinstance(description, str)
            or not 1 <= len(description) <= 500
        ):
            raise ResearchProjectError(
                "invalid_template", "invalid parameter name or description"
            )
    for key in ("questions", "success_criteria", "report_outline"):
        _strings(value[key], key, required=True)
        if len(value[key]) > 50 or any(len(x) > 2000 for x in value[key]):
            raise ResearchProjectError(
                "invalid_template", "template text exceeds limits"
            )
        for text in value[key]:
            template = Template(text)
            if not template.is_valid() or set(template.get_identifiers()) - set(params):
                raise ResearchProjectError(
                    "invalid_template", "undeclared or malformed parameter placeholder"
                )
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"domains", "namespaces"}:
        raise ResearchProjectError(
            "invalid_template", "explicit domain/namespace scope required"
        )
    for key in scope:
        _strings(scope[key], key)
    if not any(scope.values()):
        raise ResearchProjectError(
            "invalid_template", "nonempty research scope required"
        )
    packs = value["source_packs"]
    if not isinstance(packs, list) or not 1 <= len(packs) <= 20:
        raise ResearchProjectError(
            "invalid_template", "one to twenty pinned source packs required"
        )
    for pack in packs:
        if (
            not isinstance(pack, dict)
            or set(pack) != {"pack_id", "version"}
            or any(
                not isinstance(v, str) or not 1 <= len(v) <= 200 for v in pack.values()
            )
        ):
            raise ResearchProjectError(
                "invalid_template", "source packs require stable identity and version"
            )
    if len({_json(p) for p in packs}) != len(packs):
        raise ResearchProjectError("invalid_template", "duplicate source pack")
    return json.loads(_json(value))


class InvestigationTemplateStore:
    def __init__(self, conn, *, initialize=True):
        self.conn = conn
        self.projects = ResearchProjectStore(conn, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _state(self, namespace, template_id, revision=None):
        row = self.conn.execute(
            """SELECT r.state_json FROM investigation_templates t
            JOIN investigation_template_revisions r ON r.template_id=t.template_id
            WHERE t.namespace=? AND t.template_id=? AND r.revision=coalesce(?,t.revision)""",
            [namespace, template_id, revision],
        ).fetchone()
        if not row:
            raise ResearchProjectError(
                "template_unavailable", "template revision is unavailable"
            )
        return json.loads(row[0])

    def _auth(self, state, principal_id, scopes, write=False):
        self.projects._authorize(
            {**state, "scope": state["definition"]["scope"]},
            principal_id,
            scopes,
            write=write,
        )

    def inspect(self, namespace, template_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, template_id)
        self._auth(current, principal_id, scopes)
        state = (
            self._state(namespace, template_id, revision)
            if revision is not None
            else current
        )
        self._auth(state, principal_id, scopes)
        return state

    def list(self, namespace, *, principal_id, scopes, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ResearchProjectError("invalid_limit", "limit must be 1..100")
        self.projects._authorize(
            {
                "namespace": namespace,
                "owner": principal_id,
                "scope": {"namespaces": [], "domains": []},
            },
            principal_id,
            scopes,
        )
        rows = self.conn.execute(
            "SELECT template_id FROM investigation_templates WHERE namespace=? AND (owner=? OR ?) ORDER BY template_id LIMIT ?",
            [namespace, principal_id, "operator" in scopes, limit],
        ).fetchall()
        result = []
        for (identity,) in rows:
            try:
                result.append(
                    self.inspect(
                        namespace, identity, principal_id=principal_id, scopes=scopes
                    )
                )
            except ResearchProjectError as exc:
                if exc.code != "unauthorized":
                    raise
        return {"templates": result}

    def create(self, namespace, request_key, definition, *, principal_id, scopes):
        if (
            not isinstance(namespace, str)
            or not namespace
            or not isinstance(request_key, str)
            or not 1 <= len(request_key) <= 1000
        ):
            raise ResearchProjectError(
                "invalid_request", "namespace and bounded request key required"
            )
        definition = _validate(definition)
        identity = "template:" + _hash([namespace, principal_id, request_key])[:32]
        state = {
            "contract": CONTRACT,
            "template_id": identity,
            "namespace": namespace,
            "owner": principal_id,
            "revision": 1,
            "status": "active",
            "definition": definition,
        }
        self._auth(state, principal_id, scopes, True)
        digest = _hash(state)
        old = self.conn.execute(
            "SELECT request_hash FROM investigation_templates WHERE template_id=?",
            [identity],
        ).fetchone()
        if old:
            if old[0] != digest:
                raise ResearchProjectError(
                    "idempotency_conflict", "template request key reused"
                )
            return self.inspect(
                namespace, identity, principal_id=principal_id, scopes=scopes
            )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO investigation_templates VALUES (?,?,?,1,?)",
                [identity, namespace, principal_id, digest],
            )
            self.conn.execute(
                "INSERT INTO investigation_template_revisions VALUES (?,1,?)",
                [identity, _json(state)],
            )
            self.conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - _abort always rolls back and raises
            self.projects._abort(exc)
        return state

    def revise(
        self,
        namespace,
        template_id,
        expected_revision,
        *,
        principal_id,
        scopes,
        definition=None,
        archive=False,
    ):
        if type(expected_revision) is not int or expected_revision < 1:
            raise ResearchProjectError(
                "revision_conflict", "positive expected revision required"
            )
        self.conn.execute("BEGIN")
        try:
            state = self.inspect(
                namespace, template_id, principal_id=principal_id, scopes=scopes
            )
            self._auth(state, principal_id, scopes, True)
            if state["status"] == "archived":
                raise ResearchProjectError(
                    "template_archived", "archived templates are immutable"
                )
            if definition is not None:
                state["definition"] = _validate(definition)
                self._auth(state, principal_id, scopes, True)
            state.update(
                revision=expected_revision + 1,
                status="archived" if archive else "active",
            )
            row = self.conn.execute(
                "UPDATE investigation_templates SET revision=revision+1 WHERE template_id=? AND revision=? RETURNING revision",
                [template_id, expected_revision],
            ).fetchone()
            if not row:
                raise ResearchProjectError(
                    "revision_conflict", "template changed; inspect current revision"
                )
            self.conn.execute(
                "INSERT INTO investigation_template_revisions VALUES (?,?,?)",
                [template_id, state["revision"], _json(state)],
            )
            self.conn.execute("COMMIT")
        except Exception as exc:  # noqa: BLE001 - _abort always rolls back and raises
            self.projects._abort(exc)
        return state

    def preview(
        self,
        namespace,
        template_id,
        revision,
        parameters,
        *,
        principal_id,
        scopes,
        secret_available=None,
    ):
        state = self.inspect(
            namespace,
            template_id,
            revision=revision,
            principal_id=principal_id,
            scopes=scopes,
        )
        template_status = self._state(namespace, template_id)["status"]
        if type(revision) is not int or revision < 1:
            raise ResearchProjectError(
                "invalid_revision", "pin a positive template revision"
            )
        definition = state["definition"]
        if (
            not isinstance(parameters, dict)
            or set(parameters) != set(definition["parameters"])
            or any(
                not isinstance(v, str) or not 1 <= len(v.strip()) <= 1000
                for v in parameters.values()
            )
        ):
            raise ResearchProjectError(
                "invalid_parameters",
                "supply exactly the declared nonempty text parameters",
            )
        resolved = {
            key: [Template(text).substitute(parameters) for text in definition[key]]
            for key in ("questions", "success_criteria", "report_outline")
        }
        tables = {
            r[0]
            for r in self.conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        packs = []
        for ref in definition["source_packs"]:
            row = (
                self.conn.execute(
                    "SELECT manifest_json FROM source_pack_versions WHERE pack_id=? AND version=?",
                    [ref["pack_id"], ref["version"]],
                ).fetchone()
                if "source_pack_versions" in tables
                else None
            )
            if not row:
                packs.append({**ref, "status": "unavailable", "sources": []})
                continue
            manifest = json.loads(row[0])
            if "operator" not in scopes and any(
                f"domain:{domain}:read" not in scopes for domain in manifest["domains"]
            ):
                raise ResearchProjectError(
                    "unauthorized", "current source-pack domain access required"
                )
            sources = []
            for source in manifest["sources"]:
                auth = source["auth"]
                missing = auth["kind"] == "required-secret" and not (
                    secret_available and secret_available(auth["secret_ref"])
                )
                sources.append(
                    {
                        "source_id": source["source_id"],
                        "credential_status": "missing"
                        if missing
                        else "available_or_not_required",
                    }
                )
            packs.append(
                {
                    **ref,
                    "status": "available",
                    "manifest_sha256": _hash(manifest),
                    "sources": sources,
                    "execution_readiness": "not_checked; run existing acquisition preflight",
                }
            )
        return {
            "template_id": template_id,
            "template_revision": revision,
            "parameters": dict(parameters),
            "scope": definition["scope"],
            **resolved,
            "source_packs": packs,
            "template_status": template_status,
            "can_instantiate": template_status == "active"
            and all(p["status"] == "available" for p in packs),
            "acquisition_started": False,
        }

    def instantiate(
        self,
        namespace,
        template_id,
        revision,
        parameters,
        request_key,
        budget,
        *,
        principal_id,
        scopes,
    ):
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 1000:
            raise ResearchProjectError(
                "invalid_request", "bounded project request key required"
            )
        preview = self.preview(
            namespace,
            template_id,
            revision,
            parameters,
            principal_id=principal_id,
            scopes=scopes,
        )
        project_id = "project:" + _hash([namespace, principal_id, request_key])[:32]
        prior = self.conn.execute(
            "SELECT 1 FROM research_projects WHERE project_id=?", [project_id]
        ).fetchone()
        if preview["template_status"] == "archived" and not prior:
            raise ResearchProjectError(
                "template_archived", "archived template cannot create a new project"
            )
        if any(p["status"] != "available" for p in preview["source_packs"]):
            raise ResearchProjectError(
                "dependency_unavailable",
                "install the pinned source packs before instantiation",
            )
        origin = {
            k: preview[k]
            for k in (
                "template_id",
                "template_revision",
                "parameters",
                "report_outline",
            )
        }
        origin["source_packs"] = [
            {k: p[k] for k in ("pack_id", "version", "manifest_sha256")}
            for p in preview["source_packs"]
        ]
        return self.projects.create(
            namespace,
            request_key,
            questions=preview["questions"],
            success_criteria=preview["success_criteria"],
            scope=preview["scope"],
            budget=budget,
            principal_id=principal_id,
            scopes=scopes,
            origin=origin,
        )
