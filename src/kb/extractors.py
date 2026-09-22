"""Versioned pluggable extraction with immutable output provenance."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

CONTRACT = "noesis-extractor-definition-v1"
OUTPUT_TYPES = frozenset({"entity","relation","claim","event","table","domain"})

_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_extractor_definitions (
  extractor_id TEXT PRIMARY KEY, name TEXT NOT NULL, semantic_version TEXT NOT NULL,
  definition_json TEXT NOT NULL, definition_hash TEXT NOT NULL,
  UNIQUE(name,semantic_version,definition_hash)
);
CREATE TABLE IF NOT EXISTS knowledge_extractor_outputs (
  output_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, input_id TEXT NOT NULL,
  input_revision TEXT NOT NULL, extractor_id TEXT NOT NULL, output_type TEXT NOT NULL,
  output_json TEXT, status TEXT NOT NULL, provenance_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS knowledge_extractor_runs (
  run_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, extractor_id TEXT NOT NULL,
  requested_json TEXT NOT NULL, result_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extractor_outputs_input
  ON knowledge_extractor_outputs(namespace,input_id,extractor_id,status);
"""


class ExtractorError(RuntimeError):
    def __init__(self,code: str,message: str,**details: Any) -> None:
        super().__init__(message); self.code,self.message,self.details=code,message,details


def _canonical(value: Any) -> str: return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _digest(value: Any) -> str: return hashlib.sha256(_canonical(value).encode()).hexdigest()


class Extractor(Protocol):
    def extract(self,value: Mapping[str,Any]) -> Sequence[Mapping[str,Any]]: ...


