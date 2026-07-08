"""
Investigation engine routes - the case-work surface over HTTP.

The engine is a first-class product surface (not gated like the agent host):
an operator opens a case on a question, the engine plans and pursues leads
over the OSINT layer, and the case either concludes through the
evidence-discipline gate or stays open with its gaps named.

    GET  /api/v1/investigation                     list cases
    POST /api/v1/investigation/open               open a case
    POST /api/v1/investigation/run                open + drive a whole case
    POST /api/v1/investigation/{case_id}/advance  one plan/pursue round
    POST /api/v1/investigation/{case_id}/conclude attempt a disciplined verdict
    GET  /api/v1/investigation/{case_id}          the full case file
    GET  /api/v1/investigation/{case_id}/matrix   the hypothesis matrix
    GET  /api/v1/investigation/{case_id}/brief    the cited case brief

Writes run on the API's shared warehouse connection under its lock (the
single-writer discipline all warehouse writes follow).
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.investigation import (
    advance_case,
    case_brief,
    case_file,
    conclude_case,
    hypothesis_matrix,
    list_cases,
    open_case,
    render_markdown,
    run_case,
)

router = APIRouter(prefix="/api/v1/investigation", tags=["investigation"])


def _conn():
    from src.database.local_analytics_connector import _LOCK, get_shared_connection

    return get_shared_connection(), _LOCK


def _or_404(payload: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(payload, dict) and payload.get("error"):
        status = 404 if payload.get("code") == "not_found" else 400
        raise HTTPException(status_code=status, detail=payload["error"])
    return payload


class OpenRequest(BaseModel):
    question: str = Field(..., max_length=500)
    hypotheses: Optional[List[str]] = Field(
        default=None, description="competing hypotheses; a null counterpart is "
        "added when fewer than two are given"
    )
    topic: Optional[str] = Field(default=None, max_length=120)
    entities: Optional[List[str]] = None


class RunRequest(OpenRequest):
    max_rounds: int = Field(default=3, ge=1, le=10)
    conclude: bool = True


@router.get("")
def cases() -> Dict[str, Any]:
    """Every case on the books, open and concluded."""
    conn, lock = _conn()
    with lock:
        rows = list_cases(conn)
    return {"cases": rows, "count": len(rows)}


@router.post("/open")
def open_(request: OpenRequest) -> Dict[str, Any]:
    """Open a case: question in, competing hypotheses on record."""
    conn, lock = _conn()
    with lock:
        return _or_404(
            open_case(
                conn, request.question, hypotheses=request.hypotheses,
                topic=request.topic, entities=request.entities,
            )
        )


@router.post("/run")
def run(request: RunRequest) -> Dict[str, Any]:
    """Open a case and drive it: plan/pursue rounds until the leads run dry,
    then attempt a disciplined conclusion (verdict or named gaps)."""
    conn, lock = _conn()
    with lock:
        return _or_404(
            run_case(
                conn, request.question, hypotheses=request.hypotheses,
                topic=request.topic, entities=request.entities,
                max_rounds=request.max_rounds, conclude=request.conclude,
            )
        )


@router.post("/{case_id}/advance")
def advance(case_id: str) -> Dict[str, Any]:
    """One engine round: plan new leads, pursue everything open, re-score."""
    conn, lock = _conn()
    with lock:
        return _or_404(advance_case(conn, case_id))


@router.post("/{case_id}/conclude")
def conclude(case_id: str) -> Dict[str, Any]:
    """Attempt a verdict through the evidence-discipline gate."""
    conn, lock = _conn()
    with lock:
        return _or_404(conclude_case(conn, case_id))


@router.get("/{case_id}")
def file_(case_id: str) -> Dict[str, Any]:
    """The full case file: record, hypotheses, evidence, leads, journal."""
    conn, lock = _conn()
    with lock:
        return _or_404(case_file(conn, case_id))


@router.get("/{case_id}/matrix")
def matrix(case_id: str) -> Dict[str, Any]:
    """The ACH hypothesis matrix under the honesty envelope."""
    conn, lock = _conn()
    with lock:
        return _or_404(hypothesis_matrix(conn, case_id))


@router.get("/{case_id}/brief")
def brief(case_id: str, format: Optional[str] = None) -> Any:
    """The cited case brief; ``?format=markdown`` renders it for humans."""
    conn, lock = _conn()
    with lock:
        payload = _or_404(case_brief(conn, case_id))
    if format == "markdown":
        return {"case_id": case_id, "markdown": render_markdown(payload)}
    return payload
