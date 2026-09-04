"""Evidence-bearing canonical event resolution with reversible corrections."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

CONTRACT="noesis-canonical-event-v1"

_DDL="""
CREATE TABLE IF NOT EXISTS canonical_events (
  event_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL,
  participants_json TEXT NOT NULL, location_json TEXT, start_ms BIGINT, end_ms BIGINT,
  recurrence_key TEXT, evidence_json TEXT NOT NULL, revision BIGINT NOT NULL,
  status TEXT NOT NULL, canonical_id TEXT, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_event_reports (
  report_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT,
  report_json TEXT NOT NULL, confidence DOUBLE NOT NULL, alternatives_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, linked_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_event_operations (
  operation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, action TEXT NOT NULL,
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, reversed_at_ms BIGINT
);
"""


class EventResolutionError(RuntimeError):
    def __init__(self,code: str,message: str,**details: Any) -> None:
        super().__init__(message); self.code,self.message,self.details=code,message,details


def _canonical(value: Any) -> str:return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False)
def _digest(value: Any) -> str:return hashlib.sha256(_canonical(value).encode()).hexdigest()
def _load(value: Any,default: Any)->Any:return default if value is None else json.loads(value) if isinstance(value,str) else value
def _jaccard(left: Sequence[str],right: Sequence[str])->float:
    a,b=set(left),set(right); return len(a&b)/len(a|b) if a|b else 1.0


class EventResolver:
    def __init__(self,conn: Any,*,initialize: bool=True,auto_link_threshold: float=.82) -> None:
        self.conn,self.threshold=conn,auto_link_threshold
        if initialize: conn.execute(_DDL)

    @staticmethod
    def normalize(value: Mapping[str,Any]) -> dict[str,Any]:
        event_type=str(value.get("event_type", "")); participants=sorted({str(v) for v in value.get("participants",[]) if str(v)}); interval=dict(value.get("time") or {}); evidence=list(value.get("evidence") or [])
        if not event_type or not participants or not evidence: raise EventResolutionError("invalid_event","event type, participants, and source evidence are required")
        start=interval.get("start_ms"); end=interval.get("end_ms",start)
        if start is not None and end is not None and int(end)<int(start): raise EventResolutionError("invalid_interval","event interval ends before it starts")
        return {"event_type":event_type,"participants":participants,"location":value.get("location"),"time":{"start_ms":start,"end_ms":end},"recurrence_key":value.get("recurrence_key"),"evidence":evidence}

    @staticmethod
    def score(left: Mapping[str,Any],right: Mapping[str,Any]) -> tuple[float,dict[str,float]]:
        type_score=float(left["event_type"]==right["event_type"]); participant=_jaccard(left["participants"],right["participants"]); location=float(_canonical(left.get("location"))==_canonical(right.get("location"))) if left.get("location") or right.get("location") else .5
        ls,le=left["time"].get("start_ms"),left["time"].get("end_ms"); rs,re=right["time"].get("start_ms"),right["time"].get("end_ms")
        if None in {ls,le,rs,re}: temporal=.5
        else:
            overlap=max(0,min(int(le),int(re))-max(int(ls),int(rs))+1); span=max(int(le),int(re))-min(int(ls),int(rs))+1; temporal=overlap/span
        recurrence=float(left.get("recurrence_key")==right.get("recurrence_key")) if left.get("recurrence_key") or right.get("recurrence_key") else 1.0
        parts={"type":type_score,"participants":participant,"location":location,"temporal":temporal,"recurrence":recurrence}; score=.3*type_score+.25*participant+.15*location+.2*temporal+.1*recurrence
        if left.get("recurrence_key") and right.get("recurrence_key") and not recurrence: score=min(score,.79)
        return round(score,6),parts

    def _event(self,row: Sequence[Any])->dict[str,Any]:
        return {"contract":CONTRACT,"event_id":row[0],"namespace":row[1],"event_type":row[2],"participants":_load(row[3],[]),"location":_load(row[4],None),"time":{"start_ms":row[5],"end_ms":row[6]},"recurrence_key":row[7],"evidence":_load(row[8],[]),"revision":int(row[9]),"status":row[10],"canonical_id":row[11],"created_at_ms":int(row[12]),"updated_at_ms":int(row[13])}

    def list(self,namespace: str)->list[dict[str,Any]]:return [self._event(row) for row in self.conn.execute("SELECT * FROM canonical_events WHERE namespace=? ORDER BY event_id",[namespace]).fetchall()]

    def resolve_report(self,namespace: str,report: Mapping[str,Any],*,report_id: str,auto_link: bool=True,now_ms: int|None=None)->dict[str,Any]:
        normalized=self.normalize(report); candidates=[]
        prior=self.conn.execute("SELECT report_json,event_id,confidence,alternatives_json FROM canonical_event_reports WHERE report_id=?",[report_id]).fetchone()
        if prior:
            if prior[0]!=_canonical(normalized):raise EventResolutionError("report_conflict","report id was reused with different event content")
            return {"report_id":report_id,"event_id":prior[1],"confidence":float(prior[2]),"linked":True,"alternatives":_load(prior[3],[]),"forced_merge":False,"idempotent":True}
        for event in self.list(namespace):
            if event["status"]!="active":continue
            candidate={key:event[key] for key in ("event_type","participants","location","time","recurrence_key","evidence")}; score,parts=self.score(normalized,candidate); candidates.append({"event_id":event["event_id"],"confidence":score,"factors":parts})
        candidates.sort(key=lambda item:(-item["confidence"],item["event_id"])); chosen=candidates[0] if candidates and candidates[0]["confidence"]>=self.threshold and auto_link else None; now=now_ms or int(time.time()*1000)
        if chosen: event_id=chosen["event_id"]
        else:
            event_id="event:"+_digest({"namespace":namespace,**normalized})[:24]
            self.conn.execute("INSERT OR IGNORE INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,1,'active',NULL,?,?)",[event_id,namespace,normalized["event_type"],_canonical(normalized["participants"]),_canonical(normalized["location"]),normalized["time"]["start_ms"],normalized["time"]["end_ms"],normalized["recurrence_key"],_canonical(normalized["evidence"]),now,now])
        confidence=chosen["confidence"] if chosen else 1.0; alternatives=[item for item in candidates if item["event_id"]!=event_id][:5]
        self.conn.execute("INSERT OR REPLACE INTO canonical_event_reports VALUES (?,?,?,?,?,?,?,?)",[report_id,namespace,event_id,_canonical(normalized),confidence,_canonical(alternatives),_canonical(normalized["evidence"]),now]); return {"report_id":report_id,"event_id":event_id,"confidence":confidence,"linked":bool(chosen),"alternatives":alternatives,"forced_merge":False}

    def revise(self,event_id: str,patch: Mapping[str,Any],*,reason: str,now_ms: int|None=None)->dict[str,Any]:
        row=self.conn.execute("SELECT * FROM canonical_events WHERE event_id=?",[event_id]).fetchone()
        if not row:raise EventResolutionError("not_found","event does not exist")
        before=self._event(row); candidate={key:before[key] for key in ("event_type","participants","location","time","recurrence_key","evidence")}; candidate.update(patch); normalized=self.normalize(candidate); now=now_ms or int(time.time()*1000)
        self.conn.execute("UPDATE canonical_events SET event_type=?,participants_json=?,location_json=?,start_ms=?,end_ms=?,recurrence_key=?,evidence_json=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",[normalized["event_type"],_canonical(normalized["participants"]),_canonical(normalized["location"]),normalized["time"]["start_ms"],normalized["time"]["end_ms"],normalized["recurrence_key"],_canonical(normalized["evidence"]),now,event_id]); after=self._event(self.conn.execute("SELECT * FROM canonical_events WHERE event_id=?",[event_id]).fetchone()); return {"before":before,"after":after,"reason":reason}

    def merge(self,event_ids: Sequence[str],*,reason: str,now_ms: int|None=None)->dict[str,Any]:
        ids=sorted(set(event_ids))
        if len(ids)<2:raise EventResolutionError("insufficient_events","merge requires at least two events")
        rows=[self.conn.execute("SELECT * FROM canonical_events WHERE event_id=?",[event_id]).fetchone() for event_id in ids]
        if any(row is None for row in rows):raise EventResolutionError("not_found","a merge event does not exist")
        before=[self._event(row) for row in rows]; namespace=before[0]["namespace"]
        if any(event["namespace"]!=namespace for event in before):raise EventResolutionError("namespace_mismatch","events must share a namespace")
        recurrence={event["recurrence_key"] for event in before if event["recurrence_key"]}
        if len(recurrence)>1:raise EventResolutionError("recurrence_conflict","different recurring occurrences cannot be merged")
        combined={"event_type":before[0]["event_type"],"participants":sorted({v for event in before for v in event["participants"]}),"location":before[0]["location"],"time":{"start_ms":min(v for v in [e["time"]["start_ms"] for e in before] if v is not None) if any(e["time"]["start_ms"] is not None for e in before) else None,"end_ms":max(v for v in [e["time"]["end_ms"] for e in before] if v is not None) if any(e["time"]["end_ms"] is not None for e in before) else None},"recurrence_key":next(iter(recurrence),None),"evidence":[v for event in before for v in event["evidence"]]}; now=now_ms or int(time.time()*1000); canonical_id="event:"+_digest({"namespace":namespace,**combined})[:24]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT OR IGNORE INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,1,'active',NULL,?,?)",[canonical_id,namespace,combined["event_type"],_canonical(combined["participants"]),_canonical(combined["location"]),combined["time"]["start_ms"],combined["time"]["end_ms"],combined["recurrence_key"],_canonical(combined["evidence"]),now,now])
            for event_id in ids:self.conn.execute("UPDATE canonical_events SET status='merged',canonical_id=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",[canonical_id,now,event_id])
            reports={event_id:[row[0] for row in self.conn.execute("SELECT report_id FROM canonical_event_reports WHERE event_id=? ORDER BY report_id",[event_id]).fetchall()] for event_id in ids}
            self.conn.execute("UPDATE canonical_event_reports SET event_id=? WHERE event_id IN (SELECT unnest(?))",[canonical_id,ids]); after=self._event(self.conn.execute("SELECT * FROM canonical_events WHERE event_id=?",[canonical_id]).fetchone()); operation_id="event-operation:"+_digest(["merge",ids,canonical_id,now])[:24]; self.conn.execute("INSERT INTO canonical_event_operations VALUES (?,?,'merge',?,?,'committed',?,NULL)",[operation_id,namespace,_canonical({"events":before,"reports":reports}),_canonical(after),now]); self.conn.execute("COMMIT")
        except Exception:self.conn.execute("ROLLBACK");raise
        return {"operation_id":operation_id,"canonical_event":after,"merged_event_ids":ids,"reversible":True,"reason":reason}

    def reverse(self,operation_id: str,*,reason: str,now_ms: int|None=None)->dict[str,Any]:
        row=self.conn.execute("SELECT namespace,action,before_json,after_json,status FROM canonical_event_operations WHERE operation_id=?",[operation_id]).fetchone()
        if not row:raise EventResolutionError("not_found","event operation does not exist")
        if row[4]!="committed":raise EventResolutionError("already_reversed","event operation was already reversed")
        before_payload=_load(row[2],{});before=before_payload.get("events",before_payload if isinstance(before_payload,list) else []);reports=before_payload.get("reports",{}) if isinstance(before_payload,dict) else {};after=_load(row[3],{});now=now_ms or int(time.time()*1000)
        self.conn.execute("BEGIN")
        try:
            for event in before:self.conn.execute("UPDATE canonical_events SET status=?,canonical_id=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",[event["status"],event["canonical_id"],now,event["event_id"]])
            for event_id,report_ids in reports.items():
                if report_ids:self.conn.execute("UPDATE canonical_event_reports SET event_id=? WHERE report_id IN (SELECT unnest(?))",[event_id,report_ids])
            self.conn.execute("UPDATE canonical_events SET status='split',revision=revision+1,updated_at_ms=? WHERE event_id=?",[now,after["event_id"]]);self.conn.execute("UPDATE canonical_event_operations SET status='reversed',reversed_at_ms=? WHERE operation_id=?",[now,operation_id]);self.conn.execute("COMMIT")
        except Exception:self.conn.execute("ROLLBACK");raise
        return {"operation_id":operation_id,"status":"reversed","restored_event_ids":[event["event_id"] for event in before],"reason":reason}
