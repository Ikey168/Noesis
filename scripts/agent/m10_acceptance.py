"""
M10 acceptance: an agent run is fully reconstructable from the audit trail.

Runs the analyst agent (M10.2) end to end with the provisioning audit sink
(M10.4) attached to the runtime, then reconstructs the run from the audit trail
alone and confirms it matches the live transcript call for call:

  * every tool call the agent made is written to the provisioning audit trail;
  * replaying the trail yields the same ordered sequence of (plane, server, tool,
    arguments, ok) - the run is reproducible from the record.

Run:  python scripts/agent/m10_acceptance.py

The executable form of docs/milestones/agent-m10.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

RUN_ID = "analyst-run-delta"
GOAL = "flooding in the coastal delta"


def _seed(conn):
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO news_articles (id, title, url, source, publish_date) VALUES (?,?,?,?,?)",
        [
            ("d1", "Delta flooding", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Delta support", "http://b/1", "Beta Journal", "2026-06-02"),
            ("d3", "Delta support two", "http://c/1", "Gamma Review", "2026-06-03"),
        ],
    )
    conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    conn.execute(
        "INSERT INTO argument_claims VALUES ('k1', 'Severe flooding struck the delta.', 'd1', 'news', 0.9, NULL)"
    )
    conn.execute(
        "CREATE TABLE claim_evidence (evidence_id VARCHAR, claim_id VARCHAR, evidence_text VARCHAR, "
        "evidence_document_id VARCHAR, evidence_source_type VARCHAR, relation VARCHAR, "
        "similarity_score DOUBLE, found_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO claim_evidence (evidence_id, claim_id, evidence_document_id, "
        "evidence_source_type, relation, similarity_score) VALUES (?,?,?,?,?,?)",
        [("e1", "k1", "d2", "news", "supports", 0.88), ("e2", "k1", "d3", "news", "supports", 0.82)],
    )
    conn.execute(
        "CREATE TABLE outlet_scores (source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, stance_neutrality DOUBLE, "
        "composite_score DOUBLE, doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    conn.execute(
        "INSERT INTO outlet_scores (source, source_type, score_date, composite_score) "
        "VALUES ('Alpha Wire', 'outlet', '2026-06-01', 0.62)"
    )


def _key(call):
    return (call.get("step"), call.get("plane"), call.get("server"), call.get("tool"), call.get("ok"))


def main() -> dict:
    import duckdb

    from src.agent.analyst import AnalystAgent
    from src.agent.audit import provisioning_audit_sink, replay_run
    from src.agent.local_backend import build_local_caller
    from src.agent.runtime import AgentRuntime

    print("M10 acceptance: an agent run is reconstructable from the audit trail\n")

    conn = duckdb.connect(":memory:")
    _seed(conn)

    sink = provisioning_audit_sink(conn, RUN_ID)
    runtime = AgentRuntime(build_local_caller(conn), audit_sink=sink)
    result = AnalystAgent(runtime).run(
        GOAL, sources=["Alpha Wire", "Beta Journal", "Gamma Review"],
        claim_id="k1", source="Alpha Wire",
    )
    print(f"1. analyst run: steps={result.steps}, findings={result.findings}, "
          f"kg={result.kg['name']!r} provisioned={result.kg['provisioned']}")

    live = [c.summary() for c in runtime.transcript()]
    replayed = replay_run(conn, RUN_ID)
    conn.close()

    # Every call was recorded, and the replay matches the live run call for call.
    all_recorded = len(replayed) == len(live) and len(live) > 0
    same_sequence = [_key(c) for c in replayed] == [_key(c) for c in live]
    same_arguments = all(r.get("arguments") == l.get("arguments") for r, l in zip(replayed, live))

    print(f"2. audit trail: {len(replayed)} events recorded for {len(live)} calls; "
          f"complete={all_recorded}")
    print(f"3. replay: sequence_matches={same_sequence}, arguments_match={same_arguments}")

    ok = all([all_recorded, same_sequence, same_arguments, result.findings >= 2])
    print("\nRESULT: " + (
        "OK - the run is fully reconstructable from the audit trail"
        if ok else "FAIL"
    ))
    return {
        "steps": result.steps,
        "calls": len(live),
        "events": len(replayed),
        "all_recorded": all_recorded,
        "same_sequence": same_sequence,
        "same_arguments": same_arguments,
        "ok": bool(ok),
    }


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["ok"] else 1)
