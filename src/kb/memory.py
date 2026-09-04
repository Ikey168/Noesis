"""Provenance-native, tenant- and task-scoped knowledge memory."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Mapping, Sequence
from typing import Any

CONTRACT = "noesis-memory-object-v1"
READ_SCOPE = "knowledge:memory:read"
WRITE_SCOPE = "knowledge:memory:write"
ADMIN_SCOPE = "knowledge:memory:admin"
KINDS = frozenset({"episodic", "semantic", "preference", "task-state"})
EPISTEMIC = frozenset({"observation", "inferred-summary", "user-confirmed"})
STATES = frozenset({"active", "archived", "expired", "superseded", "forgotten"})
_TOKENS = re.compile(r"[^\W_]+", re.UNICODE)
_LOCK = threading.RLock()

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_memories (
  memory_id TEXT PRIMARY KEY, identity_hash TEXT NOT NULL UNIQUE,
  namespace TEXT NOT NULL, tenant_id TEXT NOT NULL, task_id TEXT,
  kind TEXT NOT NULL, subject_json TEXT NOT NULL, content_json TEXT NOT NULL,
  content_type TEXT NOT NULL, epistemic_status TEXT NOT NULL, author TEXT NOT NULL,
  evidence_json TEXT NOT NULL, confidence DOUBLE NOT NULL, valid_from_ms BIGINT,
  valid_to_ms BIGINT, sensitivity TEXT NOT NULL, lifecycle_json TEXT NOT NULL,
  state TEXT NOT NULL, revision BIGINT NOT NULL, supersedes_id TEXT,
  created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_memory_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, source_id TEXT NOT NULL,
  relation TEXT NOT NULL, target_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_memory_policies (
  namespace TEXT NOT NULL, tenant_id TEXT NOT NULL, task_id TEXT,
  policy_json TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, tenant_id, task_id)
);
CREATE TABLE IF NOT EXISTS knowledge_memory_idempotency (
  idempotency_key TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
  result_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_memory_audit (
  event_id TEXT PRIMARY KEY, memory_id TEXT, namespace TEXT NOT NULL,
  principal_id TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_knowledge_memory_scope
  ON knowledge_memories(namespace, tenant_id, task_id, state, kind);
CREATE INDEX IF NOT EXISTS idx_knowledge_memory_subject
  ON knowledge_memories(namespace, tenant_id, subject_json, state);
"""


class MemoryError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message); self.code,self.message,self.details=code,message,details
    def as_dict(self) -> dict[str,Any]:
        result={"code":self.code,"message":self.message}
        if self.details: result["details"]=self.details
        return result


def _canonical(value: Any) -> str: return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _digest(value: Any) -> str: return hashlib.sha256(_canonical(value).encode()).hexdigest()
def _now() -> int: return int(time.time()*1000)
def _load(value: Any, default: Any) -> Any: return default if value is None else json.loads(value) if isinstance(value,str) else value
def _terms(value: Any) -> set[str]: return {term.casefold() for term in _TOKENS.findall(_canonical(value))}


def _authorize(scopes: set[str], required: str, namespace: str, tenant_id: str) -> None:
    if "operator" in scopes: return
    if required not in scopes or f"namespace:{namespace}:read" not in scopes and f"namespace:{namespace}:write" not in scopes or f"tenant:{tenant_id}" not in scopes:
        raise MemoryError("unauthorized","memory scope is not authorized")


