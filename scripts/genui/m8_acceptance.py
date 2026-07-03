"""
M8 acceptance: the full persisted-canvas lifecycle, save -> reopen -> share.

One warehouse, two identities (alice the owner, bob a second user). The harness
drives the whole M8 flow through the canvas store and asserts the access model
is enforced end to end:

  * alice saves a canvas; it reopens with its spec and live data bindings intact
    (M8.1);
  * alice mints a read-only share link; bob opens it and sees the canvas
    read-only, with no owner identity leaked (M8.2);
  * the model is enforced (M8.3): bob cannot reopen it by id, cannot delete or
    share it, and editing it under his own identity makes a copy rather than
    mutating alice's; revoking the link stops it resolving.

Run:  python scripts/genui/m8_acceptance.py

The executable form of docs/genui-m8-acceptance.md.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _now():
    return datetime(2026, 7, 3, tzinfo=timezone.utc)


def _spec(topic="climate policy"):
    return {
        "spec_version": "ui-spec-v1",
        "intent": "track the climate policy debate",
        "title": "Climate policy",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": ["claims"],
        "topic": topic,
        "source_type": None,
        "panels": [
            {"id": "p1", "type": "claims", "title": "Key claims", "span": 6,
             "priority": 0.8, "rationale": "", "endpoint": None,
             "params": {"topic": topic}, "body": ""}
        ],
    }


def _bindings():
    return {"p1": {"server": "neuronews-arguments", "tool": "list_claims",
                   "arguments": {"topic": "climate policy"},
                   "snapshot": {"claims": [{"claim_id": "k1"}]}}}


def main() -> dict:
    import duckdb

    from src.genui import canvas_access, canvas_store

    conn = duckdb.connect(":memory:")
    canvas_store.ensure_schema(conn)

    print("M8 acceptance: save -> reopen -> share, access model enforced\n")

    # 1) Alice saves a canvas (M8.1).
    saved = canvas_store.save_canvas(
        conn, owner="alice", spec=_spec(), now=_now(), data_bindings=_bindings()
    )
    cid = saved["id"]
    print(f"1. alice saved canvas {cid!r}")

    # 2) It reopens with spec and live data bindings intact.
    reopened = canvas_store.get_canvas(conn, cid, owner="alice")
    save_reopen_ok = (
        reopened is not None
        and reopened["spec"] == _spec()
        and reopened["data_bindings"] == _bindings()
    )
    print(f"2. reopened with spec + live bindings intact: {save_reopen_ok}")

    # 3) Alice mints a read-only share link (M8.2), stable across calls.
    token = canvas_store.share_canvas(conn, cid, owner="alice")
    stable = token == canvas_store.share_canvas(conn, cid, owner="alice")
    print(f"3. alice minted a stable share link: {bool(token) and stable}")

    # 4) Bob opens the link: read-only, bindings present, no owner leaked (M8.2).
    shared = canvas_store.get_shared_canvas(conn, token)
    viewer_ok = (
        shared is not None
        and shared["read_only"] is True
        and shared["spec"] == _spec()
        and "owner" not in shared
    )
    print(f"4. bob renders the link read-only, no owner leaked: {viewer_ok}")

    # 5) The access model is enforced (M8.3).
    bob_cannot_reopen = canvas_store.get_canvas(conn, cid, owner="bob") is None
    bob_cannot_delete = canvas_store.delete_canvas(conn, cid, owner="bob") is False
    bob_cannot_share = canvas_store.share_canvas(conn, cid, owner="bob") is None
    bob_copy = canvas_store.save_canvas(
        conn, owner="bob", spec=_spec("hijacked"), now=_now(), canvas_id=cid
    )
    original_untouched = canvas_store.get_canvas(conn, cid, owner="alice")["spec"]["topic"] == "climate policy"
    made_a_copy = bob_copy["id"] != cid
    enforced = (
        bob_cannot_reopen and bob_cannot_delete and bob_cannot_share
        and original_untouched and made_a_copy
    )
    print(f"5. bob cannot reopen/delete/share; his edit is a copy: {enforced}")

    # 6) The permission matrix matches the stated model.
    alice_canvas = canvas_store.get_canvas(conn, cid)
    matrix_ok = (
        canvas_access.role_for(alice_canvas, "alice") == canvas_access.ROLE_OWNER
        and canvas_access.role_for(alice_canvas, "bob") == canvas_access.ROLE_NONE
        and canvas_access.role_for(alice_canvas, "bob", via_share_token=True) == canvas_access.ROLE_VIEWER
        and canvas_access.can(canvas_access.ROLE_OWNER, canvas_access.DELETE)
        and canvas_access.can(canvas_access.ROLE_VIEWER, canvas_access.READ)
        and not canvas_access.can(canvas_access.ROLE_VIEWER, canvas_access.WRITE)
        and not canvas_access.can(canvas_access.ROLE_NONE, canvas_access.READ)
    )
    print(f"6. permission matrix matches the model: {matrix_ok}")

    # 7) Revoking the link stops it resolving (M8.2).
    canvas_store.unshare_canvas(conn, cid, owner="alice")
    revoked_ok = canvas_store.get_shared_canvas(conn, token) is None
    print(f"7. revoked link no longer resolves: {revoked_ok}")

    conn.close()
    ok = all([save_reopen_ok, bool(token), stable, viewer_ok, enforced, matrix_ok, revoked_ok])
    print("\nRESULT: " + (
        "OK - canvas saved, reopened, shared read-only, access model enforced"
        if ok else "FAIL"
    ))
    return {
        "canvas_id": cid,
        "save_reopen_ok": save_reopen_ok,
        "share_stable": bool(token) and stable,
        "viewer_read_only_ok": viewer_ok,
        "access_enforced": enforced,
        "matrix_ok": matrix_ok,
        "revoked_ok": revoked_ok,
        "ok": bool(ok),
    }


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["ok"] else 1)
