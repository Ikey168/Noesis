"""ASNs, prefixes, domains and organisations across sources through reviewable identity (#2743, II07).

Follows :mod:`src.kb.entity_history` and the wave's identity modules: every relation between records of different
sources is an *assertion* that is ``proposed``, then ``accepted`` or ``rejected`` by a reviewer (reviewer, reason and
time recorded), and an accepted assertion can be ``reverted``. Nothing is auto-merged: records keep their own source,
and answers show accepted matches side by side, never one merged record.

Methods, in order (stated identifiers before names; a name is never a match):

* ``stated-identifier:asn`` / ``stated-identifier:prefix`` / ``stated-identifier:domain`` - the RIPEstat overview,
  the PeeringDB network and the RDAP autnum, IP network or domain stating the same declared ASN, prefix or domain;
  crt.sh certificates and the RDAP domain stating the same exact domain (confidence 1.0 as a fact of the identifiers,
  still proposed until reviewed);
* ``stated-identifier:asn-holder`` - the PeeringDB organisation the declared ASN's PeeringDB network names
  (``org_id``) and the holder organisation the RDAP autnum of the same ASN states. Both are tied to the ASN by stated
  identifiers; the organisation names are evidence context only (confidence 0.6). Two organisations that merely share
  a name are never proposed.

There is no "other networks of this organisation" listing: an organisation is reached only through a declared ASN.
Records without an accepted assertion stay visible as unmatched.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from itertools import combinations
from typing import Any

from src.kb.internet_infrastructure_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    InfrastructureRecordError,
    authorize,
    canonical,
    digest,
    iso,
    load,
)
from src.kb.internet_infrastructure_store import InternetInfrastructureStore

CONTRACT = "noesis-internet-infrastructure-identity-v1"
KINDS = ("same-asn", "same-prefix", "same-domain", "organisation")
STATES = ("proposed", "accepted", "rejected", "reverted")
DECISIONS = ("accepted", "rejected")
# The record kinds that stand for a resource in each source (observation series stand for RIPEstat by overview call).
REPRESENTATIVE = {
    ("ripestat", "routing-observation"): {"as-overview": "asn", "prefix-overview": "prefix"},
    ("peeringdb", "net"): "asn",
    ("rdap", "autnum"): "asn",
    ("rdap", "ip-network"): "prefix",
    ("rdap", "domain"): "domain",
    ("crtsh", "certificate"): "domain",
}
_DDL = """
CREATE TABLE IF NOT EXISTS ii_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, left_object_id TEXT NOT NULL,
  right_object_id TEXT NOT NULL, method TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
