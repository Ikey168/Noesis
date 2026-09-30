"""Labour series linked to definitions, methodology documents and demographic denominators by citation (#2219, LB08).

**References.** Every reference a series' source document states - ILO resolutions (ICLS), EU acts (CELEX),
Eurostat ESMS pages, OECD methodology pages, BLS Handbook of Methods chapters - is kept exactly as published and
resolved only by its exact identifier:

* a URL (the reference's ``url``, or an identifier that is a URL) resolves to an acquired document whose ``url`` or
  ``canonical_url`` is exactly that URL (the shared ``documents`` table);
* a CELEX number resolves to a Legal work through :class:`src.kb.legal.LegalStore` (normalised with
  :func:`src.kb.demographics_links.act_identifier`), as the demographics links do.

A reference that resolves to nothing (or to several targets) stays an ``unresolved`` citation with the published
string; nothing is ever linked by indicator-name or title similarity.

**Denominators.** A link to an Economics demographic series (e.g. a population denominator) is created only when
the source document names the denominator (``denominator``: provider, series code, geography) and a demographic
series with exactly that provider, series code and geography is held; otherwise the named denominator is an
unresolved citation. No rate is computed from a denominator.

**Comparability.** :meth:`LabourLinks.propose_definition_notes` proposes typed comparability notes between series of
the same concept for the same place (by the same area code or an accepted place mapping) whose recorded
definitions differ in basis, age bounds or survey coverage, citing both definitions; breaks stated by a source are
already source-stated notes attached to their periods. Notes follow :mod:`src.kb.demographics_comparability`
through :class:`src.kb.labour_statistics.LabourComparability`. No derived indicator is produced.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from itertools import combinations
from typing import Any

from src.kb.labour_statistics import (
    READ_SCOPE,
    WRITE_SCOPE,
    LabourComparability,
    LabourError,
    LabourStore,
    authorize,
    canonical,
    digest,
    table_exists,
)

CONTRACT = "noesis-labour-link-v1"
LEGAL_READ = "knowledge:legal:read"
DEMOGRAPHICS_READ = "knowledge:demographics:read"
NO_DERIVATION = "A citation link records what the source states; no indicator is derived and no rate is computed."
_DDL = """
CREATE TABLE IF NOT EXISTS labour_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, kind TEXT NOT NULL,
  reference_json TEXT NOT NULL, target_json TEXT, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def _url(reference: dict[str, Any]) -> str | None:
    for value in (reference.get("url"), reference.get("identifier")):
        if isinstance(value, str) and value.startswith(("https://", "http://")):
            return value
    return None


class LabourLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = LabourStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _put(self, namespace, series_id, kind, reference, target, state, evidence, principal_id):
        link_id = "lb-link:" + digest([namespace, series_id, kind, reference, target, state])[:24]
        if self.conn.execute("SELECT 1 FROM labour_links WHERE namespace=? AND link_id=?",
                             [namespace, link_id]).fetchone():
            return link_id, False
        now = self.now()
        self.conn.execute(
            "INSERT INTO labour_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, link_id, series_id, kind, canonical(reference), None if target is None else canonical(target),
             state, canonical(evidence), canonical([{"state": state, "by": principal_id, "at_ms": now}]),
             principal_id, now],
        )
        return link_id, True

    def _documents(self, url: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "documents"):
            return []
        return [{"kind": "document", "id": r[0], "title": r[1], "url": r[2]} for r in self.conn.execute(
            "SELECT document_id, title, url FROM documents WHERE url=? OR canonical_url=? ORDER BY document_id",
            [url, url]).fetchall()]

    def _legal(self, reference: dict[str, Any], legal_namespace: str, scopes: set[str]) -> tuple[list, str]:
        if not table_exists(self.conn, "legal_works"):
            return [], "provider_absent"
        from src.kb.demographics_links import act_identifier
        from src.kb.legal import LegalError, LegalStore

        if LEGAL_READ not in scopes and "operator" not in scopes:
            raise LabourError("unauthorized", f"{LEGAL_READ} is required to resolve acts")
        try:
            found = LegalStore(self.conn, initialize=False).lookup(
                legal_namespace, scopes=scopes, identifier=act_identifier("celex", reference["identifier"]),
                jurisdiction="EU")
        except LegalError as exc:
            raise LabourError(exc.code, str(exc)) from exc
        return [{"kind": "legal-work", "namespace": legal_namespace, "id": w["work_id"], "title": w.get("title")}
                for w in found["works"]], found["status"]

    def link_references(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                        legal_namespace: str = "global") -> dict[str, Any]:
        """Resolve every stated reference by its exact identifier; unresolvable references stay as citations."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        linked, unresolved = [], []
        for series in self.store.find_series(namespace):
            for reference in series["references"]:
                reference = dict(reference)
                url = _url(reference)
                if reference.get("scheme") == "celex":
                    targets, status = self._legal(reference, legal_namespace, scopes)
                elif url:
                    targets, status = self._documents(url), "looked_up_by_exact_url"
                else:
                    targets, status = [], "no_resolvable_identifier"
                evidence = {"stated_in": series["dataflow"], "lookup": status, "note": NO_DERIVATION}
                if len(targets) == 1:
                    link_id, new = self._put(namespace, series["series_id"], "reference", reference, targets[0],
                                             "linked", evidence, principal_id)
                    (linked if new else []).append(link_id)
                else:
                    evidence["reason"] = "several targets carry this identifier" if targets else (
                        "the cited document is not held; kept as the published citation")
                    link_id, new = self._put(namespace, series["series_id"], "reference", reference, None,
                                             "unresolved", evidence, principal_id)
                    (unresolved if new else []).append(link_id)
        return {"linked": linked, "unresolved": unresolved}

    def link_denominators(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                          demographic_namespace: str = "global") -> dict[str, Any]:
        """Link series to the demographic series their source names as denominator, by exact identifiers only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        linked, unresolved = [], []
        held = table_exists(self.conn, "demographic_series")
        for series in self.store.find_series(namespace):
            named = series.get("denominator")
            if not named:
                continue
            if held and DEMOGRAPHICS_READ not in scopes and "operator" not in scopes:
                raise LabourError("unauthorized", f"{DEMOGRAPHICS_READ} is required to resolve denominators")
            rows = self.conn.execute(
                "SELECT series_id, indicator, geography_code FROM demographic_series WHERE namespace=? AND provider=? "
                "AND series_code=? AND geography_code=? ORDER BY series_id",
                [demographic_namespace, str(named.get("provider")), str(named.get("series_code")),
                 str(named.get("geography_code"))],
            ).fetchall() if held else []
            evidence = {"named_by_source": named, "note": NO_DERIVATION}
            if len(rows) == 1:
                target = {"kind": "demographic-series", "namespace": demographic_namespace, "id": rows[0][0],
                          "indicator": rows[0][1], "geography_code": rows[0][2]}
                link_id, new = self._put(namespace, series["series_id"], "denominator", named, target, "linked",
                                         evidence, principal_id)
                (linked if new else []).append(link_id)
            else:
                evidence["reason"] = ("the Economics demographics store is not held" if not held else
                                      "several demographic series match" if rows else
                                      "the named denominator series is not held")
                link_id, new = self._put(namespace, series["series_id"], "denominator", named, None, "unresolved",
                                         evidence, principal_id)
                (unresolved if new else []).append(link_id)
        return {"linked": linked, "unresolved": unresolved}

    def propose_definition_notes(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Propose comparability notes between same-concept series of one place whose definitions differ."""
        from src.kb.labour_identity import LabourIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        identity = LabourIdentity(self.conn, initialize=True)
        comparability = LabourComparability(self.conn, now=self.now, initialize=False)

        def place(series):
            accepted = identity.place_for_area(namespace, series["area"]["scheme"], series["area"]["code"])
            if accepted and accepted["state"] == "accepted":
                return accepted["target"]["place_id"]
            return f"{series['area']['scheme']}:{series['area']['code']}"

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for series in self.store.find_series(namespace):
            if series["sector"] or series["occupation"]:
                continue
            groups.setdefault((series["indicator"]["concept"], place(series)), []).append(series)
        created = []
        for (_concept, _place), members in sorted(groups.items()):
            for left, right in combinations(members, 2):
                ld = self.store.definition(namespace, left["current_definition_id"])["content"]
                rd = self.store.definition(namespace, right["current_definition_id"])["content"]
                for field, relation in (("basis", "different_definition_basis"),
                                        ("age_bounds", "different_age_bounds"),
                                        ("coverage", "different_survey_coverage")):
                    if ld.get(field) and rd.get(field) and ld[field] != rd[field]:
                        statement = (f"{field.replace('_', ' ')}: {ld[field]} ({left['provider']}, "
                                     f"{left['native_key']}) against {rd[field]} ({right['provider']}, "
                                     f"{right['native_key']}), as each source states it")
                        note = comparability.record(
                            namespace, {"series_id": left["series_id"]}, {"series_id": right["series_id"]},
                            relation, statement, principal_id=principal_id, scopes=scopes)
                        created.append(note["note_id"])
        return {"proposed": sorted(set(created))}

    def link(self, namespace: str, link_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT link_id, series_id, kind, reference_json, target_json, state, evidence_json, history_json, "
            "created_by, created_at_ms FROM labour_links WHERE namespace=? AND link_id=?", [namespace, link_id]
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "link is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "link_id": row[0], "series_id": row[1],
                "kind": row[2], "reference": json.loads(row[3]), "target": None if row[4] is None else json.loads(
                    row[4]), "state": row[5], "evidence": json.loads(row[6]), "history": json.loads(row[7]),
                "created_by": row[8], "created_at_ms": row[9]}

    def links(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None,
              kind: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "labour_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM labour_links WHERE namespace=? AND (? IS NULL OR series_id=?) AND "
            "(? IS NULL OR kind=?) AND (? IS NULL OR state=?) ORDER BY series_id, kind, link_id",
            [namespace, series_id, series_id, kind, kind, state, state]).fetchall()
        return [self.link(namespace, r[0]) for r in rows]


__all__ = ["CONTRACT", "LabourLinks", "NO_DERIVATION"]
