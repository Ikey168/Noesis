"""On-chain observation records (#2053, B02 #2055).

Contract ``noesis-onchain-observation-v1``. This module owns five
revision-addressable, namespace-scoped record kinds, each citing the explorer
observation that first stated its current content:

* ``transaction`` - hash, block, block hash, timestamp, from/to and value
  (EVM) or inputs/outputs (UTXO), status and finality;
* ``transfer`` - a token transfer as the explorer reports it (token contract,
  symbol and decimals *as stated*, from, to, raw value, log index when given);
* ``contract_deployment`` - contract, deployer, creation transaction, factory;
* ``funding`` - the first inbound value transfers of an address in an explicit
  acquisition window, with ``complete`` and the internal-transfer gap stated;
* ``label_assertion`` - what a public label dataset *states* about an address,
  with the source, licence and attribution. There is no owner field anywhere:
  Noesis never asserts who controls an address.

Revisions (the C01.2 store rules): re-acquiring unchanged content adds
nothing; changed content becomes a new revision and is current only when its
source position (``as_of_block``: the chain head the explorer reported, else
the block itself) is not older than the current one; late older data is kept
as history without a correction event; a return to earlier content is a new
revision (``reversion``). Reorganisations (a transaction reported in a
different block hash), confirmation-depth crossings (``finality``) and
transactions that disappear (``dropped``) are explicit changes. Confirmation
counts themselves are not content, so repeated reads do not churn revisions.

Keys include the CAIP-2 chain id, so the same address string on two chains is
two records. Organizations and contracts may be referenced to
``canonical_entities`` only through reviewed label references
(:class:`OnchainStore.propose_label_reference`); person-typed entities are
refused by the schema and by the code.

Reads never raise a raw database error: before any acquisition has run every
public read returns ``status: not_ready``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.kb.onchain_identifiers import (
    CHAINS,
    IdentifierError,
    display_address,
    explorer_url,
    family,
    normalize_address,
    normalize_chain,
    normalize_tx_hash,
)

CONTRACT = "noesis-onchain-observation-v1"
READ_SCOPE = "knowledge:onchain:read"
WRITE_SCOPE = "knowledge:onchain:write"
REVIEW_SCOPE = "knowledge:onchain:review"
KINDS = ("transaction", "transfer", "contract_deployment", "funding", "label_assertion")
TABLES = (
    "onchain_observations",
    "onchain_acquisitions",
    "onchain_revisions",
    "onchain_current",
    "onchain_address_roles",
    "onchain_coverage",
    "onchain_label_references",
)
PERSON_TYPES = frozenset(
    {"person", "people", "individual", "natural_person", "human", "per"}
)
REFERENCE_TYPES = frozenset(
    {
        "organization",
        "organisation",
        "org",
        "company",
        "contract",
        "project",
        "protocol",
        "software",
        "product",
    }
)
NOT_ATTRIBUTION = (
    "labels are quoted from their source with attribution; Noesis never states who controls an "
    "address"
)
_NAMESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_ENTITY_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}

_DDL = """
CREATE TABLE IF NOT EXISTS onchain_observations (
  observation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, operation TEXT NOT NULL,
  chain_id TEXT NOT NULL, subject TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, request_id TEXT NOT NULL,
  record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onchain_acquisitions (
  request_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, operation TEXT NOT NULL,
  input_hash TEXT NOT NULL, receipt_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS onchain_revisions (
  namespace TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL, revision INTEGER NOT NULL,
  revision_id TEXT NOT NULL, chain_id TEXT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL,
  as_of_block BIGINT, observed_at_ms BIGINT NOT NULL, observation_id TEXT NOT NULL, change TEXT NOT NULL,
  detail_json TEXT NOT NULL, PRIMARY KEY(namespace, kind, record_key, revision)
);
CREATE TABLE IF NOT EXISTS onchain_current (
  namespace TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL, chain_id TEXT NOT NULL,
  revision INTEGER NOT NULL, revision_id TEXT NOT NULL, as_of_block BIGINT, observed_at_ms BIGINT NOT NULL,
  observation_id TEXT NOT NULL, content_json TEXT NOT NULL, PRIMARY KEY(namespace, kind, record_key)
);
CREATE TABLE IF NOT EXISTS onchain_address_roles (
  namespace TEXT NOT NULL, kind TEXT NOT NULL, record_key TEXT NOT NULL, chain_id TEXT NOT NULL,
  address TEXT NOT NULL, role TEXT NOT NULL, block_number BIGINT,
  PRIMARY KEY(namespace, kind, record_key, address, role)
);
CREATE TABLE IF NOT EXISTS onchain_coverage (
  coverage_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, chain_id TEXT NOT NULL, address TEXT NOT NULL,
  operation TEXT NOT NULL, from_block BIGINT, to_block BIGINT, complete BOOLEAN NOT NULL,
  observation_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS onchain_label_references (
  namespace TEXT NOT NULL, reference_id TEXT NOT NULL, label_record_key TEXT NOT NULL, chain_id TEXT NOT NULL,
  address TEXT NOT NULL, canonical_id TEXT NOT NULL, entity_type TEXT NOT NULL, state TEXT NOT NULL,
  decision_id TEXT, review_seq INTEGER NOT NULL, evidence_json TEXT NOT NULL, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, reference_id)
);
"""


class OnchainError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.details = code, details

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "code": self.code,
            "error": str(self),
            **self.details,
        }


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(int(seconds), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_exists(conn: Any, table: str) -> bool:
    try:
        return bool(
            conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
            ).fetchone()
        )
    except Exception:  # noqa: BLE001 - readiness probes never raise
        return False


def ready(conn: Any) -> bool:
    return all(
        table_exists(conn, t)
        for t in (
            "onchain_revisions",
            "onchain_current",
            "onchain_address_roles",
            "onchain_coverage",
        )
    )


def not_ready(**extra: Any) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "reason": "no on-chain acquisition has run in this warehouse yet",
        "note": "acquire observations for an explicit address, transaction or contract first",
        **extra,
    }


def authorize(namespace: str, scopes: Iterable[str], scope: str) -> str:
    if not _NAMESPACE.match(str(namespace or "")):
        raise OnchainError("invalid_namespace", "a namespace is required")
    granted = set(scopes or ())
    if scope not in granted and "operator" not in granted:
        raise OnchainError("forbidden", f"{scope} is required", required_scope=scope)
    return namespace


def finality(
    chain_id: str, confirmations: int | None, block_number: int | None
) -> str | None:
    """``pending`` (no block), ``unconfirmed`` (below the chain's finality depth) or ``final``; None if unknown."""
    if block_number is None:
        return "pending"
    if confirmations is None:
        return None
    return (
        "final"
        if confirmations >= CHAINS[chain_id]["finality_depth"]
        else "unconfirmed"
    )


def _int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _roles(kind: str, content: Mapping[str, Any]) -> list[tuple[str, str, int | None]]:
    block = _int(content.get("block_number"))
    out: list[tuple[str, str, int | None]] = []
    if kind == "transaction":
        for role in ("from", "to", "contract_created"):
            if content.get(role):
                out.append((content[role], role, block))
        for item in content.get("inputs") or []:
            if item.get("address"):
                out.append((item["address"], "input", block))
        for item in content.get("outputs") or []:
            if item.get("address"):
                out.append((item["address"], "output", block))
    elif kind == "transfer":
        for role in ("from", "to"):
            if content.get(role):
                out.append((content[role], role, block))
        if content.get("token_contract"):
            out.append((content["token_contract"], "token", block))
    elif kind == "contract_deployment":
        out.append((content["contract_address"], "contract", block))
        if content.get("deployer"):
            out.append((content["deployer"], "deployer", block))
    elif kind == "funding":
        out.append((content["address"], "funded", None))
        for item in content.get("first_inbound") or []:
            if item.get("from"):
                out.append((item["from"], "funder", _int(item.get("block_number"))))
    elif kind == "label_assertion":
        out.append((content["address"], "labelled", None))
    return sorted(set(out), key=lambda r: (r[0], r[1]))


def _changed(
    before: Mapping[str, Any] | None, after: Mapping[str, Any]
) -> dict[str, Any]:
    if before is None:
        return {}
    keys = sorted(set(before) | set(after))
    return {
        k: {"before": before.get(k), "after": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    }


def _classify(
    kind: str,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any],
    earlier: set[str],
) -> str:
    if before is None:
        return "initial"
    if after.get("state") == "dropped" and before.get("state") != "dropped":
        return "dropped"
    if after.get("state") == "withdrawn" and before.get("state") != "withdrawn":
        return "withdrawn"
    if (
        kind in {"transaction", "transfer"}
        and before.get("block_hash")
        and after.get("block_hash")
        and (before["block_hash"] != after["block_hash"])
    ):
        return "reorg"
    fields = set(_changed(before, after))
    if fields == {"finality"}:
        return "finality"
    if digest(after) in earlier:
        return "reversion"
    return "correction"


class OnchainStore:
    """Namespace-scoped on-chain records with per-record revisions."""

    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for statement in _DDL.strip().split(";"):
                if statement.strip():
                    conn.execute(statement)

    # ------------------------------------------------------------------ writes

    def record_observation(self, namespace: str, record: Mapping[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO onchain_observations VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                record["observation_id"],
                namespace,
                record["provider"],
                record["operation"],
                record["chain_id"],
                record["subject"]["value"],
                record["observed_at_ms"],
                record["request_id"],
                canonical(record),
            ],
        )

    def append(
        self,
        namespace: str,
        kind: str,
        record_key: str,
        chain_id: str,
        content: Mapping[str, Any],
        *,
        as_of_block: int | None,
        observed_at_ms: int,
        observation_id: str,
    ) -> dict[str, Any]:
        """Add one record content; returns the change (``None`` when nothing was added)."""
        if kind not in KINDS:
            raise OnchainError("invalid_kind", f"unknown record kind {kind!r}")
        content = json.loads(canonical(content))  # canonical, JSON-only values
        content_hash = digest(content)
        current = self.conn.execute(
            "SELECT revision, content_json, as_of_block, observed_at_ms FROM onchain_current WHERE namespace=? AND "
            "kind=? AND record_key=?",
            [namespace, kind, record_key],
        ).fetchone()
        history = self.conn.execute(
            "SELECT revision, content_hash FROM onchain_revisions WHERE namespace=? AND kind=? AND record_key=?",
            [namespace, kind, record_key],
        ).fetchall()
        before = json.loads(current[1]) if current else None
        if before is not None and digest(before) == content_hash:
            return {
                "record_key": record_key,
                "kind": kind,
                "change": None,
                "status": "unchanged",
            }
        position = (
            -1 if as_of_block is None else int(as_of_block),
            int(observed_at_ms),
        )
        newer = current is None or position >= (
            -1 if current[2] is None else int(current[2]),
            int(current[3]),
        )
        known = {row[1] for row in history}
        if not newer and content_hash in known:
            return {
                "record_key": record_key,
                "kind": kind,
                "change": None,
                "status": "history_known",
            }
        revision = max((row[0] for row in history), default=0) + 1
        change = _classify(kind, before, content, known) if newer else "history"
        detail = (
            {"changed": _changed(before, content)}
            if newer
            else {"late_older_than_current": True}
        )
        revision_id = (
            "onchain-rev:"
            + digest([namespace, kind, record_key, revision, content_hash])[:28]
        )
        self.conn.execute(
            "INSERT INTO onchain_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                kind,
                record_key,
                revision,
                revision_id,
                chain_id,
                content_hash,
                canonical(content),
                as_of_block,
                observed_at_ms,
                observation_id,
                change,
                canonical(detail),
            ],
        )
        if newer:
            self.conn.execute(
                "DELETE FROM onchain_current WHERE namespace=? AND kind=? AND record_key=?",
                [namespace, kind, record_key],
            )
            self.conn.execute(
                "INSERT INTO onchain_current VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    kind,
                    record_key,
                    chain_id,
                    revision,
                    revision_id,
                    as_of_block,
                    observed_at_ms,
                    observation_id,
                    canonical(content),
                ],
            )
            self.conn.execute(
                "DELETE FROM onchain_address_roles WHERE namespace=? AND kind=? AND record_key=?",
                [namespace, kind, record_key],
            )
            for address, role, block in _roles(kind, content):
                self.conn.execute(
                    "INSERT INTO onchain_address_roles VALUES (?,?,?,?,?,?,?)",
                    [namespace, kind, record_key, chain_id, address, role, block],
                )
        return {
            "record_key": record_key,
            "kind": kind,
            "change": change,
            "revision": revision,
            "revision_id": revision_id,
            "current": newer,
        }

    def record_coverage(
        self,
        namespace: str,
        chain_id: str,
        item: Mapping[str, Any],
        observation_id: str,
        observed_at_ms: int,
    ) -> None:
        window = [
            namespace,
            chain_id,
            item["address"],
            item["operation"],
            _int(item.get("from_block")),
            _int(item.get("to_block")),
            bool(item.get("complete")),
        ]
        # Keyed by every distinguishing field of the window (one observation may state a complete and a
        # truncated window); re-observing the same window adds nothing and keeps its first observation.
        self.conn.execute(
            "INSERT INTO onchain_coverage VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                "onchain-cov:" + digest(window)[:28],
                *window,
                observation_id,
                observed_at_ms,
            ],
        )

    def project(
        self,
        namespace: str,
        observation: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Project one observation's normalised facts into records (idempotent)."""
        authorize(namespace, scopes, WRITE_SCOPE)
        chain = normalize_chain(observation["chain_id"])
        facts = observation["facts"]
        head = _int(facts.get("head_block"))
        at = int(observation["observed_at_ms"])
        oid = observation["observation_id"]
        changes: list[dict[str, Any]] = []

        def add(
            kind: str, key: str, content: Mapping[str, Any], as_of: int | None
        ) -> None:
            result = self.append(
                namespace,
                kind,
                key,
                chain,
                content,
                as_of_block=as_of,
                observed_at_ms=at,
                observation_id=oid,
            )
            if result.get("change"):
                changes.append(result)

        for tx in facts.get("transactions") or []:
            content = transaction_content(chain, tx)
            add(
                "transaction",
                f"{chain}|{content['tx_hash']}",
                content,
                head if head is not None else content.get("block_number"),
            )
        for tx_hash in facts.get("dropped") or []:
            key = f"{chain}|{normalize_tx_hash(chain, tx_hash)}"
            row = self.conn.execute(
                "SELECT content_json FROM onchain_current WHERE namespace=? AND kind='transaction' "
                "AND record_key=?",
                [namespace, key],
            ).fetchone()
            if row:
                prior = json.loads(row[0])
                if (
                    prior.get("finality") != "final"
                ):  # a final transaction is never reported dropped
                    add(
                        "transaction",
                        key,
                        {**prior, "state": "dropped", "finality": None},
                        head,
                    )
        for tr in facts.get("transfers") or []:
            content = transfer_content(chain, tr)
            add(
                "transfer",
                f"{chain}|{content['transfer_key']}",
                content,
                head if head is not None else content.get("block_number"),
            )
        for dep in facts.get("deployments") or []:
            content = deployment_content(chain, dep)
            add(
                "contract_deployment",
                f"{chain}|{content['contract_address']}",
                content,
                head if head is not None else content.get("block_number"),
            )
        for fund in facts.get("funding") or []:
            content = funding_content(chain, fund)
            add("funding", f"{chain}|{content['address']}", content, head)
        for label in facts.get("labels") or []:
            content = label_content(chain, label, observation)
            add(
                "label_assertion",
                f"{chain}|{content['address']}|{content['source_id']}|{content['label_id']}",
                content,
                None,
            )
        for address in facts.get("labels_withdrawn") or []:
            # The source no longer states a label it stated before: a new revision, never a silent quote.
            prefix = f"{chain}|{normalize_address(chain, address)}|{observation['provider']}|"
            rows = self.conn.execute(
                "SELECT record_key, content_json FROM onchain_current WHERE namespace=? AND "
                "kind='label_assertion' AND starts_with(record_key, ?)",
                [namespace, prefix],
            ).fetchall()
            for key, content_json in rows:
                prior = json.loads(content_json)
                if prior.get("state") != "withdrawn":
                    add("label_assertion", key, {**prior, "state": "withdrawn"}, None)
        for item in facts.get("coverage") or []:
            self.record_coverage(
                namespace,
                chain,
                {**item, "address": normalize_address(chain, item["address"])},
                oid,
                at,
            )
        return {
            "observation_id": oid,
            "namespace": namespace,
            "changes": changes,
            "added": len(changes),
            "principal_id": principal_id,
        }

    # ------------------------------------------------------------------ label references (reviewable)

    def propose_label_reference(
        self,
        namespace: str,
        label_record_key: str,
        canonical_id: str,
        *,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Offer: the label a source states refers to an organization or contract canonical entity.

        The reference is about the *label text*, never a statement that the
        entity controls the address. Person-typed or untyped entities are refused.
        """
        authorize(namespace, scopes, WRITE_SCOPE)
        if not str(reason or "").strip():
            raise OnchainError("invalid_reference", "a reference needs a reason")
        label = self.conn.execute(
            "SELECT content_json, chain_id FROM onchain_current WHERE namespace=? AND kind='label_assertion' AND "
            "record_key=?",
            [namespace, label_record_key],
        ).fetchone()
        if label is None:
            raise OnchainError(
                "unknown_label",
                "no current label assertion with that key in this namespace",
            )
        entity = (
            self.conn.execute(
                "SELECT entity_type FROM canonical_entities WHERE canonical_id=?",
                [canonical_id],
            ).fetchone()
            if table_exists(self.conn, "canonical_entities")
            else None
        )
        if entity is None:
            raise OnchainError("unknown_entity", "no canonical entity with that id")
        entity_type = str(entity[0] or "").strip().lower()
        if entity_type in PERSON_TYPES:
            raise OnchainError(
                "person_reference_refused",
                "addresses are never linked to person entities",
            )
        if entity_type not in REFERENCE_TYPES:
            raise OnchainError(
                "entity_type_refused",
                "only organization or contract entities can be referenced",
                entity_type=entity_type or None,
            )
        content = json.loads(label[0])
        reference_id = (
            "onchain-ref:" + digest([namespace, label_record_key, canonical_id])[:24]
        )
        row = self.conn.execute(
            "SELECT state FROM onchain_label_references WHERE namespace=? AND reference_id=?",
            [namespace, reference_id],
        ).fetchone()
        if row is not None:
            return {"reference_id": reference_id, "change": None, "state": row[0]}
        now = self.now()
        evidence = {
            "label_record_key": label_record_key,
            "label": content.get("label"),
            "stated_by": content.get("publisher"),
            "source_id": content.get("source_id"),
            "reason": reason.strip(),
        }
        self.conn.execute(
            "INSERT INTO onchain_label_references VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                reference_id,
                label_record_key,
                label[1],
                content["address"],
                canonical_id,
                entity_type,
                "candidate",
                None,
                0,
                canonical(evidence),
                canonical([{"state": "candidate", "by": principal_id, "at_ms": now}]),
                principal_id,
                now,
            ],
        )
        return {"reference_id": reference_id, "change": "created", "state": "candidate"}

    def label_reference(self, namespace: str, reference_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT reference_id, label_record_key, chain_id, address, canonical_id, entity_type, state, decision_id, "
            "review_seq, evidence_json, history_json FROM onchain_label_references WHERE namespace=? AND "
            "reference_id=?",
            [namespace, reference_id],
        ).fetchone()
        if row is None:
            raise OnchainError("not_found", "no label reference with that id")
        return {
            "reference_id": row[0],
            "label_record_key": row[1],
            "chain_id": row[2],
            "address": row[3],
            "canonical_id": row[4],
            "entity_type": row[5],
            "state": row[6],
            "decision_id": row[7],
            "review_seq": int(row[8]),
            "evidence": json.loads(row[9]),
            "history": json.loads(row[10]),
        }

    def _transition(
        self,
        namespace: str,
        ref: Mapping[str, Any],
        state: str,
        decision_id: str | None,
        principal_id: str,
        reason: str,
        change: str,
    ) -> dict[str, Any]:
        history = [
            *ref["history"],
            {
                "state": state,
                "by": principal_id,
                "reason": reason,
                "at_ms": self.now(),
                "decision_id": decision_id,
                "change": change,
            },
        ]
        self.conn.execute(
            "UPDATE onchain_label_references SET state=?, decision_id=?, review_seq=?, history_json=? WHERE "
            "namespace=? AND reference_id=?",
            [
                state,
                decision_id,
                ref["review_seq"] + 1,
                canonical(history),
                namespace,
                ref["reference_id"],
            ],
        )
        return self.label_reference(namespace, ref["reference_id"])

    def review_label_reference(
        self,
        namespace: str,
        reference_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Accept or reject a candidate as an entity identity decision (reversible)."""
        from src.kb.entity_history import EntityHistoryStore

        authorize(namespace, scopes, REVIEW_SCOPE)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise OnchainError("invalid_decision", "accept or reject with a reason")
        ref = self.label_reference(namespace, reference_id)
        if ref["state"] != "candidate":
            raise OnchainError(
                "invalid_state",
                f"reference is {ref['state']}; revert it before re-reviewing",
            )
        history = EntityHistoryStore(self.conn, now=self.now)
        label_entity = "onchain-label:" + digest(ref["label_record_key"])[:24]
        target = "canonical:" + ref["canonical_id"]
        for entity, alias in (
            (label_entity, ref["label_record_key"]),
            (target, ref["canonical_id"]),
        ):
            history.register_entity(
                namespace,
                entity,
                [alias],
                principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES,
            )
        recorded = history.decide(
            namespace,
            "match" if decision == "accept" else "non-match",
            [label_entity, target],
            {
                "reference_id": reference_id,
                "evidence": ref["evidence"],
                "reason": reason.strip(),
                "review_seq": ref["review_seq"],
                "provenance": {
                    "producer": "onchain.core",
                    "records": [ref["label_record_key"]],
                },
                "policy": {
                    "merge": False,
                    "note": "the label text refers to the entity; not a control or ownership "
                    "statement about the address",
                },
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"onchain-label-ref:{namespace}:{reference_id}:{ref['review_seq']}",
        )
        return self._transition(
            namespace,
            ref,
            "accepted" if decision == "accept" else "rejected",
            recorded["decision_id"],
            principal_id,
            reason.strip(),
            "reviewed",
        )

    def revert_label_reference(
        self,
        namespace: str,
        reference_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Undo the decision; the reference returns to ``candidate`` (an earlier acceptance never reactivates)."""
        from src.kb.entity_history import EntityHistoryStore

        authorize(namespace, scopes, REVIEW_SCOPE)
        if not str(reason or "").strip():
            raise OnchainError("invalid_decision", "a revert needs a reason")
        ref = self.label_reference(namespace, reference_id)
        if ref["state"] not in {"accepted", "rejected"}:
            raise OnchainError(
                "invalid_state",
                "only an accepted or rejected reference can be reverted",
            )
        undo = EntityHistoryStore(self.conn, now=self.now).undo(
            namespace,
            ref["decision_id"],
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
        )
        return self._transition(
            namespace,
            ref,
            "candidate",
            None,
            principal_id,
            reason.strip(),
            f"reverted ({undo['decision_id']})",
        )


# ---------------------------------------------------------------------- content builders


def _addr(chain: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    return normalize_address(chain, value)


def _value(value: Any) -> str | None:
    number = _int(value)
    return None if number is None else str(number)


def transaction_content(chain: str, tx: Mapping[str, Any]) -> dict[str, Any]:
    block = _int(tx.get("block_number"))
    confirmations = _int(tx.get("confirmations"))
    content: dict[str, Any] = {
        "chain_id": chain,
        "tx_hash": normalize_tx_hash(chain, tx["tx_hash"]),
        "block_number": block,
        "block_hash": (str(tx["block_hash"]).lower() if tx.get("block_hash") else None),
        "timestamp": tx.get("timestamp"),
        "status": tx.get("status"),
        "finality": finality(chain, confirmations, block),
        "state": "observed",
    }
    if family(chain) == "evm":
        content.update(
            {
                "from": _addr(chain, tx.get("from")),
                "to": _addr(chain, tx.get("to")),
                "value": _value(tx.get("value")),
                "unit": "wei",
                "contract_created": _addr(chain, tx.get("contract_created")),
            }
        )
    else:
        content.update(
            {
                "inputs": [
                    {
                        "address": _addr(chain, i.get("address")),
                        "value": _value(i.get("value")),
                        "prev_tx": i.get("prev_tx"),
                        "prev_index": _int(i.get("prev_index")),
                        "coinbase": bool(i.get("coinbase")),
                    }
                    for i in tx.get("inputs") or []
                ],
                "outputs": [
                    {
                        "address": _addr(chain, o.get("address")),
                        "value": _value(o.get("value")),
                        "index": _int(o.get("index")),
                    }
                    for o in tx.get("outputs") or []
                ],
                "unit": "sat",
            }
        )
    return content


def transfer_content(chain: str, tr: Mapping[str, Any]) -> dict[str, Any]:
    tx_hash = normalize_tx_hash(chain, tr["tx_hash"])
    token = _addr(chain, tr.get("token_contract"))
    frm, to, value = (
        _addr(chain, tr.get("from")),
        _addr(chain, tr.get("to")),
        _value(tr.get("value")),
    )
    log_index = _int(tr.get("log_index"))
    # Keyed by the log index when the explorer states one; otherwise by the transfer's own fields
    # (an explorer that omits log indexes cannot distinguish two identical transfers in one transaction).
    suffix = (
        f"log{log_index}"
        if log_index is not None
        else "t" + digest([token, frm, to, value])[:16]
    )
    block = _int(tr.get("block_number"))
    return {
        "chain_id": chain,
        "transfer_key": f"{tx_hash}|{suffix}",
        "tx_hash": tx_hash,
        "token_contract": token,
        "token_symbol_as_stated": tr.get("token_symbol"),
        "token_name_as_stated": tr.get("token_name"),
        "token_decimals_as_stated": _int(tr.get("token_decimals")),
        "standard": tr.get("standard"),
        "from": frm,
        "to": to,
        "value": value,
        "log_index": log_index,
        "block_number": block,
        "block_hash": (str(tr["block_hash"]).lower() if tr.get("block_hash") else None),
        "timestamp": tr.get("timestamp"),
        "finality": finality(chain, _int(tr.get("confirmations")), block),
        "state": "observed",
    }


def deployment_content(chain: str, dep: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "chain_id": chain,
        "contract_address": normalize_address(chain, dep["contract_address"]),
        "deployer": _addr(chain, dep.get("deployer")),
        "tx_hash": normalize_tx_hash(chain, dep["tx_hash"])
        if dep.get("tx_hash")
        else None,
        "block_number": _int(dep.get("block_number")),
        "timestamp": dep.get("timestamp"),
        "factory": _addr(chain, dep.get("factory")),
    }


def funding_content(chain: str, fund: Mapping[str, Any]) -> dict[str, Any]:
    inbound = [
        {
            "tx_hash": normalize_tx_hash(chain, i["tx_hash"]),
            "from": _addr(chain, i.get("from")),
            "value": _value(i.get("value")),
            "block_number": _int(i.get("block_number")),
            "timestamp": i.get("timestamp"),
            "via": i.get("via") or "transaction",
        }
        for i in fund.get("first_inbound") or []
    ]
    return {
        "chain_id": chain,
        "address": normalize_address(chain, fund["address"]),
        "subject_kind": fund.get("subject_kind") or "account",
        "window": {
            "from_block": _int((fund.get("window") or {}).get("from_block")),
            "to_block": _int((fund.get("window") or {}).get("to_block")),
        },
        "complete": bool(fund.get("complete")),
        "first_inbound": inbound,
        "internal_transfers": fund.get("internal_transfers") or "not_acquired",
        "unit": "wei" if family(chain) == "evm" else "sat",
    }


def label_content(
    chain: str, label: Mapping[str, Any], observation: Mapping[str, Any]
) -> dict[str, Any]:
    license_ = dict(observation.get("license") or {})
    return {
        "chain_id": chain,
        "address": normalize_address(chain, label["address"]),
        "label_id": str(label.get("label_id") or "label"),
        "label": str(label["label"]),
        "category_as_stated": label.get("category"),
        "attributes_as_stated": dict(label.get("attributes") or {}),
        "source_id": observation["provider"],
        "publisher": observation.get("publisher"),
        "dataset": observation.get("dataset"),
        "source_record": label.get("source_record"),
        "licence": license_.get("id"),
        "attribution": observation.get("attribution"),
        "redistribution": license_.get("redistribution"),
        "state": "stated",
    }


# ---------------------------------------------------------------------- reads


def _rows(conn: Any, sql: str, params: Sequence[Any]) -> tuple[list[tuple], int]:
    """Rows whose JSON decodes; a bad row is skipped and counted, never fatal."""
    good, bad = [], 0
    for row in conn.execute(sql, list(params)).fetchall():
        try:
            good.append((*row[:-1], json.loads(row[-1])))
        except (TypeError, ValueError):
            bad += 1
    return good, bad


def _loads(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _observation(conn: Any, observation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT record_json FROM onchain_observations WHERE observation_id=?",
        [observation_id],
    ).fetchone()
    try:
        return json.loads(row[0]) if row else None
    except (TypeError, ValueError):
        return None


def citation(
    conn: Any,
    chain_id: str,
    observation_id: str,
    *,
    kind: str,
    identifier: str | None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Explorer citation for one record: the human explorer page and the receipted observation."""
    cache = cache if cache is not None else {}
    if observation_id not in cache:
        cache[observation_id] = _observation(conn, observation_id) or {}
    record = cache[observation_id]
    return {
        "observation_id": observation_id,
        "provider": record.get("provider"),
        "publisher": record.get("publisher"),
        "api_locator": record.get("locator"),
        "explorer_url": explorer_url(chain_id, kind, identifier)
        if identifier
        else None,
        "observed_at": record.get("observed_at"),
        "cited": bool(record),
    }


def generation(conn: Any, namespace: str) -> str:
    """Changes whenever any record, coverage window or label reference in the namespace changes."""
    if not ready(conn):
        return "onchain-gen:not-ready"
    parts = [
        conn.execute(
            "SELECT kind, COUNT(*), MAX(observed_at_ms), SUM(revision) FROM onchain_revisions WHERE "
            "namespace=? GROUP BY kind ORDER BY kind",
            [namespace],
        ).fetchall(),
        conn.execute(
            "SELECT COUNT(*), MAX(observed_at_ms) FROM onchain_coverage WHERE namespace=?",
            [namespace],
        ).fetchall(),
    ]
    if table_exists(conn, "onchain_label_references"):
        parts.append(
            conn.execute(
                "SELECT COUNT(*), SUM(review_seq) FROM onchain_label_references WHERE "
                "namespace=?",
                [namespace],
            ).fetchall()
        )
    return "onchain-gen:" + digest([[list(map(str, r)) for r in p] for p in parts])[:24]


def labels_for(
    conn: Any, namespace: str, chain_id: str, addresses: Iterable[str]
) -> dict[str, list[dict[str, Any]]]:
    """Quoted label assertions per address: what each source states, with attribution."""
    out: dict[str, list[dict[str, Any]]] = {}
    wanted = sorted(set(addresses))
    if not wanted or not ready(conn):
        return out
    marks = ",".join("?" for _ in wanted)
    rows, _ = _rows(
        conn,
        "SELECT observation_id, content_json FROM onchain_current WHERE namespace=? AND "
        f"kind='label_assertion' AND chain_id=? AND json_extract_string(content_json, '$.address') IN "
        f"({marks}) ORDER BY record_key",
        [namespace, chain_id, *wanted],
    )
    cache: dict[str, Any] = {}
    for observation_id, content in rows:
        if content.get("state") == "withdrawn":
            continue  # the source no longer states it; history keeps the earlier revision
        out.setdefault(content["address"], []).append(
            {
                "quote": content["label"],
                "category_as_stated": content.get("category_as_stated"),
                "stated_by": content.get("publisher"),
                "source_id": content.get("source_id"),
                "licence": content.get("licence"),
                "attribution": content.get("attribution"),
                "citation": {
                    **citation(
                        conn,
                        chain_id,
                        observation_id,
                        kind="address",
                        identifier=None,
                        cache=cache,
                    ),
                    "source_record": content.get("source_record"),
                },
                "framing": "label as stated by the source; not a Noesis finding",
            }
        )
    return out


def _coverage(
    conn: Any, namespace: str, chain: str, address: str, operations: Sequence[str]
) -> list[dict[str, Any]]:
    marks = ",".join("?" for _ in operations)
    rows = conn.execute(
        "SELECT operation, from_block, to_block, complete, observation_id, observed_at_ms FROM onchain_coverage "
        f"WHERE namespace=? AND chain_id=? AND address=? AND operation IN ({marks}) ORDER BY from_block NULLS FIRST, "
        "to_block",
        [namespace, chain, address, *operations],
    ).fetchall()
    return [
        {
            "operation": r[0],
            "from_block": r[1],
            "to_block": r[2],
            "complete": bool(r[3]),
            "observation_id": r[4],
            "observed_at": iso_ms(int(r[5])),
        }
        for r in rows
    ]


def uncovered(
    windows: Sequence[Mapping[str, Any]], from_block: int | None, to_block: int | None
) -> list[dict]:
    """Requested block ranges no complete acquisition window covers (open ends stay open)."""
    lo = 0 if from_block is None else int(from_block)
    spans = sorted(
        (
            int(w["from_block"] or 0),
            int(w["to_block"]) if w["to_block"] is not None else None,
        )
        for w in windows
        if w["complete"]
    )
    gaps: list[dict[str, Any]] = []
    cursor = lo
    for start, end in spans:
        if to_block is not None and cursor > to_block:
            break
        if end is not None and end < cursor:
            continue
        if start > cursor:
            gaps.append(
                {
                    "from_block": cursor,
                    "to_block": start - 1
                    if to_block is None
                    else min(start - 1, to_block),
                }
            )
        if end is None:
            return gaps
        cursor = max(cursor, end + 1)
    if to_block is None or cursor <= to_block:
        gaps.append({"from_block": cursor, "to_block": to_block})
    return gaps


def _resolve(chain_id: str, address: str) -> tuple[str, str]:
    chain = normalize_chain(chain_id)
    return chain, normalize_address(chain, address)


def _envelope(n: int, method: str, assumptions: Sequence[str]) -> dict[str, Any]:
    return {"n": int(n), "method": method, "assumptions": list(assumptions)}


def address_observations(
    conn: Any,
    namespace: str,
    chain_id: str,
    address: str,
    *,
    from_block: int | None = None,
    to_block: int | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Cited transactions and transfers in and out of one address within a block range.

    Unknowns are explicit: block ranges no acquisition covered, truncated
    windows, internal transfers that were not acquired, and an address no
    acquisition ever covered returns ``status: unknown`` rather than an empty list.
    """
    if not ready(conn):
        return not_ready(address=address, chain_id=chain_id)
    try:
        chain, addr = _resolve(chain_id, address)
    except IdentifierError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}
    limit = max(1, min(int(limit or 200), 1000))
    coverage = _coverage(conn, namespace, chain, addr, ("address-transactions",))
    base = {
        "namespace": namespace,
        "chain_id": chain,
        "address": addr,
        "display_address": display_address(chain, addr),
        "from_block": from_block,
        "to_block": to_block,
        "generation": generation(conn, namespace),
        "attribution": NOT_ATTRIBUTION,
    }
    if not coverage:
        return {
            **base,
            "status": "unknown",
            "items": [],
            "coverage": [],
            "note": "no acquisition covers this address; nothing is known about it here, which is not the "
            "same as no activity",
            **_envelope(0, "stored explorer observations", []),
        }
    params: list[Any] = [namespace, chain, addr]
    clause = ""
    if from_block is not None:
        clause += " AND r.block_number >= ?"
        params.append(int(from_block))
    if to_block is not None:
        clause += " AND r.block_number <= ?"
        params.append(int(to_block))
    rows, skipped = _rows(
        conn,
        "SELECT DISTINCT c.kind, c.record_key, c.observation_id, c.revision, c.content_json FROM onchain_address_roles r "
        "JOIN onchain_current c USING(namespace, kind, record_key) WHERE r.namespace=? AND r.chain_id=? AND "
        f"r.address=? AND r.kind IN ('transaction','transfer'){clause} ORDER BY c.record_key",
        params,
    )
    cache: dict[str, Any] = {}
    items = []
    for kind, _key, observation_id, revision, content in rows:
        items.append(
            _item(conn, chain, addr, kind, content, observation_id, revision, cache)
        )
    items.sort(
        key=lambda i: (
            i["block_number"] is None,
            i["block_number"] or 0,
            i["tx_hash"],
            i["kind"],
        )
    )
    truncated = len(items) > limit
    items = items[:limit]
    counterparties = sorted({c for i in items for c in i["counterparties"]})
    gaps = uncovered(coverage, from_block, to_block)
    unknowns = []
    if gaps:
        unknowns.append(
            {
                "kind": "uncovered_block_ranges",
                "ranges": gaps,
                "note": "no complete acquisition window covers these blocks",
            }
        )
    if any(not w["complete"] for w in coverage):
        unknowns.append(
            {
                "kind": "truncated_windows",
                "windows": [w for w in coverage if not w["complete"]],
                "note": "a page budget ended before the window did",
            }
        )
    if family(chain) == "evm":
        unknowns.append(
            {
                "kind": "internal_transfers",
                "note": "value moved by contract-internal calls is not "
                "acquired and does not appear here",
            }
        )
    if any(i["state"] == "dropped" for i in items):
        unknowns.append(
            {
                "kind": "dropped_transactions",
                "note": "a later read no longer found these; history "
                "keeps the earlier revision",
            }
        )
    status = "observed" if items else "no_observed_activity_in_covered_range"
    return {
        **base,
        "status": status,
        "items": items,
        "count": len(items),
        "truncated": truncated,
        "counterparties": counterparties,
        "labels": labels_for(conn, namespace, chain, [addr, *counterparties]),
        "coverage": coverage,
        "unknowns": unknowns,
        "skipped_rows": skipped,
        **_envelope(
            len(items),
            "stored explorer observations, current revisions filtered by block range",
            [
                "explorer responses are reproduced as stated; nothing is inferred beyond them",
                "coverage is limited to explicit, bounded acquisition windows",
            ],
        ),
    }


def _item(
    conn: Any,
    chain: str,
    addr: str,
    kind: str,
    content: Mapping[str, Any],
    observation_id: str,
    revision: int,
    cache: dict[str, Any],
) -> dict[str, Any]:
    if kind == "transfer" or family(chain) == "evm":
        frm, to = content.get("from"), content.get("to")
        direction = (
            "self"
            if frm == addr and to == addr
            else "out"
            if frm == addr
            else "in"
            if to == addr
            else "other"
        )
        counterparties = [
            a for a in (to if direction == "out" else frm,) if a and a != addr
        ]
        value = content.get("value")
    else:
        ins = {i["address"] for i in content.get("inputs") or [] if i.get("address")}
        outs = {o["address"] for o in content.get("outputs") or [] if o.get("address")}
        direction = "out" if addr in ins else "in" if addr in outs else "other"
        counterparties = sorted((outs if direction == "out" else ins) - {addr})
        value = (
            str(
                sum(
                    int(o["value"] or 0)
                    for o in content.get("outputs") or []
                    if o.get("address") == addr
                )
            )
            if direction == "in"
            else None
        )
    return {
        "kind": kind,
        "tx_hash": content["tx_hash"],
        "block_number": content.get("block_number"),
        "timestamp": content.get("timestamp"),
        "direction": direction,
        "counterparties": counterparties,
        "value": value,
        "unit": content.get("unit")
        or ("token-base-units" if kind == "transfer" else None),
        "token_contract": content.get("token_contract"),
        "token_symbol_as_stated": content.get("token_symbol_as_stated"),
        "status": content.get("status"),
        "finality": content.get("finality"),
        "state": content.get("state"),
        "revision": revision,
        "citation": citation(
            conn,
            chain,
            observation_id,
            kind="tx",
            identifier=content["tx_hash"],
            cache=cache,
        ),
    }


def counterparties(
    conn: Any, namespace: str, chain_id: str, address: str, *, as_of_block: int
) -> dict[str, Any]:
    """Counterparties of an address as of a chain height, each with first/last block and cited transactions.

    Current revisions are selected first and then filtered to ``block_number
    <= as_of_block``, so a reorganised transaction counts where the chain now
    places it; dropped transactions are excluded and listed as unknowns.
    """
    result = address_observations(
        conn, namespace, chain_id, address, to_block=int(as_of_block), limit=1000
    )
    if result["status"] in {"not_ready", "unknown", "error"}:
        return {**result, "as_of_block": as_of_block}
    table: dict[str, dict[str, Any]] = {}
    for item in result["items"]:
        if item["state"] == "dropped":
            continue
        for other in item["counterparties"]:
            entry = table.setdefault(
                other,
                {
                    "address": other,
                    "display_address": display_address(result["chain_id"], other),
                    "count": 0,
                    "directions": set(),
                    "first_block": None,
                    "last_block": None,
                    "citations": [],
                },
            )
            entry["count"] += 1
            entry["directions"].add(item["direction"])
            block = item["block_number"]
            if block is not None:
                entry["first_block"] = (
                    block
                    if entry["first_block"] is None
                    else min(entry["first_block"], block)
                )
                entry["last_block"] = (
                    block
                    if entry["last_block"] is None
                    else max(entry["last_block"], block)
                )
            if len(entry["citations"]) < 3:
                entry["citations"].append(item["citation"])
    rows = [
        {**v, "directions": sorted(v["directions"])} for _, v in sorted(table.items())
    ]
    return {
        **{k: v for k, v in result.items() if k not in {"items", "counterparties"}},
        "as_of_block": as_of_block,
        "counterparties": rows,
        "count": len(rows),
        **_envelope(
            len(rows),
            "counterparties of current revisions with block_number <= as_of_block",
            ["a counterparty is a ledger address, never an identity"],
        ),
    }


def _current(
    conn: Any, namespace: str, kind: str, key: str
) -> tuple[str, int, dict[str, Any]] | None:
    row = conn.execute(
        "SELECT observation_id, revision, content_json FROM onchain_current WHERE namespace=? AND "
        "kind=? AND record_key=?",
        [namespace, kind, key],
    ).fetchone()
    if row is None:
        return None
    try:
        return row[0], int(row[1]), json.loads(row[2])
    except (TypeError, ValueError):
        return None


def _funding_hop(
    conn: Any, namespace: str, chain: str, address: str, cache: dict[str, Any]
) -> dict[str, Any]:
    found = _current(conn, namespace, "funding", f"{chain}|{address}")
    if found is None:
        return {
            "address": address,
            "display_address": display_address(chain, address),
            "status": "not_acquired",
            "note": "acquire this address's funding to extend the chain",
        }
    observation_id, revision, content = found
    first = content.get("first_inbound") or []
    status = (
        "funded"
        if first
        else "no_inbound_value_in_window"
        if content.get("complete")
        else "not_found_in_window"
    )
    return {
        "address": address,
        "display_address": display_address(chain, address),
        "status": status,
        "window": content.get("window"),
        "complete": content.get("complete"),
        "internal_transfers": content.get("internal_transfers"),
        "revision": revision,
        "first_inbound": [
            {
                **i,
                "citation": citation(
                    conn,
                    chain,
                    observation_id,
                    kind="tx",
                    identifier=i["tx_hash"],
                    cache=cache,
                ),
            }
            for i in first
        ],
    }


def contract_origin(
    conn: Any, namespace: str, chain_id: str, contract: str, *, max_hops: int = 3
) -> dict[str, Any]:
    """Cited deployer, the contract's first inbound funding and the deployer's first-funding chain.

    Each hop follows the earliest inbound value transfer of the previous
    address; a hop that was never acquired is reported as ``not_acquired``,
    never guessed. The chain is a sequence of ledger facts, not attribution.
    """
    if not ready(conn):
        return not_ready(contract=contract, chain_id=chain_id)
    try:
        chain, addr = _resolve(chain_id, contract)
    except IdentifierError as exc:
        return {"status": "error", "code": exc.code, "error": str(exc)}
    if family(chain) != "evm":
        return {
            "status": "error",
            "code": "unsupported_chain",
            "error": "contracts are EVM records",
        }
    max_hops = max(1, min(int(max_hops or 3), 5))
    cache: dict[str, Any] = {}
    base = {
        "namespace": namespace,
        "chain_id": chain,
        "contract": addr,
        "display_contract": display_address(chain, addr),
        "generation": generation(conn, namespace),
        "attribution": NOT_ATTRIBUTION,
    }
    found = _current(conn, namespace, "contract_deployment", f"{chain}|{addr}")
    if found is None:
        return {
            **base,
            "status": "unknown",
            "deployment": None,
            "note": "no deployment observation for this address; it may be an account, not a contract, or it "
            "was never acquired",
            **_envelope(0, "stored explorer observations", []),
        }
    observation_id, revision, dep = found
    deployment = {
        **dep,
        "revision": revision,
        "display_deployer": display_address(chain, dep["deployer"])
        if dep.get("deployer")
        else None,
        "citation": citation(
            conn,
            chain,
            observation_id,
            kind="tx",
            identifier=dep.get("tx_hash"),
            cache=cache,
        ),
    }
    contract_funding = _funding_hop(conn, namespace, chain, addr, cache)
    chain_hops: list[dict[str, Any]] = []
    unknowns: list[dict[str, Any]] = []
    seen = {addr}
    current = dep.get("deployer")
    while current and len(chain_hops) < max_hops:
        if current in seen:
            chain_hops.append({"address": current, "status": "cycle"})
            break
        seen.add(current)
        hop = _funding_hop(conn, namespace, chain, current, cache)
        chain_hops.append(hop)
        if hop["status"] != "funded":
            unknowns.append(
                {"kind": "funding_hop", "address": current, "status": hop["status"]}
            )
            break
        current = hop["first_inbound"][0]["from"]
    else:
        if current and len(chain_hops) >= max_hops:
            unknowns.append(
                {
                    "kind": "hop_limit",
                    "address": current,
                    "note": f"stopped after {max_hops} hops",
                }
            )
    if dep.get("deployer") is None:
        unknowns.append({"kind": "deployer", "note": "the explorer stated no deployer"})
    if contract_funding["status"] != "funded":
        unknowns.append(
            {"kind": "contract_funding", "status": contract_funding["status"]}
        )
    unknowns.append(
        {
            "kind": "internal_transfers",
            "note": "funding through contract-internal calls is not acquired",
        }
    )
    addresses = [
        addr,
        *(h["address"] for h in chain_hops),
        *(
            i["from"]
            for h in chain_hops
            for i in h.get("first_inbound") or []
            if i.get("from")
        ),
    ]
    return {
        **base,
        "status": "observed",
        "deployment": deployment,
        "contract_funding": contract_funding,
        "deployer_funding_chain": chain_hops,
        "unknowns": unknowns,
        "labels": labels_for(conn, namespace, chain, addresses),
        **_envelope(
            1 + len(chain_hops),
            "stored deployment and first-inbound funding observations",
            [
                "first inbound value is the earliest inbound transaction in an explicit window",
                "an exchange or bridge often funds many unrelated accounts; a funder is not a controller",
            ],
        ),
    }


def record_history(
    conn: Any, namespace: str, kind: str, record_key: str
) -> dict[str, Any]:
    """Every revision of one record in arrival order, with the one currently selected."""
    if not ready(conn):
        return not_ready(record_key=record_key)
    rows, skipped = _rows(
        conn,
        "SELECT revision, revision_id, as_of_block, observed_at_ms, observation_id, change, detail_json, "
        "content_json FROM onchain_revisions WHERE namespace=? AND kind=? AND record_key=? ORDER BY revision",
        [namespace, kind, record_key],
    )
    current = _current(conn, namespace, kind, record_key)
    if not rows:
        return {"status": "unknown", "record_key": record_key, "revisions": []}
    return {
        "status": "observed",
        "kind": kind,
        "record_key": record_key,
        "current_revision": current[1] if current else None,
        "skipped_rows": skipped,
        "revisions": [
            {
                "revision": r[0],
                "revision_id": r[1],
                "as_of_block": r[2],
                "observed_at": iso_ms(int(r[3])),
                "observation_id": r[4],
                "change": r[5],
                "detail": _loads(r[6]),
                "content": r[7],
            }
            for r in rows
        ],
    }


def revision_as_of(
    conn: Any, namespace: str, kind: str, record_key: str, *, as_of_block: int
) -> dict[str, Any] | None:
    """The revision that was current at a chain head: the latest one whose source position is <= ``as_of_block``."""
    if not ready(conn):
        return None
    row = conn.execute(
        "SELECT revision, content_json FROM onchain_revisions WHERE namespace=? AND kind=? AND record_key=? AND "
        "as_of_block IS NOT NULL AND as_of_block <= ? ORDER BY as_of_block DESC, observed_at_ms DESC, revision DESC "
        "LIMIT 1",
        [namespace, kind, record_key, int(as_of_block)],
    ).fetchone()
    return {"revision": int(row[0]), "content": json.loads(row[1])} if row else None


def trace_observation(conn: Any, observation_id: str) -> dict[str, Any]:
    """Provenance chain of one on-chain observation: source, receipt, observation, records citing it."""
    if not table_exists(conn, "onchain_observations"):
        return {"error": "no on-chain observations available", "code": "not_ready"}
    record = _observation(conn, observation_id)
    if record is None:
        return {
            "error": f"observation {observation_id!r} not found",
            "code": "not_found",
        }
    receipt = None
    if table_exists(conn, "onchain_acquisitions"):
        hit = conn.execute(
            "SELECT receipt_json FROM onchain_acquisitions WHERE request_id=?",
            [record["request_id"]],
        ).fetchone()
        receipt = json.loads(hit[0]) if hit else None
    revisions = [
        {
            "kind": r[0],
            "record_key": r[1],
            "revision": int(r[2]),
            "revision_id": r[3],
            "change": r[4],
        }
        for r in conn.execute(
            "SELECT kind, record_key, revision, revision_id, change FROM onchain_revisions "
            "WHERE observation_id=? ORDER BY kind, record_key, revision",
            [observation_id],
        ).fetchall()
    ]
    cite = {
        "observation_id": observation_id,
        "locator": record.get("locator"),
        "observed_at": record.get("observed_at"),
        "cited": True,
    }
    chain = [
        {
            "stage": "source",
            "source_pack_source": record["provider"],
            "publisher": record.get("publisher"),
            "license": record.get("license"),
            "redistribution": (record.get("license") or {}).get("redistribution"),
            "cite": cite,
        },
        {
            "stage": "acquisition",
            "request_id": record.get("request_id"),
            "status": (receipt or {}).get("status"),
            "adapter_version": (receipt or {}).get("adapter_version"),
            "retries": (receipt or {}).get("retries"),
            "receipt": receipt,
        },
        {
            "stage": "observation",
            "observation_id": observation_id,
            "contract": record.get("contract"),
            "operation": record.get("operation"),
            "subject": record.get("subject"),
            "chain_id": record.get("chain_id"),
            "head_block": (record.get("facts") or {}).get("head_block"),
            "raw_sha256": record.get("raw_sha256"),
            "cite": cite,
        },
        {"stage": "projection", "record_revisions": revisions},
    ]
    return {
        "artifact": {"type": "onchain-observation", "id": observation_id},
        "cited": True,
        "chain": chain,
        "stage_count": len(chain),
        "claims": [],
    }


def references_for_entity(
    conn: Any, namespace: str, canonical_id: str
) -> list[dict[str, Any]]:
    """Accepted label references of one organization or contract entity whose label is still stated."""
    if not table_exists(conn, "onchain_label_references") or not ready(conn):
        return []
    rows = conn.execute(
        "SELECT r.reference_id, r.label_record_key, r.chain_id, r.address, r.entity_type, r.decision_id FROM "
        "onchain_label_references r JOIN onchain_current c ON c.namespace=r.namespace AND "
        "c.kind='label_assertion' AND c.record_key=r.label_record_key WHERE r.namespace=? AND "
        "r.canonical_id=? AND r.state='accepted' AND "
        "coalesce(json_extract_string(c.content_json, '$.state'), 'stated') <> 'withdrawn' "
        "ORDER BY r.reference_id",
        [namespace, canonical_id],
    ).fetchall()
    return [
        {
            "reference_id": r[0],
            "label_record_key": r[1],
            "chain_id": r[2],
            "address": r[3],
            "entity_type": r[4],
            "decision_id": r[5],
        }
        for r in rows
        if str(r[4]).lower() not in PERSON_TYPES
    ]


def readiness(conn: Any, namespace: str) -> dict[str, Any]:
    if not ready(conn):
        return not_ready()
    counts = dict(
        conn.execute(
            "SELECT kind, COUNT(*) FROM onchain_current WHERE namespace=? GROUP BY kind",
            [namespace],
        ).fetchall()
    )
    return {
        "status": "ready" if counts else "empty",
        "namespace": namespace,
        "records": {k: int(counts.get(k, 0)) for k in KINDS},
        "generation": generation(conn, namespace),
    }
