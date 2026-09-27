"""Budget plans linked to budget acts and legislative dossiers, beneficiaries to procurement award history (B07).

A plan (or supplementary plan) is *linked* only by an explicit citation:

* to a ``legal.works`` act (:class:`src.kb.legal.LegalStore`, CELLAR and national
  publications) when a reference the plan document states - a CELEX number, an
  ELI, a Bundesgesetzblatt or Berlin GVBl reference - equals an identifier of
  exactly one acquired work (``lookup_legal_work``, never another jurisdiction);
* to a legislative dossier (:mod:`src.domains.political.legislative_dossiers`,
  DIP and EUR-Lex procedures) when a stated printed-paper number or EU
  procedure reference equals the canonical key of one of the dossier's stages
  (the normalisation the lobbying feature uses; jurisdictions must match).

Shared words between a plan's label and an act title or dossier stage are
*candidates*; a reviewer accepts or rejects them, and every decision can be
reverted. A stated citation that matches nothing stays ``unresolved`` with the
source string.

Beneficiaries and procurement suppliers are offered as identity candidates
(``procurement.award-history`` parties) through the shared reviewable identity
state machine. Award history is shown as *context* for an accepted match
only: it never shows that a payment was made or that a procedure is open.

Every link records its basis, the plan's source revision, the target revision
and the reviewer or rule.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.public_finance import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    PublicFinanceError,
    PublicFinanceStore,
    authorize,
    canonical,
    digest,
    normalize_identifier,
    normalize_name,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-public-finance-link-v1"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
PROCUREMENT_READ = "knowledge:procurement:read"
STATES = (
    "linked",
    "unresolved",
    "candidate",
    "accepted",
    "rejected",
    "reverted",
    "superseded",
)
LINKING_STATES = ("linked", "accepted")
ACT_SCHEMES = {"celex": "EU", "eli": "EU", "de-bgbl": "DE", "de-be-gvbl": "DE-BE"}
DOSSIER_SCHEMES = ("de-drucksache", "eu-procedure", "celex", "eli")
PROVIDER_JURISDICTION = {
    "bundeshaushalt": "DE",
    "berlin-senfin": "DE-BE",
    "eu-fts": "EU",
    "eurostat-gfs": "EU",
}
AWARD_CONTEXT = (
    "award history of a reviewed identity match, shown as context: it never shows that a payment was made or that "
    "a procedure is open"
)
_STOPWORDS = frozenset(
    {"gesetz", "gesetzes", "entwurf", "eines", "ueber", "über", "regulation", "budget"}
)
_DDL = """
CREATE TABLE IF NOT EXISTS public_finance_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT, target_id TEXT, target_revision TEXT, basis TEXT NOT NULL,
  state TEXT NOT NULL, reference_json TEXT, evidence_json TEXT NOT NULL, rule TEXT, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _tokens(text: Any) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-zäöüß0-9]{4,}", str(text or "").casefold())
        if w not in _STOPWORDS and not w.isdigit()
    }


def _years(text: Any) -> set[str]:
    return set(re.findall(r"\b(\d{4})\b", str(text or "")))


def act_identifier(scheme: str, value: Any) -> str:
    """The act reference as the Legal store keeps identifiers: CELEX upper-case, gazette text with single spaces."""
    text = " ".join(str(value or "").split())
    return text.upper() if scheme == "celex" else text


class PublicFinanceLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = PublicFinanceStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "public_finance_links")

    # ------------------------------------------------------------------ helpers

    def _plan_references(
        self, namespace: str, plan: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        return [dict(r) for r in plan["references"]]

    def _insert(
        self,
        namespace,
        link_id,
        subject_kind,
        subject_id,
        target_kind,
        target_namespace,
        target_id,
        target_revision,
        basis,
        state,
        reference,
        evidence,
        rule,
        principal_id,
    ) -> bool:
        if self.conn.execute(
            "SELECT 1 FROM public_finance_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone():
            return False
        now = self.now()
        self.conn.execute(
            "INSERT INTO public_finance_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                subject_kind,
                subject_id,
                target_kind,
                target_namespace,
                target_id,
                None if target_revision is None else str(target_revision),
                basis,
                state,
                None if reference is None else canonical(reference),
                canonical(evidence),
                rule,
                canonical(
                    [{"state": state, "by": principal_id, "rule": rule, "at_ms": now}]
                ),
                principal_id,
                now,
            ],
        )
        return True

    def _supersede_candidates(
        self, namespace, subject_id, target_kind, target_id, principal_id
    ):
        for (link_id,) in self.conn.execute(
            "SELECT link_id FROM public_finance_links WHERE namespace=? AND subject_id=? AND target_kind=? AND "
            "target_id=? AND state='candidate' ORDER BY link_id",
            [namespace, subject_id, target_kind, target_id],
        ).fetchall():
            # Stronger evidence arrived: an explicit citation now links the pair, so the candidate is superseded.
            self._transition(
                namespace,
                self.link(namespace, link_id, scopes={"operator"}),
                "superseded",
                principal_id,
                "an explicit citation now links this plan",
            )

    # ------------------------------------------------------------------ acts

    def link_acts(
        self,
        namespace: str,
        *,
        legal_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict:
        """Resolve each plan's stated act citations to Legal works by exact identifier; propose title candidates."""
        from src.kb.legal import LegalError, LegalStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        if not table_exists(self.conn, "legal_works"):
            return {"linked": [], "unresolved": [], "candidates": []}
        legal = LegalStore(self.conn, initialize=False)
        linked, unresolved, candidates = [], [], []
        works = self.conn.execute(
            "SELECT work_id, jurisdiction, title FROM legal_works WHERE namespace=? ORDER BY work_id",
            [legal_namespace],
        ).fetchall()
        for plan in self.store.plans(namespace):
            explicit: set[str] = set()
            for reference in self._plan_references(namespace, plan):
                scheme = reference.get("scheme")
                if scheme not in ACT_SCHEMES:
                    continue
                identifier = act_identifier(scheme, reference.get("value"))
                try:
                    found = legal.lookup(
                        legal_namespace,
                        scopes=scopes,
                        identifier=identifier,
                        jurisdiction=ACT_SCHEMES[scheme],
                    )
                except LegalError as exc:
                    raise PublicFinanceError(exc.code, str(exc)) from exc
                revision = reference["stated_in"]["release_id"]
                if found["status"] == "found":
                    work = found["works"][0]
                    explicit.add(work["work_id"])
                    link_id = (
                        "pf-link:"
                        + digest(
                            [
                                namespace,
                                plan["plan_id"],
                                "legal-work",
                                work["work_id"],
                                scheme,
                                identifier,
                                revision,
                            ]
                        )[:24]
                    )
                    if self._insert(
                        namespace,
                        link_id,
                        "plan",
                        plan["plan_id"],
                        "legal-work",
                        legal_namespace,
                        work["work_id"],
                        None,
                        "explicit-citation",
                        "linked",
                        {
                            k: reference[k]
                            for k in ("scheme", "value")
                            if k in reference
                        },
                        {
                            "plan_source_revision": reference["stated_in"],
                            "work_title": work.get("title"),
                            "matched_identifier": identifier,
                        },
                        "exact identifier via lookup_legal_work",
                        principal_id,
                    ):
                        linked.append(link_id)
                    self._supersede_candidates(
                        namespace,
                        plan["plan_id"],
                        "legal-work",
                        work["work_id"],
                        principal_id,
                    )
                else:
                    link_id = (
                        "pf-link:"
                        + digest(
                            [
                                namespace,
                                plan["plan_id"],
                                "legal-work",
                                None,
                                scheme,
                                identifier,
                                revision,
                                found["status"],
                            ]
                        )[:24]
                    )
                    if self._insert(
                        namespace,
                        link_id,
                        "plan",
                        plan["plan_id"],
                        "legal-work",
                        legal_namespace,
                        None,
                        None,
                        "explicit-citation",
                        "unresolved",
                        {
                            k: reference[k]
                            for k in ("scheme", "value")
                            if k in reference
                        },
                        {
                            "plan_source_revision": reference["stated_in"],
                            "lookup_status": found["status"],
                            "note": "the cited act is not acquired (or matches more than one work); kept "
                            "as the source string",
                        },
                        "exact identifier via lookup_legal_work",
                        principal_id,
                    ):
                        unresolved.append(link_id)
            words = _tokens(plan["label"]) | {"haushalt"}
            for work_id, jurisdiction, title in works:
                if work_id in explicit or plan["fiscal_year"] not in _years(title):
                    continue
                if jurisdiction != PROVIDER_JURISDICTION.get(plan["provider"]):
                    continue
                shared = sorted(
                    w
                    for w in words
                    if any(t.startswith(w) or w.startswith(t) for t in _tokens(title))
                )
                if not shared:
                    continue
                link_id = (
                    "pf-candidate:"
                    + digest([namespace, plan["plan_id"], "legal-work", work_id])[:24]
                )
                if self._insert(
                    namespace,
                    link_id,
                    "plan",
                    plan["plan_id"],
                    "legal-work",
                    legal_namespace,
                    work_id,
                    None,
                    "discovery",
                    "candidate",
                    None,
                    {
                        "shared_words": shared,
                        "fiscal_year": plan["fiscal_year"],
                        "work_title": title,
                        "note": "shared words and a year are a discovery candidate, not a link",
                    },
                    "title word overlap",
                    principal_id,
                ):
                    candidates.append(link_id)
        return {"linked": linked, "unresolved": unresolved, "candidates": candidates}

    # ------------------------------------------------------------------ dossiers

    def link_dossier(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Link plans to a dossier's current revision by its own identifiers; shared words are candidates only."""
        from src.domains.political.legislative_dossiers import (
            DossierError,
            LegislativeDossierStore,
        )
        from src.ingestion.lobbying_sources import REFERENCE_SCHEMES, reference_key
        from src.kb.lobbying_links import stage_keys

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, DOSSIER_READ)
        dossiers = LegislativeDossierStore(self.conn, initialize=False)
        try:
            current = dossiers._state(dossier_namespace, dossier_id)
            dossier = dossiers._full(
                dossier_namespace, dossier_id, current["revision"], principal_id, scopes
            )
        except DossierError as exc:
            raise PublicFinanceError(exc.code, str(exc)) from exc
        keys = stage_keys(dossier)
        linked, candidates = [], []
        for plan in self.store.plans(namespace):
            explicit = False
            for reference in self._plan_references(namespace, plan):
                scheme = reference.get("scheme")
                if (
                    scheme not in DOSSIER_SCHEMES
                    or REFERENCE_SCHEMES.get(scheme) != dossier["jurisdiction"]
                ):
                    continue
                key = reference_key(scheme, reference.get("value"))
                for stage in keys.get(key or "", []):
                    explicit = True
                    link_id = (
                        "pf-link:"
                        + digest(
                            [
                                namespace,
                                plan["plan_id"],
                                "dossier",
                                dossier_namespace,
                                dossier_id,
                                dossier["revision"],
                                key,
                                stage["stage_id"],
                            ]
                        )[:24]
                    )
                    if self._insert(
                        namespace,
                        link_id,
                        "plan",
                        plan["plan_id"],
                        "dossier",
                        dossier_namespace,
                        dossier_id,
                        dossier["revision"],
                        "dossier-identifier",
                        "linked",
                        {"scheme": scheme, "value": reference.get("value"), "key": key},
                        {
                            "plan_source_revision": reference["stated_in"],
                            "stage": stage,
                            "dossier_identifier": stage["identifier"],
                        },
                        "canonical reference key equals a dossier stage identifier",
                        principal_id,
                    ):
                        linked.append(link_id)
            if explicit:
                self._supersede_candidates(
                    namespace, plan["plan_id"], "dossier", dossier_id, principal_id
                )
                continue
            words = _tokens(plan["label"])
            for stage in dossier["stages"]:
                title = stage.get("title")
                shared = sorted(
                    w
                    for w in words
                    if any(t.startswith(w) or w.startswith(t) for t in _tokens(title))
                )
                if not shared or plan["fiscal_year"] not in _years(title):
                    continue
                link_id = (
                    "pf-candidate:"
                    + digest(
                        [
                            namespace,
                            plan["plan_id"],
                            "dossier",
                            dossier_namespace,
                            dossier_id,
                            stage["stage_id"],
                        ]
                    )[:24]
                )
                if self._insert(
                    namespace,
                    link_id,
                    "plan",
                    plan["plan_id"],
                    "dossier",
                    dossier_namespace,
                    dossier_id,
                    dossier["revision"],
                    "discovery",
                    "candidate",
                    None,
                    {
                        "shared_words": shared,
                        "stage_title": title,
                        "stage_id": stage["stage_id"],
                        "note": "shared keywords are a discovery candidate, not a link",
                    },
                    "keyword overlap",
                    principal_id,
                ):
                    candidates.append(link_id)
        return {
            "dossier_id": dossier_id,
            "dossier_revision": dossier["revision"],
            "linked": linked,
            "candidates": candidates,
        }

    # ------------------------------------------------------------------ reviews

    def _transition(self, namespace, link, state, principal_id, reason, extra=None):
        history = link["history"] + [
            {
                "state": state,
                "by": principal_id,
                "reason": reason,
                "at_ms": self.now(),
                **(extra or {}),
            }
        ]
        self.conn.execute(
            "UPDATE public_finance_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link["link_id"]],
        )
        return self.link(namespace, link["link_id"], scopes={"operator"})

    def review(
        self,
        namespace: str,
        link_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise PublicFinanceError(
                "invalid_decision", "accept or reject with a reason"
            )
        link = self.link(namespace, link_id, scopes=scopes)
        if link["state"] != "candidate":
            raise PublicFinanceError(
                "invalid_state",
                f"link is {link['state']}; only a candidate is reviewed",
            )
        return self._transition(
            namespace,
            link,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
        )

    def revert(
        self,
        namespace: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Undo a reviewed decision (the link no longer holds); an explicit citation is the source's own statement."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise PublicFinanceError("invalid_decision", "a revert needs a reason")
        link = self.link(namespace, link_id, scopes=scopes)
        if link["state"] not in {"accepted", "rejected"}:
            raise PublicFinanceError(
                "invalid_state",
                "only an accepted or rejected candidate can be reverted",
            )
        return self._transition(
            namespace, link, "reverted", principal_id, reason.strip()
        )

    # ------------------------------------------------------------------ reads

    def link(
        self, namespace: str, link_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT link_id, subject_kind, subject_id, target_kind, target_namespace, target_id, target_revision, "
            "basis, state, reference_json, evidence_json, rule, history_json, created_by, created_at_ms FROM "
            "public_finance_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise PublicFinanceError(
                "not_found", "link is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "link_id",
                    "subject_kind",
                    "subject_id",
                    "target_kind",
                    "target_namespace",
                    "target_id",
                    "target_revision",
                    "basis",
                    "state",
                ),
                row[:9],
            )
        )
        view.update(
            reference=json.loads(row[9]) if row[9] else None,
            evidence=json.loads(row[10]),
            rule=row[11],
            history=json.loads(row[12]),
            created_by=row[13],
            created_at_ms=row[14],
        )
        last = view["history"][-1]
        view["reviewer"] = (
            last.get("by")
            if view["state"] in {"accepted", "rejected", "reverted"}
            else None
        )
        view["link_kind"] = (
            "explicit-citation"
            if view["state"] == "linked"
            else "reviewed-assertion"
            if view["state"] == "accepted"
            else "unreviewed-candidate"
            if view["state"] == "candidate"
            else view["state"]
        )
        return {"contract": CONTRACT, "namespace": namespace, **view}

    def links(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        subject_id: str | None = None,
        target_kind: str | None = None,
        states: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        wanted = set(states or STATES)
        rows = self.conn.execute(
            "SELECT link_id FROM public_finance_links WHERE namespace=? AND (? IS NULL OR subject_id=?) AND "
            "(? IS NULL OR target_kind=?) ORDER BY subject_id, target_kind, created_at_ms, link_id",
            [namespace, subject_id, subject_id, target_kind, target_kind],
        ).fetchall()
        return [
            v
            for v in (self.link(namespace, r[0], scopes=scopes) for r in rows)
            if v["state"] in wanted
        ]

    # ------------------------------------------------------------------ procurement awards

    def propose_award_parties(
        self,
        namespace: str,
        procurement_namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Offer beneficiaries and procurement suppliers as identity candidates (reviewable, reversible)."""
        from src.kb.procurement_identity import (
            IdentityError,
            ProcurementIdentityService,
        )
        from src.kb.public_finance_identity import PublicFinanceIdentity, vat_key

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, PROCUREMENT_READ)
        identity = PublicFinanceIdentity(self.conn, now=self.now)
        # The procurement owner creates its own link table if this deployment has not yet used it.
        procurement = ProcurementIdentityService(self.conn)
        try:
            parties = [
                p
                for p in procurement.parties(procurement_namespace, scopes=scopes)
                if "supplier" in p["roles"]
            ]
        except IdentityError as exc:
            raise PublicFinanceError(exc.code, str(exc)) from exc
        offered = []
        for subject in identity.subjects(namespace):
            ours = {
                vat_key(i.get("value"), i.get("country") or subject["country"])
                for i in subject["identifiers"]
                if i.get("scheme") == "vat"
            }
            for party in parties:
                item = party["party"]
                country = str(item.get("country") or "").upper()[:2] or None
                theirs = {
                    vat_key(i["id"], country)
                    for i in item.get("identifiers") or []
                    if i["scheme"] == "VAT"
                }
                shared = {v for v in ours & theirs if v and v[0]}
                right_entity = (party.get("link") or {}).get("entity_id") or party[
                    "party_key"
                ]
                right = {
                    "record_key": party["party_key"],
                    "party": item["name"],
                    "notices": party["notices"],
                    "procurement_namespace": procurement_namespace,
                }
                if shared:
                    basis, evidence = (
                        "cross-referenced-identifier",
                        {
                            "kind": "vat",
                            "value": "".join(sorted(shared)[0]),
                            "issuers": "same",
                            "fields": [
                                "beneficiary_identifiers",
                                "supplier.identifiers",
                            ],
                        },
                    )
                elif (
                    subject["country"]
                    and country == str(subject["country"]).upper()
                    and normalize_name(subject["name"]) == normalize_name(item["name"])
                ):
                    basis, evidence = (
                        "name-jurisdiction",
                        {
                            "kind": "name",
                            "value": subject["name"],
                            "country": country,
                            "note": "equal normalized names in one country are a weak signal, never an identity",
                        },
                    )
                elif normalize_identifier(subject["name"]) == normalize_identifier(
                    item["name"]
                ):
                    basis, evidence = (
                        "similar-name",
                        {
                            "kind": "name",
                            "value": subject["name"],
                            "note": "a name alone is never an identity",
                        },
                    )
                else:
                    continue
                offered.append(
                    identity._offer(
                        namespace,
                        subject,
                        party["party_key"],
                        right_entity,
                        basis,
                        {**evidence, "left": subject["side"], "right": right},
                        principal_id,
                        scopes,
                    )
                )
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": [
                c
                for c in identity.candidates(namespace, scopes=scopes)
                if any(r.startswith("procurement-party:") for r in c["records"])
            ],
        }

    def award_context(
        self,
        namespace: str,
        beneficiary_key: str,
        procurement_namespace: str,
        *,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Award history of suppliers a reviewer matched to this beneficiary; context only."""
        from src.kb.procurement_identity import (
            IdentityError,
            ProcurementIdentityService,
            party_key,
        )
        from src.kb.public_finance_identity import PublicFinanceIdentity

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, PROCUREMENT_READ)
        identity = PublicFinanceIdentity(self.conn, now=self.now)
        matched = [
            link
            for link in identity.identity(namespace, beneficiary_key, scopes=scopes)[
                "links"
            ]
            if link["target"].startswith("procurement-party:")
        ]
        awards = []
        if matched:
            procurement = ProcurementIdentityService(self.conn)
            try:
                history = procurement.award_history(
                    procurement_namespace, scopes=scopes
                )
            except IdentityError as exc:
                raise PublicFinanceError(exc.code, str(exc)) from exc
            targets = {link["target"]: link for link in matched}
            for row in history:
                for supplier in row["suppliers"]:
                    key = party_key(supplier)
                    if key in targets:
                        awards.append(
                            {
                                **row,
                                "matched_supplier": supplier["name"],
                                "identity_link": targets[key],
                                "semantics": AWARD_CONTEXT,
                            }
                        )
        return {
            "beneficiary_key": beneficiary_key,
            "matched_parties": matched,
            "awards": awards,
            "note": AWARD_CONTEXT
            if awards
            else "no reviewed supplier match; no award history is shown",
        }
