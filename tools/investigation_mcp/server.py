"""
Noesis investigation engine - MCP server.

The case-work surface: open a case on a question, plan and pursue leads over
the OSINT composition layer, weigh the hypothesis matrix, and conclude only
through the evidence-discipline gate. Everything a case does is journalled,
so any case replays step by step.

Tool surface:
  case_open(question, hypotheses?, topic?, entities?)   open a case
  case_run(question, ..., max_rounds?)                  open + drive a whole case
  case_advance(case_id)                                 one plan/pursue round
  case_conclude(case_id)                                attempt a verdict (gated)
  case_list()                                           every case on the books
  case_file(case_id)                                    the full durable case file
  hypothesis_matrix(case_id)                            ACH scoring (honesty-wrapped)
  case_brief(case_id)                                   the cited brief

Write authority: the write tools open the DuckDB warehouse read-write and
hold a process-level lock for the operation, mirroring the provisioning
server; in the deployed system the write path is the API process that owns
the single warehouse writer. Read tools open the warehouse read-only.

Design constraints (as for every tool server): stdlib + fastmcp at import
time, lazy imports inside tools, discipline enforced in
:mod:`src.investigation`.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import List, Optional

from fastmcp import FastMCP

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.analytics.honesty import INTERVAL_SCHEMA, honesty_output_schema  # noqa: E402

mcp = FastMCP("noesis-investigation")

# Serialises every case write in this process (single-writer warehouse).
_WRITE_LOCK = threading.Lock()


def _db_path() -> str:
    from src.config.env import warehouse_path
    return warehouse_path(str(REPO_ROOT / "data" / "neuronews.duckdb"))


def _warehouse_ro():
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"warehouse not found at {path}")
    return duckdb.connect(path, read_only=True)


def _warehouse_rw():
    import duckdb

    path = _db_path()
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"warehouse not found at {path} - start the API once to seed it, "
            f"or set NOESIS_DB_PATH"
        )
    return duckdb.connect(path, read_only=False)


# --------------------------------------------------------------------------- #
# Write tools (hold the write lock for the operation)
# --------------------------------------------------------------------------- #

@mcp.tool
def case_open(
    question: str,
    hypotheses: Optional[List[str]] = None,
    topic: Optional[str] = None,
    entities: Optional[List[str]] = None,
) -> dict:
    """Open an investigation on a question with competing hypotheses. When no
    hypotheses are given the engine seeds the affirmative reading of the
    question and its null counterpart, so every case can state what would
    disconfirm it.

    Args:
        question: the question under investigation.
        hypotheses: optional competing hypothesis statements.
        topic: optional topic scope (claim-text substring).
        entities: optional entities in scope (dossiers and connection paths).
    """
    with _WRITE_LOCK:
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.investigation import open_case

            return open_case(
                con, question, hypotheses=hypotheses, topic=topic,
                entities=entities,
            )
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()


@mcp.tool
def case_run(
    question: str,
    hypotheses: Optional[List[str]] = None,
    topic: Optional[str] = None,
    entities: Optional[List[str]] = None,
    max_rounds: int = 3,
) -> dict:
    """Open a case and drive it end to end: plan and pursue lead rounds until
    the leads run dry (or the round budget is spent), then attempt a
    conclusion through the evidence-discipline gate. Returns the case file
    with the final hypothesis matrix and either a verdict or the named gaps.

    Args:
        question: the question under investigation.
        hypotheses: optional competing hypothesis statements.
        topic: optional topic scope.
        entities: optional entities in scope.
        max_rounds: plan/pursue round budget (default 3).
    """
    with _WRITE_LOCK:
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.investigation import run_case

            return run_case(
                con, question, hypotheses=hypotheses, topic=topic,
                entities=entities, max_rounds=max_rounds,
            )
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()


@mcp.tool
def case_advance(case_id: str) -> dict:
    """One engine round on an open case: plan any genuinely new leads
    (planning is idempotent), pursue everything open, and re-score the
    hypothesis matrix.

    Args:
        case_id: the case to advance (see case_list).
    """
    with _WRITE_LOCK:
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.investigation import advance_case

            return advance_case(con, case_id)
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()


@mcp.tool
def case_conclude(case_id: str) -> dict:
    """Attempt a verdict through the evidence-discipline gate: enough
    independent sources, a real margin over the runner-up, no unanswered
    contradiction, no open leads. On failure the case stays open and every
    gap is returned by name - the engine never forces a call.

    Args:
        case_id: the case to conclude (see case_list).
    """
    with _WRITE_LOCK:
        try:
            con = _warehouse_rw()
        except Exception as exc:
            return {"error": str(exc)}
        try:
            from src.investigation import conclude_case

            return conclude_case(con, case_id)
        except Exception as exc:
            return {"error": str(exc)}
        finally:
            con.close()


# --------------------------------------------------------------------------- #
# Read tools
# --------------------------------------------------------------------------- #

@mcp.tool
def case_list() -> dict:
    """Every investigation on the books, open and concluded, with status and
    verdict."""
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.investigation import list_cases

        rows = list_cases(con)
        return {"cases": rows, "count": len(rows)}
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def case_file(case_id: str) -> dict:
    """The full durable case file: the record, hypotheses, cited evidence
    rows, leads (open and pursued), and the journal - every step the case
    took, oldest first, so it replays as a narrative.

    Args:
        case_id: the case to read (see case_list).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.investigation import case_file as _file

        return _file(con, case_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool(
    output_schema=honesty_output_schema(
        {
            "case_id": {"type": "string"},
            "question": {"type": "string"},
            "hypotheses": {"type": "array"},
            "leader": {"type": ["string", "null"]},
            "margin": {"type": ["number", "null"]},
            "support_credibility": {**INTERVAL_SCHEMA, "type": ["object", "null"]},
            "open_leads": {"type": "integer"},
            "contradictions_in_record": {"type": "integer"},
            "uncited_evidence_count": {"type": "integer"},
            "evidence_count": {"type": "integer"},
        }
    ),
)
def hypothesis_matrix(case_id: str) -> dict:
    """ACH-style scoring of a case's hypotheses: per hypothesis, the
    independent supporting and contradicting sources, weighted tallies, and
    diagnostic sources; the leader carries a calibrated support interval,
    never a bare score.

    Args:
        case_id: the case to score (see case_list).
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.investigation import hypothesis_matrix as _matrix

        return _matrix(con, case_id)
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


@mcp.tool
def case_brief(case_id: str, markdown: bool = False) -> dict:
    """The cited case brief: question, verdict (or the gaps keeping the case
    open), the hypothesis ranking, the key evidence behind the leader, where
    the record disagrees with itself, and what the engine did. Uncited lines
    are flagged, never hidden.

    Args:
        case_id: the case to brief (see case_list).
        markdown: also render the brief as markdown for humans.
    """
    try:
        con = _warehouse_ro()
    except Exception as exc:
        return {"error": str(exc)}
    try:
        from src.investigation import case_brief as _brief
        from src.investigation import render_markdown

        brief = _brief(con, case_id)
        if markdown and not brief.get("error"):
            brief["markdown"] = render_markdown(brief)
        return brief
    except Exception as exc:
        return {"error": str(exc)}
    finally:
        con.close()


if __name__ == "__main__":
    mcp.run()