class ExtractorRegistry:
    def __init__(self,conn: Any,*,initialize: bool=True) -> None:
        self.conn=conn; self.implementations: dict[str,Extractor]={}
        if initialize: conn.execute(_DDL)

    @staticmethod
    def validate(definition: Mapping[str,Any]) -> dict[str,Any]:
        value=json.loads(json.dumps(definition)); required={"name","semantic_version","capabilities","accepted_object_types","output_schemas","implementation","configuration","resources"}
        if required-set(value): raise ExtractorError("invalid_definition","extractor definition is incomplete")
        if not set(value["capabilities"])<=OUTPUT_TYPES or not value["capabilities"]: raise ExtractorError("invalid_capability","extractor output capability is unsupported")
        implementation=value["implementation"]
        if not implementation.get("model_version") and not implementation.get("rule_version"): raise ExtractorError("version_required","model or rule version is required")
        value["configuration_hash"]=_digest(value["configuration"]); value["contract"]=CONTRACT; return value

    def register(self,definition: Mapping[str,Any],implementation: Extractor | None=None) -> dict[str,Any]:
        value=self.validate(definition); definition_hash=_digest(value); extractor_id=f"extractor:{value['name']}:{value['semantic_version']}:{definition_hash[:16]}"; value["extractor_id"]=extractor_id
        conflicts=self.conn.execute("SELECT definition_hash FROM knowledge_extractor_definitions WHERE name=? AND semantic_version=?",[value["name"],value["semantic_version"]]).fetchall()
        if conflicts and all(row[0]!=definition_hash for row in conflicts): raise ExtractorError("immutable_version","extractor version already has different content")
        self.conn.execute("INSERT OR IGNORE INTO knowledge_extractor_definitions VALUES (?,?,?,?,?)",[extractor_id,value["name"],value["semantic_version"],_canonical(value),definition_hash])
        if implementation is not None: self.implementations[extractor_id]=implementation
        return value

    def list(self) -> list[dict[str,Any]]:
        return [json.loads(row[0]) for row in self.conn.execute("SELECT definition_json FROM knowledge_extractor_definitions ORDER BY name,semantic_version,extractor_id").fetchall()]

    def run(self,extractor_id: str,namespace: str,inputs: Sequence[Mapping[str,Any]],*,select_ids: Sequence[str] | None=None,available: bool=True,now_ms: int | None=None) -> dict[str,Any]:
        row=self.conn.execute("SELECT definition_json FROM knowledge_extractor_definitions WHERE extractor_id=?",[extractor_id]).fetchone()
        if not row: raise ExtractorError("not_found","extractor is not registered")
        definition=json.loads(row[0]); implementation=self.implementations.get(extractor_id); selected=set(select_ids or []); now=now_ms or int(time.time()*1000); outputs=[]; failures=[]
        for value in sorted(inputs,key=lambda item:str(item.get("id"))):
            input_id=str(value.get("id", "")); object_type=str(value.get("object_type", "")); revision=str(value.get("revision") or _digest(value))
            if selected and input_id not in selected: continue
            if object_type not in definition["accepted_object_types"]: continue
            provenance={"extractor_id":extractor_id,"extractor_name":definition["name"],"extractor_version":definition["semantic_version"],"model_version":definition["implementation"].get("model_version"),"rule_version":definition["implementation"].get("rule_version"),"configuration_hash":definition["configuration_hash"],"input_id":input_id,"input_revision":revision}
            if not available or implementation is None:
                extracted=[]; status="unavailable"; failures.append({"input_id":input_id,"reason":"implementation-unavailable"})
            else:
                try: extracted=list(implementation.extract(value)); status="empty" if not extracted else "produced"
                except Exception as exc:  # noqa: BLE001 - isolate per-input extractor failures
                    extracted=[]; status="failed"; failures.append({"input_id":input_id,"reason":str(exc)[:300]})
            if not extracted:
                output_id="extraction:"+_digest([namespace,input_id,revision,extractor_id,status])[:24]; self.conn.execute("INSERT OR IGNORE INTO knowledge_extractor_outputs VALUES (?,?,?,?,?,?,NULL,?,?,?)",[output_id,namespace,input_id,revision,extractor_id,"none",status,_canonical(provenance),now]); outputs.append({"output_id":output_id,"input_id":input_id,"status":status,"provenance":provenance}); continue
            for index,item in enumerate(extracted):
                output_type=str(item.get("output_type", ""))
                if output_type not in definition["capabilities"]: failures.append({"input_id":input_id,"reason":f"undeclared output {output_type}"}); continue
                output_id="extraction:"+_digest([namespace,input_id,revision,extractor_id,index,item])[:24]; self.conn.execute("INSERT OR IGNORE INTO knowledge_extractor_outputs VALUES (?,?,?,?,?,?,?,?,?,?)",[output_id,namespace,input_id,revision,extractor_id,output_type,_canonical(item),"produced",_canonical(provenance),now]); outputs.append({"output_id":output_id,"input_id":input_id,"output":dict(item),"status":"produced","provenance":provenance})
        run_basis={"namespace":namespace,"extractor_id":extractor_id,"inputs":[str(v.get("id")) for v in inputs],"selected":sorted(selected)}; run_id="extractor-run:"+_digest(run_basis)[:24]; result={"run_id":run_id,"extractor_id":extractor_id,"outputs":outputs,"failures":failures,"counts":{"inputs":len(inputs),"outputs":len(outputs),"failures":len(failures)}}; self.conn.execute("INSERT OR REPLACE INTO knowledge_extractor_runs VALUES (?,?,?,?,?,?)",[run_id,namespace,extractor_id,_canonical(run_basis),_canonical(result),now]); return result

    def plan_reprocessing(self,name: str,target_extractor_id: str,namespace: str,*,input_ids: Sequence[str] | None=None) -> dict[str,Any]:
        target=self.conn.execute("SELECT 1 FROM knowledge_extractor_definitions WHERE extractor_id=? AND name=?",[target_extractor_id,name]).fetchone()
        if not target: raise ExtractorError("not_found","target extractor version does not exist")
        rows=self.conn.execute("SELECT DISTINCT input_id,extractor_id,input_revision FROM knowledge_extractor_outputs WHERE namespace=? AND extractor_id IN (SELECT extractor_id FROM knowledge_extractor_definitions WHERE name=?) ORDER BY input_id",[namespace,name]).fetchall(); allowed=set(input_ids or []); selected=[{"input_id":r[0],"from_extractor_id":r[1],"input_revision":r[2],"to_extractor_id":target_extractor_id} for r in rows if r[1]!=target_extractor_id and (not allowed or r[0] in allowed)]
        return {"namespace":namespace,"extractor_name":name,"target_extractor_id":target_extractor_id,"inputs":selected,"selective":bool(input_ids),"overwrites_prior_outputs":False,"plan_hash":_digest(selected)}
