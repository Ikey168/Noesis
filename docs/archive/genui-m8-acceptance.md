# M8 acceptance: persisted canvases, saved, reopened, shared

Milestone M8 (issues #684-#686) takes a canvas from an ephemeral, per-session
layout to a **persisted, shareable** object. This is the acceptance record; its
executable form is `scripts/genui/m8_acceptance.py`, run in CI by
`tests/unit/genui/test_canvas_access.py`.

## The access model

A persisted canvas has one **owner** (the identity that saved it) and, once
shared, a read-only **share token**. `src/genui/canvas_access.py` is the single,
declarative authority for the resulting permissions:

| role | read | write | share | delete |
|---|---|---|---|---|
| **owner** | yes | yes | yes | yes |
| **viewer** (holds a valid share link) | yes | no | no | no |
| **none** (everyone else) | no | no | no | no |

`authorize(canvas, requester, action, via_share_token=?)` is the one enforcement
decision. The store consults it for every owner-scoped read, so ownership checks
are never re-derived ad hoc; the shared-link read path grants the viewer role and
never more.

## What it proves

One warehouse, two identities: `alice` (the owner) and `bob` (a second user).

1. **Save and reopen (M8.1).** Alice saves a canvas; it reopens with its
   `ui-spec` and its live data bindings (which data-mode tool feeds each panel,
   plus the client's snapshot) intact.
2. **Share, stable (M8.2).** Alice mints a read-only share link; minting again
   returns the same token, so the link is stable.
3. **Viewer is read-only (M8.2).** Bob opens the link and sees the canvas marked
   `read_only`, with its bindings, and no owner identity leaked.
4. **Model enforced (M8.3).** Bob cannot reopen the canvas by id, cannot delete
   or share it, and editing it under his own identity produces a *copy* rather
   than mutating Alice's original (owner-keyed update-in-place). The permission
   matrix matches the table above.
5. **Revocable (M8.2).** Revoking the link stops it resolving.

## Result

```
1. alice saved canvas 'hA0n5BqIueUI'
2. reopened with spec + live bindings intact: True
3. alice minted a stable share link: True
4. bob renders the link read-only, no owner leaked: True
5. bob cannot reopen/delete/share; his edit is a copy: True
6. permission matrix matches the model: True
7. revoked link no longer resolves: True

RESULT: OK - canvas saved, reopened, shared read-only, access model enforced
```

## How read-only is enforced, not just labelled

A share token carries no write path: the only mutation endpoints are owner-scoped
(`POST /api/v1/ui/canvas` with the owner's identity). A viewer POSTing with the
owner's canvas id under their own identity is routed to a new canvas of their own
by the M8.1 update-in-place rule, so the original is structurally unreachable for
writes. Read-only is a property of the model, not a flag the client is trusted to
honour.