class MemoryStore:
    def __init__(self, conn: Any, *, initialize: bool=True) -> None:
        self.conn=conn
        if initialize: conn.execute(_DDL)

    @staticmethod
    def _validate(value: Mapping[str,Any]) -> dict[str,Any]:
        kind=str(value.get("kind", "")); epistemic=str(value.get("epistemic_status", "")); scope=dict(value.get("scope") or {}); subject=value.get("subject")
        if kind not in KINDS: raise MemoryError("invalid_kind",f"memory kind must be one of {sorted(KINDS)}")
        if epistemic not in EPISTEMIC: raise MemoryError("invalid_epistemic_status","observation, inferred-summary, or user-confirmed is required")
        if not scope.get("namespace") or not scope.get("tenant_id") or not subject: raise MemoryError("invalid_memory","namespace, tenant, and subject are required")
        confidence=float(value.get("confidence",0.5))
        if not 0<=confidence<=1: raise MemoryError("invalid_confidence","confidence must be between zero and one")
        if epistemic=="inferred-summary" and not value.get("evidence"): raise MemoryError("evidence_required","inferred summaries require source evidence")
        return {"kind":kind,"epistemic_status":epistemic,"scope":{"namespace":str(scope["namespace"]),"tenant_id":str(scope["tenant_id"]),"task_id":None if scope.get("task_id") is None else str(scope["task_id"])},"subject":subject,"content":value.get("content"),"content_type":str(value.get("content_type") or ("reference" if isinstance(value.get("content"),Mapping) and "ref" in value.get("content",{}) else "value")),"author":str(value.get("author") or "unknown"),"evidence":list(value.get("evidence") or []),"confidence":confidence,"valid_from_ms":value.get("valid_from_ms"),"valid_to_ms":value.get("valid_to_ms"),"sensitivity":str(value.get("sensitivity") or "private"),"lifecycle":dict(value.get("lifecycle") or {})}

    @staticmethod
    def _row(row: Sequence[Any]) -> dict[str,Any]:
        return {"contract":CONTRACT,"memory_id":row[0],"scope":{"namespace":row[2],"tenant_id":row[3],"task_id":row[4]},"kind":row[5],"subject":_load(row[6],{}),"content":_load(row[7],None),"content_type":row[8],"epistemic_status":row[9],"author":row[10],"evidence":_load(row[11],[]),"confidence":float(row[12]),"valid_from_ms":row[13],"valid_to_ms":row[14],"sensitivity":row[15],"lifecycle":_load(row[16],{}),"state":row[17],"revision":int(row[18]),"supersedes_id":row[19],"created_at_ms":int(row[20]),"updated_at_ms":int(row[21])}

    def _find(self,memory_id: str) -> tuple[Any,...]:
        row=self.conn.execute("SELECT * FROM knowledge_memories WHERE memory_id=?",[memory_id]).fetchone()
        if not row: raise MemoryError("not_found","memory does not exist")
        return row

    def _audit(self,memory_id: str | None,namespace: str,principal: str,action: str,detail: Mapping[str,Any]) -> None:
        now=_now(); identity=[memory_id,namespace,principal,action,detail,now]
        self.conn.execute("INSERT INTO knowledge_memory_audit VALUES (?,?,?,?,?,?,?)",["memory-audit:"+_digest(identity)[:24],memory_id,namespace,principal,action,_canonical(detail),now])

    def remember(self, value: Mapping[str,Any], idempotency_key: str, *, principal_id: str, scopes: set[str], created_at_ms: int | None=None, supersedes_id: str | None=None) -> dict[str,Any]:
        normalized=self._validate(value); scope=normalized["scope"]; _authorize(scopes,WRITE_SCOPE,scope["namespace"],scope["tenant_id"])
        request_hash=_digest({"value":normalized,"principal":principal_id,"supersedes":supersedes_id}); prior=self.conn.execute("SELECT request_hash,result_json FROM knowledge_memory_idempotency WHERE idempotency_key=?",[idempotency_key]).fetchone()
        if prior:
            if prior[0]!=request_hash: raise MemoryError("idempotency_conflict","idempotency key was reused")
            return json.loads(prior[1])
        identity={"scope":scope,"kind":normalized["kind"],"subject":normalized["subject"],"content":normalized["content"],"epistemic_status":normalized["epistemic_status"],"author":normalized["author"]}; identity_hash=_digest(identity)
        duplicate=self.conn.execute("SELECT * FROM knowledge_memories WHERE identity_hash=?",[identity_hash]).fetchone()
        if duplicate:
            result=self._row(duplicate); result["deduplicated"]=True
            self.conn.execute("INSERT INTO knowledge_memory_idempotency VALUES (?,?,?,?)",[idempotency_key,request_hash,_canonical(result),created_at_ms or _now()]); return result
        now=created_at_ms or _now(); memory_id="memory:"+identity_hash[:24]
        with _LOCK:
            self.conn.execute("BEGIN")
            try:
                self.conn.execute("INSERT INTO knowledge_memories VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',1,?,?,?)",[memory_id,identity_hash,scope["namespace"],scope["tenant_id"],scope["task_id"],normalized["kind"],_canonical(normalized["subject"]),_canonical(normalized["content"]),normalized["content_type"],normalized["epistemic_status"],normalized["author"],_canonical(normalized["evidence"]),normalized["confidence"],normalized["valid_from_ms"],normalized["valid_to_ms"],normalized["sensitivity"],_canonical(normalized["lifecycle"]),supersedes_id,now,now])
                result=self._row(self._find(memory_id)); self.conn.execute("INSERT INTO knowledge_memory_idempotency VALUES (?,?,?,?)",[idempotency_key,request_hash,_canonical(result),now]); self._audit(memory_id,scope["namespace"],principal_id,"remember",{"idempotency_key":idempotency_key}); self.conn.execute("COMMIT")
            except Exception: self.conn.execute("ROLLBACK"); raise
        return result

    def get(self,memory_id: str,*,principal_id: str,scopes: set[str]) -> dict[str,Any]:
        row=self._find(memory_id); _authorize(scopes,READ_SCOPE,row[2],row[3]); return self._row(row)

    def retrieve(self, query: Any, scope: Mapping[str,Any], *, principal_id: str, scopes: set[str], kinds: Sequence[str]=(), limit: int=20, at_ms: int | None=None, include_archived: bool=False) -> dict[str,Any]:
        namespace=str(scope.get("namespace", "")); tenant=str(scope.get("tenant_id", "")); task=scope.get("task_id"); _authorize(scopes,READ_SCOPE,namespace,tenant); now=at_ms or _now(); allowed_states=("active","archived") if include_archived else ("active",)
        rows=self.conn.execute("SELECT * FROM knowledge_memories WHERE namespace=? AND tenant_id=? AND (task_id IS NULL OR task_id=?) AND state IN (SELECT unnest(?)) ORDER BY memory_id",[namespace,tenant,task,list(allowed_states)]).fetchall(); query_terms=_terms(query); results=[]
        for row in rows:
            value=self._row(row)
            if kinds and value["kind"] not in kinds: continue
            if value["valid_from_ms"] is not None and now<value["valid_from_ms"] or value["valid_to_ms"] is not None and now>=value["valid_to_ms"]: continue
            terms=_terms({"subject":value["subject"],"content":value["content"]}); relevance=len(query_terms&terms)/max(1,len(query_terms)); age=max(0,now-value["updated_at_ms"]); half_life=int(value["lifecycle"].get("decay_half_life_ms",30*86_400_000)); recency=math.pow(.5,age/max(1,half_life)); evidence=min(1,len(value["evidence"])/2); scope_score=1 if value["scope"]["task_id"]==task and task is not None else .6
            score=.45*relevance+.2*recency+.2*value["confidence"]+.1*scope_score+.05*evidence
            results.append({"memory":value,"score":round(score,6),"explanation":{"relevance":round(relevance,6),"recency_priority":round(recency,6),"historical_confidence":value["confidence"],"evidence_support":evidence,"scope_match":scope_score,"decay_does_not_rewrite_confidence":True}})
        results.sort(key=lambda item:(-item["score"],item["memory"]["memory_id"])); return {"contract":"noesis-memory-retrieval-v1","scope":{"namespace":namespace,"tenant_id":tenant,"task_id":task},"results":results[:max(1,min(limit,100))],"total_candidates":len(results)}

    def correct(self,memory_id: str,replacement: Mapping[str,Any],idempotency_key: str,*,principal_id: str,scopes: set[str],reason: str) -> dict[str,Any]:
        old=self.get(memory_id,principal_id=principal_id,scopes=scopes); merged={key:value for key,value in old.items() if key in {"kind","subject","content","content_type","epistemic_status","author","evidence","confidence","valid_from_ms","valid_to_ms","sensitivity","lifecycle"}}; merged["scope"]=old["scope"]; merged.update(replacement)
        new=self.remember(merged,idempotency_key,principal_id=principal_id,scopes=scopes,supersedes_id=memory_id)
        if new["memory_id"]==memory_id: raise MemoryError("no_change","correction must change memory content or status")
        now=_now(); link_id="memory-link:"+_digest([new["memory_id"],"corrects",memory_id])[:24]
        with _LOCK:
            self.conn.execute("UPDATE knowledge_memories SET state='superseded',revision=revision+1,updated_at_ms=? WHERE memory_id=?",[now,memory_id]); self.conn.execute("INSERT OR IGNORE INTO knowledge_memory_links VALUES (?,?,?,?,?,?,?)",[link_id,old["scope"]["namespace"],new["memory_id"],"corrects",memory_id,_canonical({"reason":reason}),now]); self._audit(memory_id,old["scope"]["namespace"],principal_id,"correct",{"replacement":new["memory_id"],"reason":reason})
        return {"before":self.get(memory_id,principal_id=principal_id,scopes=scopes),"after":new,"relation":"corrects"}

    def set_policy(self, scope: Mapping[str,Any], policy: Mapping[str,Any], *, principal_id: str, scopes: set[str]) -> dict[str,Any]:
        namespace=str(scope.get("namespace","")); tenant=str(scope.get("tenant_id","")); task=scope.get("task_id"); _authorize(scopes,ADMIN_SCOPE,namespace,tenant)
        allowed={"retention_ms","archive_after_ms","expires_after_ms","legal_hold","maximum_sensitivity","decay_half_life_ms"}
        if set(policy)-allowed: raise MemoryError("invalid_policy","unsupported lifecycle policy field")
        now=_now(); self.conn.execute("INSERT OR REPLACE INTO knowledge_memory_policies VALUES (?,?,?,?,?)",[namespace,tenant,task,_canonical(policy),now]); self._audit(None,namespace,principal_id,"set-policy",policy); return {"scope":{"namespace":namespace,"tenant_id":tenant,"task_id":task},"policy":dict(policy),"updated_at_ms":now}

    def apply_lifecycle(self, scope: Mapping[str,Any], *, principal_id: str, scopes: set[str], at_ms: int | None=None) -> dict[str,Any]:
        namespace=str(scope.get("namespace","")); tenant=str(scope.get("tenant_id","")); task=scope.get("task_id"); _authorize(scopes,ADMIN_SCOPE,namespace,tenant); now=at_ms or _now(); policy_row=self.conn.execute("SELECT policy_json FROM knowledge_memory_policies WHERE namespace=? AND tenant_id=? AND task_id IS NOT DISTINCT FROM ?",[namespace,tenant,task]).fetchone(); policy=_load(policy_row[0],{}) if policy_row else {}
        if policy.get("legal_hold"): return {"archived":0,"expired":0,"legal_hold":True}
        rows=self.conn.execute("SELECT memory_id,created_at_ms,state FROM knowledge_memories WHERE namespace=? AND tenant_id=? AND (task_id IS NULL OR task_id=?)",[namespace,tenant,task]).fetchall(); archived=expired=0
        for memory_id,created,state in rows:
            age=now-int(created); target=None
            if policy.get("expires_after_ms") is not None and age>=int(policy["expires_after_ms"]): target="expired"; expired+=state!="expired"
            elif policy.get("archive_after_ms") is not None and age>=int(policy["archive_after_ms"]): target="archived"; archived+=state!="archived"
            if target: self.conn.execute("UPDATE knowledge_memories SET state=?,revision=revision+1,updated_at_ms=? WHERE memory_id=?",[target,now,memory_id])
        self._audit(None,namespace,principal_id,"apply-lifecycle",{"archived":archived,"expired":expired}); return {"archived":archived,"expired":expired,"legal_hold":False}

    def forget(self,memory_id: str,*,principal_id: str,scopes: set[str],reason: str) -> dict[str,Any]:
        value=self.get(memory_id,principal_id=principal_id,scopes=scopes); _authorize(scopes,WRITE_SCOPE,value["scope"]["namespace"],value["scope"]["tenant_id"]); policy_row=self.conn.execute("SELECT policy_json FROM knowledge_memory_policies WHERE namespace=? AND tenant_id=? AND task_id IS NOT DISTINCT FROM ?",[value["scope"]["namespace"],value["scope"]["tenant_id"],value["scope"]["task_id"]]).fetchone(); policy=_load(policy_row[0],{}) if policy_row else {}
        if policy.get("legal_hold"): raise MemoryError("legal_hold","memory cannot be forgotten under legal hold")
        now=_now(); self.conn.execute("UPDATE knowledge_memories SET state='forgotten',revision=revision+1,updated_at_ms=? WHERE memory_id=?",[now,memory_id]); self._audit(memory_id,value["scope"]["namespace"],principal_id,"forget",{"reason":reason}); return {"memory_id":memory_id,"state":"forgotten","history_preserved":True}

    def contradictions(self,memory_id: str,*,principal_id: str,scopes: set[str],record: bool=False) -> dict[str,Any]:
        value=self.get(memory_id,principal_id=principal_id,scopes=scopes); rows=self.conn.execute("SELECT * FROM knowledge_memories WHERE namespace=? AND tenant_id=? AND subject_json=? AND memory_id<>? AND state='active' ORDER BY memory_id",[value["scope"]["namespace"],value["scope"]["tenant_id"],_canonical(value["subject"]),memory_id]).fetchall(); conflicts=[]
        for row in rows:
            other=self._row(row)
            if _canonical(other["content"])==_canonical(value["content"]): continue
            conflict={"memory_id":other["memory_id"],"content":other["content"],"resolution":"required"}; conflicts.append(conflict)
            if record:
                link_id="memory-link:"+_digest(sorted([memory_id,other["memory_id"]])+["contradicts"])[:24]; self.conn.execute("INSERT OR IGNORE INTO knowledge_memory_links VALUES (?,?,?,?,?,?,?)",[link_id,value["scope"]["namespace"],memory_id,"contradicts",other["memory_id"],_canonical({"resolution":"required"}),_now()])
        return {"memory_id":memory_id,"contradictions":conflicts,"silently_replaced":False}

    def consolidate(self,memory_ids: Sequence[str], summary: Any, idempotency_key: str, *, principal_id: str, scopes: set[str]) -> dict[str,Any]:
        if len(set(memory_ids))<2: raise MemoryError("insufficient_memories","consolidation requires at least two source memories")
        sources=[self.get(mid,principal_id=principal_id,scopes=scopes) for mid in sorted(set(memory_ids))]; first=sources[0]
        if any(item["scope"]!=first["scope"] or item["subject"]!=first["subject"] for item in sources): raise MemoryError("incompatible_memories","consolidation requires one scope and subject")
        value={"kind":"semantic","scope":first["scope"],"subject":first["subject"],"content":summary,"content_type":"value","epistemic_status":"inferred-summary","author":principal_id,"evidence":[{"memory_id":item["memory_id"]} for item in sources],"confidence":sum(item["confidence"] for item in sources)/len(sources),"sensitivity":max(item["sensitivity"] for item in sources),"lifecycle":first["lifecycle"]}; result=self.remember(value,idempotency_key,principal_id=principal_id,scopes=scopes)
        now=_now()
        for source in sources:
            link_id="memory-link:"+_digest([result["memory_id"],"consolidates",source["memory_id"]])[:24]; self.conn.execute("INSERT OR IGNORE INTO knowledge_memory_links VALUES (?,?,?,?,?,?,?)",[link_id,first["scope"]["namespace"],result["memory_id"],"consolidates",source["memory_id"],"{}",now])
        return {"summary":result,"source_memories":[item["memory_id"] for item in sources],"sources_preserved":True}

    def export_standard_mcp(self, scope: Mapping[str,Any], *, principal_id: str, scopes: set[str], limit: int=1000) -> dict[str,Any]:
        result=self.retrieve("",scope,principal_id=principal_id,scopes=scopes,limit=min(limit,100)); entities=[]; unsupported=[]
        for item in result["results"]:
            memory=item["memory"]; subject=memory["subject"]; name=str(subject.get("id") or subject.get("name") if isinstance(subject,Mapping) else subject)
            entities.append({"name":name,"entityType":memory["kind"],"observations":[_canonical(memory["content"])]})
            unsupported.append({"memory_id":memory["memory_id"],"fields":["confidence","evidence","validity","sensitivity","lifecycle"]})
        links=self.conn.execute("SELECT source_id,relation,target_id FROM knowledge_memory_links WHERE namespace=? ORDER BY link_id",[scope["namespace"]]).fetchall(); relations=[{"from":r[0],"relationType":r[1],"to":r[2]} for r in links]
        return {"entities":entities,"relations":relations,"mapping_report":{"exported":len(entities),"unsupported":unsupported,"conflict_behavior":"distinct observations are preserved"}}

    def import_standard_mcp(self,payload: Mapping[str,Any],scope: Mapping[str,Any],idempotency_prefix: str,*,principal_id: str,scopes: set[str],token_budget: int=10000) -> dict[str,Any]:
        encoded=_canonical(payload)
        if len(encoded)>token_budget*4: raise MemoryError("token_budget_exceeded","standard MCP memory payload exceeds the bounded budget")
        imported=[]; unsupported=[]
        for entity in payload.get("entities",[]):
            for index,observation in enumerate(entity.get("observations",[])):
                value={"kind":"semantic" if entity.get("entityType") not in KINDS else entity["entityType"],"scope":dict(scope),"subject":{"name":entity.get("name")},"content":observation,"content_type":"value","epistemic_status":"observation","author":principal_id,"evidence":[{"source":"standard-mcp-memory"}],"confidence":.5,"sensitivity":"private"}
                imported.append(self.remember(value,f"{idempotency_prefix}:entity:{entity.get('name')}:{index}",principal_id=principal_id,scopes=scopes)["memory_id"])
            unsupported.extend(sorted(set(entity)-{"name","entityType","observations"}))
        return {"imported":imported,"mapping_report":{"entities":len(payload.get("entities",[])),"relations":len(payload.get("relations",[])),"unsupported_fields":sorted(set(unsupported)),"relations_preserved_as_report":list(payload.get("relations",[])),"conflict_behavior":"never merge non-identical observations"}}
