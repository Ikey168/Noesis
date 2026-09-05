"""Durable saved knowledge queries evaluated only at committed watermarks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

CONTRACT = "noesis-knowledge-subscription-v1"
EVENT_CONTRACT = "noesis-knowledge-subscription-event-v1"
SCHEMA_VERSION = 1
READ_SCOPE = "knowledge:subscriptions:read"
WRITE_SCOPE = "knowledge:subscriptions:write"
DELIVER_SCOPE = "knowledge:subscriptions:deliver"
DETERMINISTIC_OPERATIONS = frozenset({"objects", "relations", "search", "timeline", "evidence", "federated-query"})
EVENT_TYPES = frozenset({"added", "removed", "changed", "corrected", "coverage-degraded"})

_DDL = """
CREATE TABLE IF NOT EXISTS noesis_schema_migrations (
  component TEXT NOT NULL, version INTEGER NOT NULL, applied_at_ms BIGINT NOT NULL,
  PRIMARY KEY(component, version)
);
CREATE SEQUENCE IF NOT EXISTS knowledge_subscription_event_sequence START 1;
CREATE TABLE IF NOT EXISTS knowledge_subscriptions (
  subscription_id TEXT PRIMARY KEY, version BIGINT NOT NULL, query_json TEXT NOT NULL,
  namespace TEXT NOT NULL, domain TEXT NOT NULL, filters_json TEXT NOT NULL,
  owner_principal TEXT NOT NULL, cadence_json TEXT NOT NULL, delivery_json TEXT NOT NULL,
  status TEXT NOT NULL, expires_at_ms BIGINT, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL, last_watermark BIGINT,
  UNIQUE(owner_principal, namespace, query_json, filters_json)
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_snapshots (
  subscription_id TEXT NOT NULL, watermark BIGINT NOT NULL, result_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, coverage_json TEXT NOT NULL, captured_at_ms BIGINT NOT NULL,
  PRIMARY KEY(subscription_id, watermark)
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_events (
  sequence BIGINT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE, subscription_id TEXT NOT NULL,
  owner_principal TEXT NOT NULL, namespace TEXT NOT NULL, event_type TEXT NOT NULL,
  object_key TEXT NOT NULL, watermark BIGINT NOT NULL, before_json TEXT,
  after_json TEXT, evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_outbox (
  event_id TEXT NOT NULL, delivery_kind TEXT NOT NULL, destination_ref TEXT,
  payload_json TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL,
  available_at_ms BIGINT NOT NULL, delivered_at_ms BIGINT,
  PRIMARY KEY(event_id, delivery_kind)
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_watermarks (
  namespace TEXT NOT NULL, watermark BIGINT NOT NULL, kind TEXT NOT NULL,
  detail_json TEXT NOT NULL, committed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, watermark)
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_idempotency (
  idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_subscription_audit (
  event_id TEXT PRIMARY KEY, subscription_id TEXT, principal_id TEXT NOT NULL,
  action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_subscription_owner
  ON knowledge_subscriptions(owner_principal, namespace, status);
CREATE INDEX IF NOT EXISTS idx_knowledge_subscription_events
  ON knowledge_subscription_events(subscription_id, sequence);
"""


class SubscriptionError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message); self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details: result["details"] = self.details
        return result


def _canonical(value: Any) -> str: return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
def _digest(value: Any) -> str: return hashlib.sha256(_canonical(value).encode()).hexdigest()
def _now() -> int: return int(time.time() * 1000)
def _load(value: Any, default: Any) -> Any: return default if value is None else json.loads(value) if isinstance(value, str) else value


def _scope(scopes: set[str], required: str, namespace: str | None = None) -> None:
    if "operator" in scopes: return
    if required not in scopes: raise SubscriptionError("unauthorized", f"missing required scope {required}")
    if namespace and f"namespace:{namespace}:read" not in scopes and "namespace:*:read" not in scopes:
        raise SubscriptionError("unauthorized", f"namespace {namespace!r} is not authorized")


def _cursor(subscription_id: str, sequence: int) -> str:
    payload=_canonical({"subscription_id": subscription_id, "sequence": sequence}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str, subscription_id: str) -> int:
    if not value: return 0
    try:
        raw=base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)); payload=json.loads(raw)
        if not hmac.compare_digest(str(payload["subscription_id"]), subscription_id): raise ValueError
        return max(0, int(payload["sequence"]))
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SubscriptionError("invalid_cursor", "cursor is malformed or belongs to another subscription") from exc


class SubscriptionStore:
    def __init__(self, conn: Any, *, initialize: bool = True, max_active_per_principal: int = 100, poll_limit: int = 500, rate_limit_per_minute: int = 120) -> None:
        self.conn, self.max_active_per_principal, self.poll_limit, self.rate_limit = conn, max_active_per_principal, poll_limit, rate_limit_per_minute
        self._calls: dict[str, list[int]] = {}
        if initialize: self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(_DDL)
        self.conn.execute("INSERT OR IGNORE INTO noesis_schema_migrations VALUES (?, ?, ?)", ["knowledge-subscriptions", SCHEMA_VERSION, _now()])

    def _rate(self, principal: str) -> None:
        now=_now(); retained=[stamp for stamp in self._calls.get(principal, []) if now-stamp < 60_000]
        if len(retained) >= self.rate_limit: raise SubscriptionError("rate_limited", "subscription request rate exceeded")
        retained.append(now); self._calls[principal]=retained

    def _audit(self, subscription_id: str | None, principal: str, action: str, detail: Mapping[str, Any]) -> None:
        now=_now(); identity={"subscription_id":subscription_id,"principal":principal,"action":action,"detail":detail,"at":now}
        self.conn.execute("INSERT INTO knowledge_subscription_audit VALUES (?, ?, ?, ?, ?, ?)", ["audit:"+_digest(identity)[:24],subscription_id,principal,action,_canonical(detail),now])

    def commit_watermark(self, namespace: str, watermark: int, *, kind: str = "consolidation", detail: Mapping[str, Any] | None = None, committed_at_ms: int | None = None) -> dict[str, Any]:
        value=int(watermark)
        if value < 1 or kind not in {"consolidation","ingestion"}: raise SubscriptionError("invalid_watermark", "a positive committed consolidation or ingestion watermark is required")
        encoded=_canonical(detail or {}); existing=self.conn.execute("SELECT kind, detail_json FROM knowledge_subscription_watermarks WHERE namespace=? AND watermark=?", [namespace,value]).fetchone()
        if existing and (existing[0]!=kind or existing[1]!=encoded): raise SubscriptionError("watermark_conflict", "watermark already has different committed metadata")
        self.conn.execute("INSERT OR IGNORE INTO knowledge_subscription_watermarks VALUES (?, ?, ?, ?, ?)", [namespace,value,kind,encoded,committed_at_ms or _now()])
        return {"namespace":namespace,"watermark":value,"kind":kind,"status":"committed"}

    def create(self, definition: Mapping[str, Any], idempotency_key: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        namespace=str(definition.get("namespace", "")).strip(); _scope(scopes, WRITE_SCOPE, namespace); self._rate(principal_id)
        query=dict(definition.get("query") or {}); operation=str(query.get("operation", ""))
        if operation not in DETERMINISTIC_OPERATIONS or query.get("random") or query.get("now"):
            raise SubscriptionError("nondeterministic_query", "subscribed operation must be deterministic")
        if not namespace or not principal_id: raise SubscriptionError("invalid_subscription", "namespace and authenticated owner are required")
        request={"definition":definition,"principal_id":principal_id}; request_hash=_digest(request)
        prior=self.conn.execute("SELECT request_hash,result_json FROM knowledge_subscription_idempotency WHERE idempotency_key=?", [idempotency_key]).fetchone()
        if prior:
            if prior[0]!=request_hash: raise SubscriptionError("idempotency_conflict", "idempotency key was reused")
            return json.loads(prior[1])
        active=self.conn.execute("SELECT COUNT(*) FROM knowledge_subscriptions WHERE owner_principal=? AND status IN ('active','paused')", [principal_id]).fetchone()[0]
        if int(active)>=self.max_active_per_principal: raise SubscriptionError("quota_exceeded", "active subscription quota exceeded")
        cadence=dict(definition.get("cadence") or {"trigger":"watermark"}); delivery=dict(definition.get("delivery") or {"kind":"poll"})
        if cadence.get("trigger") not in {"watermark","interval","manual"}: raise SubscriptionError("invalid_cadence", "unsupported trigger policy")
        if delivery.get("kind") not in {"poll","webhook","email","queue"}: raise SubscriptionError("invalid_delivery", "unsupported delivery hook kind")
        identity={"owner":principal_id,"namespace":namespace,"query":query,"filters":definition.get("filters") or {}}
        subscription_id="subscription:"+_digest(identity)[:24]; now=_now()
        result={"contract":CONTRACT,"subscription_id":subscription_id,"version":1,"owner_principal":principal_id,"namespace":namespace,"domain":str(definition.get("domain") or "general"),"query":query,"filters":dict(definition.get("filters") or {}),"cadence":cadence,"delivery":delivery,"status":"active","expires_at_ms":definition.get("expires_at_ms"),"created_at_ms":now,"updated_at_ms":now,"last_watermark":None}
        self.conn.execute("INSERT INTO knowledge_subscriptions VALUES (?,1,?,?,?,?,?,?,?,'active',?,?,?,NULL)", [subscription_id,_canonical(query),namespace,result["domain"],_canonical(result["filters"]),principal_id,_canonical(cadence),_canonical(delivery),result["expires_at_ms"],now,now])
        self.conn.execute("INSERT INTO knowledge_subscription_idempotency VALUES (?, ?, ?, ?)", [idempotency_key,request_hash,_canonical(result),now]); self._audit(subscription_id,principal_id,"create",{"version":1})
        return result

    def _get(self, subscription_id: str, principal_id: str, scopes: set[str], *, write: bool = False) -> tuple[Any, ...]:
        row=self.conn.execute("SELECT subscription_id,version,query_json,namespace,domain,filters_json,owner_principal,cadence_json,delivery_json,status,expires_at_ms,created_at_ms,updated_at_ms,last_watermark FROM knowledge_subscriptions WHERE subscription_id=?", [subscription_id]).fetchone()
        if not row: raise SubscriptionError("not_found", "subscription does not exist")
        _scope(scopes, WRITE_SCOPE if write else READ_SCOPE, row[3])
        if row[6]!=principal_id and "operator" not in scopes: raise SubscriptionError("not_found", "subscription does not exist")
        return row

    @staticmethod
    def _value(row: Sequence[Any]) -> dict[str, Any]:
        return {"contract":CONTRACT,"subscription_id":row[0],"version":int(row[1]),"query":_load(row[2],{}),"namespace":row[3],"domain":row[4],"filters":_load(row[5],{}),"owner_principal":row[6],"cadence":_load(row[7],{}),"delivery":_load(row[8],{}),"status":row[9],"expires_at_ms":row[10],"created_at_ms":int(row[11]),"updated_at_ms":int(row[12]),"last_watermark":row[13]}

    def inspect(self, subscription_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]: return self._value(self._get(subscription_id,principal_id,scopes))

    def list(self, *, principal_id: str, scopes: set[str], namespace: str | None = None) -> list[dict[str, Any]]:
        _scope(scopes, READ_SCOPE, namespace); self._rate(principal_id)
        rows=self.conn.execute("SELECT subscription_id,version,query_json,namespace,domain,filters_json,owner_principal,cadence_json,delivery_json,status,expires_at_ms,created_at_ms,updated_at_ms,last_watermark FROM knowledge_subscriptions WHERE owner_principal=? AND (? IS NULL OR namespace=?) ORDER BY subscription_id", [principal_id,namespace,namespace]).fetchall()
        return [self._value(row) for row in rows]

    def update(self, subscription_id: str, patch: Mapping[str, Any], *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        row=self._get(subscription_id,principal_id,scopes,write=True); allowed={"filters","cadence","delivery","expires_at_ms"}
        if set(patch)-allowed: raise SubscriptionError("invalid_patch", "only filters, cadence, delivery, and expiration can be updated")
        current=self._value(row); current.update({key:dict(value) if key in {"filters","cadence","delivery"} else value for key,value in patch.items()})
        if current["delivery"].get("kind") not in {"poll","webhook","email","queue"}: raise SubscriptionError("invalid_delivery", "unsupported delivery hook kind")
        now=_now(); version=current["version"]+1
        self.conn.execute("UPDATE knowledge_subscriptions SET version=?,filters_json=?,cadence_json=?,delivery_json=?,expires_at_ms=?,updated_at_ms=? WHERE subscription_id=?", [version,_canonical(current["filters"]),_canonical(current["cadence"]),_canonical(current["delivery"]),current["expires_at_ms"],now,subscription_id]); self._audit(subscription_id,principal_id,"update",{"version":version}); return self.inspect(subscription_id,principal_id=principal_id,scopes=scopes)

    def set_status(self, subscription_id: str, status: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        if status not in {"active","paused"}: raise SubscriptionError("invalid_status", "status must be active or paused")
        self._get(subscription_id,principal_id,scopes,write=True); now=_now(); self.conn.execute("UPDATE knowledge_subscriptions SET status=?,version=version+1,updated_at_ms=? WHERE subscription_id=?", [status,now,subscription_id]); self._audit(subscription_id,principal_id,status,{}); return self.inspect(subscription_id,principal_id=principal_id,scopes=scopes)

    def delete(self, subscription_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        self._get(subscription_id,principal_id,scopes,write=True); self.conn.execute("UPDATE knowledge_subscriptions SET status='deleted',version=version+1,updated_at_ms=? WHERE subscription_id=?", [_now(),subscription_id]); self._audit(subscription_id,principal_id,"delete",{}); return {"subscription_id":subscription_id,"status":"deleted"}

    def transfer(self, subscription_id: str, new_owner: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        self._get(subscription_id,principal_id,scopes,write=True)
        if "operator" not in scopes: raise SubscriptionError("transfer_forbidden", "ownership transfer requires operator policy")
        self.conn.execute("UPDATE knowledge_subscriptions SET owner_principal=?,version=version+1,updated_at_ms=? WHERE subscription_id=?", [new_owner,_now(),subscription_id]); self._audit(subscription_id,principal_id,"transfer",{"new_owner":new_owner}); return self.inspect(subscription_id,principal_id=new_owner,scopes=scopes)

    @staticmethod
    def _normalize_results(result: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        items=result.get("items") if "items" in result else result.get("results", [])
        normalized={str(item.get("canonical_id") or item.get("identity") or item.get("id") or _digest(item)):dict(item) for item in items}
        coverage=dict(result.get("coverage") or {"complete":True}); return normalized,coverage

    def evaluate(self, subscription_id: str, watermark: int, result_or_evaluator: Mapping[str, Any] | Callable[[dict[str, Any]], Mapping[str, Any]], *, principal_id: str, scopes: set[str], observed_at_ms: int | None = None) -> dict[str, Any]:
        row=self._get(subscription_id,principal_id,scopes,write=True); subscription=self._value(row)
        if subscription["status"]!="active": return {"subscription_id":subscription_id,"status":"skipped","reason":subscription["status"]}
        now=observed_at_ms or _now()
        if subscription["expires_at_ms"] and now>=subscription["expires_at_ms"]: self.conn.execute("UPDATE knowledge_subscriptions SET status='expired' WHERE subscription_id=?",[subscription_id]); return {"subscription_id":subscription_id,"status":"expired"}
        committed=self.conn.execute("SELECT detail_json FROM knowledge_subscription_watermarks WHERE namespace=? AND watermark=?",[subscription["namespace"],int(watermark)]).fetchone()
        if not committed: raise SubscriptionError("watermark_uncommitted", "subscriptions only evaluate committed state")
        if subscription["last_watermark"] is not None and int(watermark)<int(subscription["last_watermark"]): return {"subscription_id":subscription_id,"status":"ignored","reason":"out-of-order","watermark":watermark}
        result=result_or_evaluator(subscription) if callable(result_or_evaluator) else result_or_evaluator
        current,coverage=self._normalize_results(result); result_hash=_digest({"items":current,"coverage":coverage})
        duplicate=self.conn.execute("SELECT result_json,coverage_json FROM knowledge_subscription_snapshots WHERE subscription_id=? AND watermark=?",[subscription_id,watermark]).fetchone()
        if duplicate:
            # Compare canonical content so snapshots written with the previous
            # items-only hash remain replayable without rewriting history.
            prior_hash=_digest({"items":_load(duplicate[0],{}),"coverage":_load(duplicate[1],{"complete":True})})
            if prior_hash!=result_hash: raise SubscriptionError("snapshot_conflict", "watermark replay returned different results or coverage")
            return {"subscription_id":subscription_id,"status":"replayed","watermark":watermark,"events":0}
        previous_row=self.conn.execute("SELECT result_json,coverage_json FROM knowledge_subscription_snapshots WHERE subscription_id=? ORDER BY watermark DESC LIMIT 1",[subscription_id]).fetchone(); previous=_load(previous_row[0],{}) if previous_row else {}; previous_coverage=_load(previous_row[1],{"complete":True}) if previous_row else {"complete":True}
        changes=[]
        for key in sorted(current.keys()-previous.keys()): changes.append(("added",key,None,current[key]))
        for key in sorted(previous.keys()-current.keys()): changes.append(("removed",key,previous[key],None))
        for key in sorted(current.keys()&previous.keys()):
            if _digest(current[key])!=_digest(previous[key]): changes.append(("corrected" if current[key].get("corrected") else "changed",key,previous[key],current[key]))
        if bool(previous_coverage.get("complete",True)) and not bool(coverage.get("complete",True)): changes.append(("coverage-degraded","__coverage__",previous_coverage,coverage))
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO knowledge_subscription_snapshots VALUES (?, ?, ?, ?, ?, ?)",[subscription_id,watermark,result_hash,_canonical(current),_canonical(coverage),now])
            event_ids=[]
            for event_type,key,before,after in changes:
                identity={"subscription_id":subscription_id,"watermark":watermark,"type":event_type,"key":key,"before":before,"after":after}; event_id="subscription-event:"+_digest(identity)[:24]; idempotency=_digest(identity)
                evidence={"query_hash":_digest(subscription["query"]),"committed_watermark":watermark,"watermark_detail":_load(committed[0],{}),"coverage":coverage}
                self.conn.execute("INSERT INTO knowledge_subscription_events VALUES (nextval('knowledge_subscription_event_sequence'),?,?,?,?,?,?,?,?,?,?,?,?)",[event_id,idempotency,subscription_id,subscription["owner_principal"],subscription["namespace"],event_type,key,watermark,None if before is None else _canonical(before),None if after is None else _canonical(after),_canonical(evidence),now]); event_ids.append(event_id)
                delivery=subscription["delivery"]
                if delivery.get("kind")!="poll": self.conn.execute("INSERT INTO knowledge_subscription_outbox VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)",[event_id,delivery["kind"],delivery.get("destination_ref"),_canonical({"contract":EVENT_CONTRACT,"event_id":event_id,"event_type":event_type,"object_key":key,"watermark":watermark}),now])
            self.conn.execute("UPDATE knowledge_subscriptions SET last_watermark=?,updated_at_ms=? WHERE subscription_id=?",[watermark,now,subscription_id]); self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK"); raise
        return {"subscription_id":subscription_id,"status":"evaluated","watermark":watermark,"events":len(changes),"event_ids":event_ids,"result_hash":result_hash}

    def poll(self, subscription_id: str, *, principal_id: str, scopes: set[str], cursor: str = "", limit: int = 100) -> dict[str, Any]:
        self._get(subscription_id,principal_id,scopes); self._rate(principal_id); after=_decode_cursor(cursor,subscription_id); capped=min(max(1,int(limit)),self.poll_limit)
        rows=self.conn.execute("SELECT sequence,event_id,event_type,object_key,watermark,before_json,after_json,evidence_json,created_at_ms FROM knowledge_subscription_events WHERE subscription_id=? AND sequence>? ORDER BY sequence LIMIT ?",[subscription_id,after,capped+1]).fetchall(); selected=rows[:capped]
        events=[{"contract":EVENT_CONTRACT,"sequence":int(row[0]),"event_id":row[1],"event_type":row[2],"object_key":row[3],"watermark":int(row[4]),"before":_load(row[5],None),"after":_load(row[6],None),"evidence":_load(row[7],{}),"created_at_ms":int(row[8])} for row in selected]
        sequence=int(selected[-1][0]) if selected else after
        return {"subscription_id":subscription_id,"events":events,"cursor":_cursor(subscription_id,sequence),"has_more":len(rows)>capped}

    def pending_deliveries(self, *, principal_id: str, scopes: set[str], limit: int = 100) -> list[dict[str, Any]]:
        _scope(scopes, DELIVER_SCOPE); rows=self.conn.execute("SELECT o.event_id,o.delivery_kind,o.destination_ref,o.payload_json,o.attempts FROM knowledge_subscription_outbox o JOIN knowledge_subscription_events e ON e.event_id=o.event_id WHERE e.owner_principal=? AND o.status='pending' AND o.available_at_ms<=? ORDER BY e.sequence LIMIT ?",[principal_id,_now(),min(int(limit),self.poll_limit)]).fetchall(); return [{"event_id":r[0],"delivery_kind":r[1],"destination_ref":r[2],"payload":_load(r[3],{}),"attempts":int(r[4])} for r in rows]
