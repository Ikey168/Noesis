"""Voting-rights notifications as Corporate Ownership ``voting_rights`` control assertions (#2106, BF08).

The projection goes through the one ownership record owner
(:class:`src.kb.ownership_store.OwnershipStore`); there is no second ownership
store. For each correction chain the notice published last is projected:

* the issuer becomes a ``legal_entity`` record ``bafin-issuer:<ISIN>`` with the
  ISIN and the stated LEI as identifiers. It is its own record and never
  rewrites identifiers another record owns; the Corporate Ownership identity
  review groups it with GLEIF or register records;
* the notifier carries the notification's percentages, one ``voting_rights``
  assertion per stated basis (§ 33 shares, § 38 instruments, § 39 total), and
  every other member of the chain of controlled undertakings carries the
  percentages it states, in the stated order. Nothing is summed and nothing is
  combined into a computed owner;
* a holder whose BaFin party has an accepted, unreverted identity candidate
  (BF07) is keyed to the ownership record that candidate links. Every other
  holder stays a source-string holder (no key), which is how the ownership graph
  treats unresolved parties;
* every assertion cites the notice revision (``statement_id``) and its source
  locator. A correction revises the same assertion record, so the earlier
  statement stays in the record's history. A member a correction drops, or a
  withdrawn notice, gets a closed validity (``from`` = ``to``) and a
  ``relationship_status`` saying why. It is never deleted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.domains.market.bafin_identity import (
    BafinIdentity,
    organisation_key,
    person_key,
)
from src.domains.market.bafin_notices import (
    READ_SCOPE,
    BafinNoticeStore,
    authorize,
    correction_chains,
    digest,
    issuer_key,
    normalize_isin,
    party_key,
)

BASES = (
    ("s33", "voting_rights_pct", "WpHG § 33 (shares)"),
    ("s38", "instruments_pct", "WpHG § 38 (instruments)"),
    ("s39", "total_pct", "WpHG § 39 (total)"),
)
LABELS = {key: label for key, _, label in BASES}
KEY_PREFIX = "bafin-vr:"
NOTE = "as stated by the notifier in a voting-rights notification; not a beneficial-ownership determination"


def issuer_record_key(isin: str) -> str:
    return f"bafin-issuer:{isin}"


def _source(view: Mapping[str, Any], field: str) -> dict[str, Any]:
    notice = view["notice"]
    source = notice["source"]
    result = {
        "provider": "bafin",
        "provider_record_id": source["source_id"],
        "statement_id": view["revision_id"],
        "revision": str(view["revision"]),
        "publisher": "BaFin (voting-rights database)",
        "source_type": "voting-rights notification (WpHG §§ 33 ff.)",
        "locator": {"field": field},
        "retrieved_at_ms": int(view["observed_at_ms"]),
        "note": NOTE,
    }
    if source.get("url"):
        result["url"] = source["url"]
    return result


class BafinOwnershipProjection:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.ownership_store import OwnershipStore

        self.conn = conn
        self.notices = BafinNoticeStore(conn, initialize=False, now=now)
        self.ownership = OwnershipStore(conn, now=now)
        self.identity = BafinIdentity(conn, now=now, initialize=False)

    def _holder(
        self, namespace: str, name: str, kind: str, issuer: str
    ) -> dict[str, Any]:
        natural = kind == "natural_person"
        party = person_key(issuer, name) if natural else organisation_key(name)
        target = self.identity.accepted_target(namespace, party)
        holder = {"name": name, "kind": "person" if natural else "entity"}
        if target:
            holder["key"] = target["record_key"]
        return holder

    def records(
        self, namespace: str, *, issuers: Iterable[str] | None = None
    ) -> tuple[list[dict], list[dict]]:
        """(ownership records to apply, per-chain projection summary)."""
        from src.kb.ownership_records import record

        wanted = {normalize_isin(i) for i in issuers or []}
        views = self.notices.visible(namespace, kinds=("voting_rights_notification",))[
            "notices"
        ]
        chains = correction_chains(views)
        by_id = {v["notice_id"]: v for v in views}
        records: list[dict[str, Any]] = []
        issuers_done: set[str] = set()
        summary = []
        for nid, chain in sorted(chains.items()):
            if chain["superseded_by"] is not None:
                continue
            view = by_id[nid]
            notice = view["notice"]
            isin = (notice.get("issuer") or {}).get("isin")
            if not isin or (wanted and isin not in wanted):
                continue
            subject = issuer_record_key(isin)
            if isin not in issuers_done:
                issuers_done.add(isin)
                identifiers = [
                    {"scheme": "isin", "value": isin, "authority": "ISO 6166"}
                ]
                if notice["issuer"].get("lei"):
                    identifiers.append(
                        {
                            "scheme": "lei",
                            "value": notice["issuer"]["lei"],
                            "authority": "GLEIF",
                        }
                    )
                # A stable source: the issuer record does not change with every notice that names it.
                source = {
                    "provider": "bafin",
                    "provider_record_id": f"issuer:{isin}",
                    "publisher": "BaFin (voting-rights database)",
                    "locator": {"field": "issuer"},
                    "source_type": "issuer named in voting-rights notifications",
                    "note": NOTE,
                }
                records.append(
                    record(
                        "legal_entity",
                        subject,
                        source,
                        name=notice["issuer"].get("name") or isin,
                        identifiers=identifiers,
                    )
                )
            root = chain["root"]
            withdrawn = bool(notice.get("withdrawn"))
            validity = (
                {
                    "from": notice["event_date"],
                    "from_status": "stated",
                    "to_status": "unknown",
                }
                if notice.get("event_date")
                else {"from_status": "unknown", "to_status": "unknown"}
            )
            if withdrawn:
                closed = notice.get("event_date") or notice.get("publication_date")
                validity = {
                    "from": closed,
                    "to": closed,
                    "from_status": "stated",
                    "to_status": "stated",
                }
            notifier = notice["notifier"]
            members = list(notice.get("chain") or [])
            holders = [
                (
                    notifier["name"],
                    notifier.get("kind", "unknown"),
                    0,
                    None,
                    {key: notice["percentages"].get(key) for key, _, _ in BASES},
                    {key: f"percentages.{key}" for key, _, _ in BASES},
                )
            ]
            for member in members:
                if party_key(member["name"]) == party_key(notifier["name"]):
                    continue  # the notifier heads the chain and carries the notification's percentages
                previous = (
                    members[member["position"] - 2]["name"]
                    if member["position"] > 1
                    else notifier["name"]
                )
                holders.append(
                    (
                        member["name"],
                        "legal_person",
                        member["position"],
                        previous,
                        {key: member.get(field) for key, field, _ in BASES},
                        {
                            key: f"chain[{member['position']}].{field}"
                            for key, field, _ in BASES
                        },
                    )
                )
            keys = []
            for name, kind, position, controlled_by, shares, fields in holders:
                holder = self._holder(
                    namespace, name, kind, issuer_key(notice["issuer"])
                )
                for basis_key, value in shares.items():
                    if value is None:
                        continue
                    basis = LABELS[basis_key]
                    key = (
                        f"{KEY_PREFIX}{root}:{digest(party_key(name))[:12]}:{basis_key}"
                    )
                    keys.append(key)
                    records.append(
                        record(
                            "ownership_assertion",
                            key,
                            _source(view, fields[basis_key]),
                            subject_key=subject,
                            holder=holder,
                            assertion_kind="voting_rights",
                            share={"exact": value},
                            validity=validity,
                            statement_date=notice.get("publication_date"),
                            basis=f"{basis} as stated by the notifier",
                            relationship_status="withdrawn by the source"
                            if withdrawn
                            else "as notified",
                            native={
                                "notice_id": nid,
                                "revision_id": view["revision_id"],
                                "chain_position": position,
                                "controlled_by": controlled_by,
                                "notifier": notifier["name"],
                                "thresholds": notice.get("thresholds") or [],
                                "event_date": notice.get("event_date"),
                                "publication_date": notice.get("publication_date"),
                                "correction_chain": chain["members"],
                                "holder_reviewed": bool(holder.get("key")),
                            },
                        )
                    )
            summary.append(
                {
                    "notice_id": nid,
                    "root": root,
                    "issuer": isin,
                    "assertions": sorted(keys),
                    "withdrawn": withdrawn,
                    "corrections": chain["members"][:-1],
                }
            )
        return records, summary

    def project(
        self,
        namespace: str,
        ownership_namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        issuers: Iterable[str] | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        from src.kb.ownership_records import WRITE_SCOPE, validate_record
        from src.kb.ownership_store import authorize as ownership_authorize

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        ownership_authorize(ownership_namespace, scopes, WRITE_SCOPE, write=True)
        self.notices.require_ready()
        records, summary = self.records(namespace, issuers=issuers)
        produced = {r["record_key"] for r in records}
        # Members a correction dropped (and assertions of a chain now withdrawn) are closed, never deleted.
        roots = {s["root"] for s in summary}
        closed = []
        for view in self.ownership.records(
            ownership_namespace,
            principal_id=principal_id,
            scopes=scopes,
            kinds=("ownership_assertion",),
        ):
            body = view["record"]
            key = body["record_key"]
            if not key.startswith(KEY_PREFIX) or key in produced:
                continue
            root = next((r for r in roots if key.startswith(f"{KEY_PREFIX}{r}:")), None)
            if root is None or str(body.get("relationship_status") or "").startswith(
                "dropped"
            ):
                continue
            end = next((s for s in summary if s["root"] == root), None)
            day = body["validity"].get("from") or body.get("statement_date")
            if day is None:
                continue
            payload = {k: v for k, v in body.items() if k != "unknowns"}
            payload.update(
                {
                    "validity": {
                        "from": day,
                        "to": day,
                        "from_status": "stated",
                        "to_status": "stated",
                    },
                    "relationship_status": f"dropped by correction {end['notice_id'] if end else ''}".strip(),
                }
            )
            closed.append(validate_record(payload))
        counts = self.ownership.apply(
            ownership_namespace,
            records + closed,
            run_id=run_id or f"bafin-projection:{self.notices.generation(namespace)}",
            observed_at_ms=self.ownership.now(),
            principal_id=principal_id,
        )
        return {
            "counts": counts,
            "chains": summary,
            "closed": sorted(r["record_key"] for r in closed),
            "ownership_namespace": ownership_namespace,
            "note": NOTE,
        }
