"""Dependency-complete derived artifacts and atomic selective rebuilds."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from typing import Any

KINDS=frozenset({"source","chunk","enrichment","embedding","claim","entity","relation","summary","index","bundle"})
DEPENDENCY_KINDS=KINDS|frozenset({"parser","extractor","schema","model","configuration"})

_DDL="""
CREATE TABLE IF NOT EXISTS knowledge_artifacts (
  artifact_id TEXT PRIMARY KEY, logical_id TEXT NOT NULL, namespace TEXT NOT NULL,
  kind TEXT NOT NULL, generation BIGINT NOT NULL, content_json TEXT,
  content_hash TEXT NOT NULL, configuration_hash TEXT NOT NULL,
  producer_json TEXT NOT NULL, status TEXT NOT NULL, lineage_complete BOOLEAN NOT NULL,
  created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,logical_id,generation)
);
CREATE TABLE IF NOT EXISTS knowledge_artifact_dependencies (
  artifact_id TEXT NOT NULL, dependency_id TEXT NOT NULL, dependency_kind TEXT NOT NULL,
  dependency_content_hash TEXT, detail_json TEXT NOT NULL,
  PRIMARY KEY(artifact_id,dependency_id)
);
CREATE TABLE IF NOT EXISTS knowledge_artifact_rebuilds (
  rebuild_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, plan_hash TEXT NOT NULL,
  plan_json TEXT NOT NULL, status TEXT NOT NULL, completed_json TEXT NOT NULL,
  error_json TEXT, created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_artifact_watermarks (
  namespace TEXT PRIMARY KEY, watermark BIGINT NOT NULL, rebuild_id TEXT NOT NULL,
  committed_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifact_logical
  ON knowledge_artifacts(namespace,logical_id,status,generation);
CREATE INDEX IF NOT EXISTS idx_artifact_dependency
  ON knowledge_artifact_dependencies(dependency_id,artifact_id);
"""


class ArtifactError(RuntimeError):
    def __init__(self,code: str,message: str,**details: Any)->None:
        super().__init__(message);self.code,self.message,self.details=code,message,details


def _canonical(value: Any)->str:return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _digest(value: Any)->str:return hashlib.sha256(_canonical(value).encode()).hexdigest()
def _load(value: Any,default: Any)->Any:return default if value is None else json.loads(value) if isinstance(value,str) else value


class ArtifactGraph:
    def __init__(self,conn: Any,*,initialize: bool=True)->None:
        self.conn=conn
        if initialize:conn.execute(_DDL)

    def register(self,namespace: str,kind: str,logical_id: str,content: Any,*,configuration: Mapping[str,Any],producer: Mapping[str,Any],dependencies: Sequence[Mapping[str,Any]],lineage_complete: bool=True,now_ms: int|None=None,status: str="active",generation: int|None=None)->dict[str,Any]:
        if kind not in KINDS:raise ArtifactError("invalid_kind",f"artifact kind must be one of {sorted(KINDS)}")
        required_producer={"name","version"}
        if required_producer-set(producer):raise ArtifactError("invalid_producer","parser, extractor, model, or index producer name/version is required")
        content_hash=_digest(content);configuration_hash=_digest(configuration)
        if generation is None:
            row=self.conn.execute("SELECT COALESCE(MAX(generation),0)+1 FROM knowledge_artifacts WHERE namespace=? AND logical_id=?",[namespace,logical_id]).fetchone();generation=int(row[0])
        identity={"namespace":namespace,"logical_id":logical_id,"kind":kind,"generation":generation,"content_hash":content_hash,"configuration_hash":configuration_hash,"producer":producer};artifact_id="artifact:"+_digest(identity)[:24];now=now_ms or int(time.time()*1000)
        self.conn.execute("INSERT OR IGNORE INTO knowledge_artifacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",[artifact_id,logical_id,namespace,kind,generation,_canonical(content),content_hash,configuration_hash,_canonical(producer),status,lineage_complete,now])
        for dependency in dependencies:
            dependency_id=str(dependency.get("artifact_id") or dependency.get("dependency_id") or dependency.get("source_id") or "")
            dependency_kind=str(dependency.get("kind") or "source")
            if not dependency_id or dependency_kind not in DEPENDENCY_KINDS:raise ArtifactError("invalid_dependency","every dependency needs stable identity and kind")
            self.conn.execute("INSERT OR IGNORE INTO knowledge_artifact_dependencies VALUES (?,?,?,?,?)",[artifact_id,dependency_id,dependency_kind,dependency.get("content_hash"),_canonical(dependency.get("detail") or {})])
        return {"artifact_id":artifact_id,"logical_id":logical_id,"namespace":namespace,"kind":kind,"generation":generation,"content_hash":content_hash,"configuration_hash":configuration_hash,"producer":dict(producer),"dependencies":[dict(v) for v in dependencies],"lineage_complete":lineage_complete,"status":status}

    def inspect(self,artifact_id: str)->dict[str,Any]:
        row=self.conn.execute("SELECT artifact_id,logical_id,namespace,kind,generation,content_json,content_hash,configuration_hash,producer_json,status,lineage_complete,created_at_ms FROM knowledge_artifacts WHERE artifact_id=?",[artifact_id]).fetchone()
        if not row:raise ArtifactError("not_found","artifact does not exist")
        dependencies=self.conn.execute("SELECT dependency_id,dependency_kind,dependency_content_hash,detail_json FROM knowledge_artifact_dependencies WHERE artifact_id=? ORDER BY dependency_id",[artifact_id]).fetchall()
        return {"artifact_id":row[0],"logical_id":row[1],"namespace":row[2],"kind":row[3],"generation":int(row[4]),"content":_load(row[5],None),"content_hash":row[6],"configuration_hash":row[7],"producer":_load(row[8],{}),"status":row[9],"lineage_complete":bool(row[10]),"created_at_ms":int(row[11]),"dependencies":[{"dependency_id":v[0],"kind":v[1],"content_hash":v[2],"detail":_load(v[3],{})} for v in dependencies]}

    def upstream(self,artifact_id: str,*,transitive: bool=True)->dict[str,Any]:
        visited=set();queue=deque([artifact_id]);edges=[]
        while queue:
            child=queue.popleft()
            for row in self.conn.execute("SELECT dependency_id,dependency_kind,dependency_content_hash FROM knowledge_artifact_dependencies WHERE artifact_id=? ORDER BY dependency_id",[child]).fetchall():
                edge={"artifact_id":child,"dependency_id":row[0],"dependency_kind":row[1],"content_hash":row[2]};edges.append(edge)
                if transitive and row[0] not in visited and self.conn.execute("SELECT 1 FROM knowledge_artifacts WHERE artifact_id=?",[row[0]]).fetchone():visited.add(row[0]);queue.append(row[0])
        return {"artifact_id":artifact_id,"direction":"upstream","edges":edges,"incomplete":any(self.inspect(edge["artifact_id"])["lineage_complete"] is False for edge in edges)}

    def downstream(self,dependency_id: str,*,namespace: str|None=None)->dict[str,Any]:
        visited=set();queue=deque([dependency_id]);edges=[]
        while queue:
            parent=queue.popleft();rows=self.conn.execute("SELECT d.artifact_id,a.kind,a.namespace FROM knowledge_artifact_dependencies d JOIN knowledge_artifacts a ON a.artifact_id=d.artifact_id WHERE d.dependency_id=? AND (? IS NULL OR a.namespace=?) AND a.status='active' ORDER BY d.artifact_id",[parent,namespace,namespace]).fetchall()
            for artifact_id,kind,artifact_namespace in rows:
                edges.append({"dependency_id":parent,"artifact_id":artifact_id,"kind":kind,"namespace":artifact_namespace})
                if artifact_id not in visited:visited.add(artifact_id);queue.append(artifact_id)
        return {"dependency_id":dependency_id,"direction":"downstream","edges":edges,"affected":sorted(visited)}

    def preview_invalidation(self,namespace: str,changes: Sequence[Mapping[str,Any]])->dict[str,Any]:
        reasons={str(item["dependency_id"]):str(item.get("reason") or "content-changed") for item in changes};affected=set();parents=defaultdict(set);queue=deque(sorted(reasons))
        while queue:
            parent=queue.popleft()
            rows=self.conn.execute("SELECT d.artifact_id FROM knowledge_artifact_dependencies d JOIN knowledge_artifacts a ON a.artifact_id=d.artifact_id WHERE d.dependency_id=? AND a.namespace=? AND a.status='active' ORDER BY d.artifact_id",[parent,namespace]).fetchall()
            for (artifact_id,) in rows:
                parents[artifact_id].add(parent)
                if artifact_id not in affected:affected.add(artifact_id);queue.append(artifact_id)
        indegree={item:sum(parent in affected for parent in parents[item]) for item in affected};ready=deque(sorted(item for item,count in indegree.items() if count==0));order=[]
        while ready:
            item=ready.popleft();order.append(item)
            for child in sorted(v for v in affected if item in parents[v]):
                indegree[child]-=1
                if indegree[child]==0:ready.append(child)
        if len(order)!=len(affected):raise ArtifactError("dependency_cycle","artifact dependency graph contains a cycle")
        plan=[]
        for artifact_id in order:
            artifact=self.inspect(artifact_id);upstream=sorted(parents[artifact_id]);plan.append({"artifact_id":artifact_id,"logical_id":artifact["logical_id"],"kind":artifact["kind"],"generation":artifact["generation"],"upstream_changes":upstream,"reason":sorted({reasons.get(value,"upstream-invalidated") for value in upstream})})
        payload={"namespace":namespace,"changes":[dict(v) for v in changes],"artifacts":plan,"counts":{"affected":len(plan),"by_kind":{kind:sum(v["kind"]==kind for v in plan) for kind in KINDS}},"side_effect_free":True};payload["plan_hash"]=_digest(payload);return payload

    def rebuild(self,plan: Mapping[str,Any],builders: Mapping[str,Callable[[dict[str,Any]],Any]],*,cancelled: Callable[[],bool]|None=None,max_concurrency: int=4,fail_after: int|None=None,now_ms: int|None=None)->dict[str,Any]:
        namespace=str(plan["namespace"]);plan_hash=str(plan["plan_hash"]);rebuild_id="rebuild:"+plan_hash[:24];now=now_ms or int(time.time()*1000);prior=self.conn.execute("SELECT status,completed_json,error_json FROM knowledge_artifact_rebuilds WHERE rebuild_id=?",[rebuild_id]).fetchone();completed=set(_load(prior[1],[])) if prior else set()
        if prior and prior[0]=="committed":return {"rebuild_id":rebuild_id,"status":"committed","completed":sorted(completed),"watermark":self.watermark(namespace),"idempotent":True}
        self.conn.execute("INSERT OR IGNORE INTO knowledge_artifact_rebuilds VALUES (?,?,?,?,'building','[]',NULL,?,?)",[rebuild_id,namespace,plan_hash,_canonical(plan),now,now]);staged={}
        for item in plan["artifacts"]:
            if item["artifact_id"] not in completed:continue
            row=self.conn.execute("SELECT artifact_id FROM knowledge_artifacts WHERE namespace=? AND logical_id=? AND generation=? AND status='staged' ORDER BY artifact_id LIMIT 1",[namespace,item["logical_id"],int(item["generation"])+1]).fetchone()
            if row:staged[item["artifact_id"]]=row[0]
        if not plan["artifacts"]:
            self.conn.execute("UPDATE knowledge_artifact_rebuilds SET status='committed',updated_at_ms=? WHERE rebuild_id=?",[now,rebuild_id])
            return {"rebuild_id":rebuild_id,"status":"no-op","completed":[],"published":{},"watermark":self.watermark(namespace),"mixed_generations_visible":False}
        try:
            pending=[item for item in plan["artifacts"] if item["artifact_id"] not in completed]
            # Independent artifacts at the same topological frontier may compute concurrently;
            # publication and DuckDB writes remain serialized.
            for offset in range(0,len(pending),max(1,max_concurrency)):
                if cancelled and cancelled():raise ArtifactError("cancelled","rebuild cancelled before publication")
                group=pending[offset:offset+max(1,max_concurrency)]
                prepared=[(item,self.inspect(item["artifact_id"])) for item in group]
                def build(pair: tuple[Mapping[str,Any],dict[str,Any]]):
                    item,old=pair;builder=builders.get(old["kind"])
                    if builder is None:raise ArtifactError("builder_unavailable",f"no builder for {old['kind']}")
                    return item,old,builder(old)
                with concurrent.futures.ThreadPoolExecutor(max_workers=max(1,max_concurrency)) as executor:results=list(executor.map(build,prepared))
                for item,old,content in results:
                    dependencies=[{"dependency_id":staged.get(d["dependency_id"],d["dependency_id"]),"kind":d["kind"],"content_hash":d["content_hash"],"detail":d["detail"]} for d in old["dependencies"]];new=self.register(namespace,old["kind"],old["logical_id"],content,configuration={"previous_configuration_hash":old["configuration_hash"],"rebuild_plan":plan_hash},producer={"name":old["producer"]["name"],"version":old["producer"]["version"],"rebuild_of":old["artifact_id"]},dependencies=dependencies,lineage_complete=old["lineage_complete"],status="staged",generation=old["generation"]+1,now_ms=now);staged[old["artifact_id"]]=new["artifact_id"];completed.add(old["artifact_id"]);self.conn.execute("UPDATE knowledge_artifact_rebuilds SET completed_json=?,updated_at_ms=? WHERE rebuild_id=?",[_canonical(sorted(completed)),now,rebuild_id])
                    if fail_after is not None and len(completed)>=fail_after:raise ArtifactError("injected_failure","deterministic rebuild interruption")
            self.conn.execute("BEGIN")
            try:
                for old_id,new_id in staged.items():self.conn.execute("UPDATE knowledge_artifacts SET status='superseded' WHERE artifact_id=?",[old_id]);self.conn.execute("UPDATE knowledge_artifacts SET status='active' WHERE artifact_id=?",[new_id])
                watermark=self.watermark(namespace)+1;self.conn.execute("INSERT OR REPLACE INTO knowledge_artifact_watermarks VALUES (?,?,?,?)",[namespace,watermark,rebuild_id,now]);self.conn.execute("UPDATE knowledge_artifact_rebuilds SET status='committed',completed_json=?,error_json=NULL,updated_at_ms=? WHERE rebuild_id=?",[_canonical(sorted(completed)),now,rebuild_id]);self.conn.execute("COMMIT")
            except Exception:self.conn.execute("ROLLBACK");raise
            return {"rebuild_id":rebuild_id,"status":"committed","completed":sorted(completed),"published":staged,"watermark":watermark,"mixed_generations_visible":False}
        except Exception as exc:
            self.conn.execute("UPDATE knowledge_artifact_rebuilds SET status=?,error_json=?,updated_at_ms=? WHERE rebuild_id=?",["cancelled" if isinstance(exc,ArtifactError) and exc.code=="cancelled" else "failed",_canonical({"code":getattr(exc,"code","build_failed"),"message":str(exc)}),now,rebuild_id]);raise

    def watermark(self,namespace: str)->int:
        row=self.conn.execute("SELECT watermark FROM knowledge_artifact_watermarks WHERE namespace=?",[namespace]).fetchone();return int(row[0]) if row else 0