"""


class InfrastructureIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = InternetInfrastructureStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ candidates

    def _current(self, namespace: str, obj: Mapping[str, Any]) -> dict[str, Any] | None:
        """The latest published revision (or observation) of an object, with its id."""
        if obj["shape"] == "observations":
            rows = self.store.observations(namespace, obj["object_id"])
            return {"record_id": rows[-1]["observation_id"], "identifiers": rows[-1]["identifiers"],
                    "content": rows[-1]["content"]} if rows else None
        rows = [r for r in self.store.revisions(namespace, obj["object_id"])]
        if not rows or rows[-1]["state"] != "published":
            return None
        return {"record_id": rows[-1]["revision_id"], "identifiers": rows[-1]["identifiers"],
                "content": rows[-1]["content"]}

    def _resources(self, namespace: str) -> list[dict[str, Any]]:
        """Representative records with the identifier each states (ASN, prefix or domain)."""
        out = []
        for obj in self.store.objects(namespace):
            spec = REPRESENTATIVE.get((obj["provider"], obj["object_kind"]))
            if spec is None:
                continue
            if isinstance(spec, dict):
                call = obj["native_id"].split(":", 1)[0]
                if call not in spec:
                    continue
                kind = spec[call]
            else:
                kind = spec
            current = self._current(namespace, obj)
            if current is None:
                continue
            values = [str(v) for v in current["identifiers"].get(kind) or []]
            if obj["provider"] == "ripestat":
                values = [obj["resource"]["value"]] if obj["resource"]["kind"] == kind else []
            for value in values:
                out.append({"object": obj, "kind": kind, "value": value, "record_id": current["record_id"],
                            "content": current["content"]})
        return out

    def _existing(self, namespace: str, assertion_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT 1 FROM ii_identity_assertions WHERE namespace=? AND assertion_id=?",
                                [namespace, assertion_id]).fetchone()
        return self.assertion(namespace, assertion_id, scopes={"operator"}) if row else None

    def _propose(self, namespace, kind, left, right, method, confidence, evidence, principal_id):
        left_id, right_id = sorted((left["object"]["object_id"], right["object"]["object_id"]))
        assertion_id = "ii-identity:" + digest([namespace, kind, left_id, right_id, method])[:24]
        existing = self._existing(namespace, assertion_id)
        if existing:
            return existing, False
        history = [{"state": "proposed", "by": principal_id, "at": iso(self.now()), "reason": None}]
        self.conn.execute("INSERT INTO ii_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, assertion_id, kind, left_id, right_id, method, confidence, canonical(evidence),
                           "proposed", None, canonical(history), principal_id, self.now()])
        return self.assertion(namespace, assertion_id, scopes={"operator"}), True

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Propose matches between records of different sources by stated identifiers only; nothing is accepted."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        resources = self._resources(namespace)
        created, kept = [], []
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in resources:
            groups.setdefault((entry["kind"], entry["value"]), []).append(entry)
        for (kind, value), entries in sorted(groups.items()):
            for left, right in combinations(sorted(entries, key=lambda e: e["object"]["object_id"]), 2):
                if left["object"]["provider"] == right["object"]["provider"]:
                    continue  # one source's own records are not an identity question
                evidence = {"identifier": {"kind": kind, "value": value},
                            "records": [{"object_id": e["object"]["object_id"], "provider": e["object"]["provider"],
                                         "record_id": e["record_id"]} for e in (left, right)]}
                item, new = self._propose(namespace, f"same-{kind}", left, right, f"stated-identifier:{kind}", 1.0,
                                          evidence, principal_id)
                (created if new else kept).append(item)
        for item, new in self._organisations(namespace, principal_id):
            (created if new else kept).append(item)
        return {"contract": CONTRACT, "proposed": created, "already_recorded": len(kept),
                "unmatched": self.unmatched(namespace, scopes={"operator"}),
                "note": "proposals rest on stated identifiers; a shared name is never a match and nothing is used "
                        "until a reviewer accepts it"}

    def _organisations(self, namespace: str, principal_id: str):
        """PeeringDB organisation <-> RDAP autnum holder, both tied to the same declared ASN by stated identifiers."""
        out = []
        for net in self.store.objects(namespace, provider="peeringdb", object_kind="net"):
            current = self._current(namespace, net)
            if not current or not current["content"].get("org_id"):
                continue
            asns = current["identifiers"].get("asn") or []
            org = next((o for o in self.store.objects(namespace, provider="peeringdb", object_kind="org")
                        if o["native_id"] == str(current["content"]["org_id"])), None)
            org_current = self._current(namespace, org) if org else None
            if org_current is None:
                continue
            for autnum in self.store.objects(namespace, provider="rdap", object_kind="autnum"):
                rdap_current = self._current(namespace, autnum)
                if not rdap_current or not set(asns) & set(rdap_current["identifiers"].get("asn") or []):
                    continue
                holder = rdap_current["content"].get("holder_organisation")
                if not holder:
                    continue  # no stated holder organisation: nothing to relate
                evidence = {
                    "identifier": {"kind": "asn", "value": min(asns)},
                    "records": [{"object_id": org["object_id"], "provider": "peeringdb",
                                 "record_id": org_current["record_id"], "via": f"net {net['native_id']} org_id"},
                                {"object_id": autnum["object_id"], "provider": "rdap",
                                 "record_id": rdap_current["record_id"], "via": "autnum holder"}],
                    "names_as_stated": {"peeringdb": org_current["content"].get("name"), "rdap": holder},
                    "names_note": "names are context only; the proposal rests on the shared declared ASN",
                }
                out.append(self._propose(namespace, "organisation", {"object": org}, {"object": autnum},
                                         "stated-identifier:asn-holder", 0.6, evidence, principal_id))
        return out

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, left_object_id, right_object_id, method, confidence, evidence_json, state, "
            "reason, history_json, created_by FROM ii_identity_assertions WHERE namespace=? AND assertion_id=?",
            [namespace, assertion_id]).fetchone()
        if not row:
            raise InfrastructureRecordError("assertion_not_found", "no such identity assertion")
        return {"contract": CONTRACT, "assertion_id": row[0], "kind": row[1], "left_object_id": row[2],
                "right_object_id": row[3], "method": row[4], "confidence": row[5], "evidence": load(row[6], {}),
                "state": row[7], "reason": row[8], "history": load(row[9], []), "proposed_by": row[10]}

    def assertions(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   object_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT assertion_id FROM ii_identity_assertions WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR left_object_id=? OR right_object_id=?) ORDER BY created_at_ms, assertion_id",
            [namespace, state, state, object_id, object_id, object_id]).fetchall()
        return [self.assertion(namespace, r[0], scopes={"operator"}) for r in rows]

    def _transition(self, namespace, assertion_id, state, reason, principal_id):
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        history = item["history"] + [{"state": state, "by": principal_id, "at": iso(self.now()), "reason": reason}]
        self.conn.execute("UPDATE ii_identity_assertions SET state=?, reason=?, history_json=? WHERE namespace=? AND "
                          "assertion_id=?", [state, reason, json.dumps(history), namespace, assertion_id])
        return self.assertion(namespace, assertion_id, scopes={"operator"})

    def review(self, namespace: str, assertion_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in DECISIONS or not str(reason or "").strip():
            raise InfrastructureRecordError("invalid_review", f"a review is one of {DECISIONS} with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] != "proposed":
            raise InfrastructureRecordError("invalid_review", f"only proposed assertions are reviewed ({item['state']})")
        return self._transition(namespace, assertion_id, decision, reason, principal_id)

    def revert(self, namespace: str, assertion_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise InfrastructureRecordError("invalid_review", "a revert states its reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] != "accepted":
            raise InfrastructureRecordError("invalid_review", "only accepted assertions are reverted")
        return self._transition(namespace, assertion_id, "reverted", reason, principal_id)

    def accepted_for(self, namespace: str, object_ids: Iterable[str]) -> list[dict[str, Any]]:
        wanted = set(object_ids)
        return [a for a in self.assertions(namespace, scopes={"operator"}, state="accepted")
                if a["left_object_id"] in wanted or a["right_object_id"] in wanted]

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Representative records without an accepted assertion: visible as unmatched, never dropped."""
        authorize(namespace, scopes, READ_SCOPE)
        matched = {o for a in self.assertions(namespace, scopes={"operator"}, state="accepted")
                   for o in (a["left_object_id"], a["right_object_id"])}
        seen, out = set(), []
        for entry in self._resources(namespace):
            obj = entry["object"]
            if obj["object_id"] in matched or obj["object_id"] in seen:
                continue
            seen.add(obj["object_id"])
            out.append({"object_id": obj["object_id"], "provider": obj["provider"], "object_kind": obj["object_kind"],
                        "native_id": obj["native_id"], "resource": obj["resource"], "state": "unmatched"})
        for org in self.store.objects(namespace, provider="peeringdb", object_kind="org"):
            if org["object_id"] not in matched:
                out.append({"object_id": org["object_id"], "provider": "peeringdb", "object_kind": "org",
                            "native_id": org["native_id"], "resource": org["resource"], "state": "unmatched"})
        return out


__all__ = ["CONTRACT", "KINDS", "STATES", "InfrastructureIdentity"]
