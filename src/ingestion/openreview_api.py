"""Bounded public OpenReview API v2 notes; invitation types remain attributable."""

import hashlib
import json
import re
from urllib.parse import urlencode


def is_openreview(source):
    return (
        source["source_id"] == "openreview-notes"
        and source["endpoint"] == "https://api2.openreview.net/notes"
    )


def parameters(request, *, cursor, limit):
    values = dict(request.get("parameters") or {})
    if set(values) - {"id", "forum", "invitation", "replyto", "domain"}:
        raise ValueError("unsupported OpenReview filter")
    if request.get("from_ms") is not None or request.get("to_ms") is not None:
        raise ValueError("OpenReview date windows are not supported")
    if not values or any(
        not isinstance(v, str) or not v.strip() for v in values.values()
    ):
        raise ValueError(
            "an explicit OpenReview note, forum, invitation or domain is required"
        )
    if not 1 <= limit <= 1000:
        raise ValueError("invalid OpenReview page limit")
    # The API's after control avoids shifting offset pages. Resume is a scan,
    # not a provider snapshot; a fresh scan is required to discover later edits.
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 256:
            raise ValueError("invalid OpenReview cursor")
        values["after"] = cursor
    return {**values, "limit": limit, "sort": "id:asc"}


def records(payload, *, cursor, limit):
    if not isinstance(payload, dict) or not isinstance(payload.get("notes"), list):
        raise ValueError("OpenReview response lacks notes")  # noqa: TRY004 - provider schema failure
    notes = payload["notes"]
    if len(notes) > limit:
        raise ValueError("OpenReview exceeded page limit")
    result = []
    seen = set()
    for note in notes:
        if (
            not isinstance(note, dict)
            or not isinstance(note.get("id"), str)
            or not note["id"]
        ):
            raise ValueError("OpenReview note lacks identity")
        if note["id"] in seen or note["id"] == cursor:
            raise ValueError("OpenReview pagination did not advance")
        seen.add(note["id"])
        # Public-only integration: authenticated/private material needs an
        # explicit access mapping before it can enter shared source packs.
        if note.get("readers") != ["everyone"] or note.get("nonreaders"):
            raise ValueError("OpenReview note is not explicitly public")
        content = note.get("content", {})
        if not isinstance(content, dict):
            raise ValueError("invalid OpenReview content")  # noqa: TRY004 - provider schema failure
        visible = {}
        for key, field in content.items():
            if not isinstance(field, dict):
                raise ValueError("API v2 content requires value envelopes")  # noqa: TRY004 - provider schema failure
            if field.get("readers", ["everyone"]) == ["everyone"] and not field.get(
                "nonreaders"
            ):
                visible[key] = field.get("value")
        invitations = note.get("invitations") or []
        if not isinstance(invitations, list) or any(
            not isinstance(i, str) for i in invitations
        ):
            raise ValueError("invalid OpenReview invitations")
        kinds = {re.sub(r"[^a-z0-9]+", "_", i.rsplit("/", 1)[-1].lower()).strip("_") for i in invitations}
        kind = next(
            (
                label
                for names, label in [
                    ({"decision", "meta_review", "acceptance_decision"}, "decision"),
                    ({"rebuttal", "author_response", "author_rebuttal"}, "rebuttal"),
                    ({"official_review", "review", "peer_review"}, "review"),
                    ({"submission", "blind_submission", "paper_revision"}, "submission"),
                ]
                if kinds & names
            ),
            "unknown",
        )
        texts = [
            visible[k]
            for k in (
                "abstract",
                "review",
                "comment",
                "rebuttal",
                "response",
                "decision",
            )
            if isinstance(visible.get(k), str)
        ]
        authors = visible.get("authors") or []
        if not isinstance(authors, list) or any(
            not isinstance(a, str) for a in authors
        ):
            raise ValueError("invalid OpenReview authors")
        title = visible.get("title") or f"OpenReview {kind} {note['id']}"
        public_note = {
            k: note.get(k)
            for k in (
                "id",
                "forum",
                "replyto",
                "invitations",
                "signatures",
                "license",
                "cdate",
                "mdate",
                "tcdate",
                "tmdate",
                "pdate",
                "odate",
                "ddate",
            )
        }
        # Provider-native edit/revision handles are optional. Retain them when
        # present without changing the identity hash of older API responses.
        for key in ("number", "original", "referent"):
            if key in note:
                public_note[key] = note[key]
        public_note["content"] = visible
        revision = hashlib.sha256(
            json.dumps(
                public_note, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        result.append(
            {
                "id": note["id"],
                "url": "https://openreview.net/forum?" + urlencode({"id": note["id"]}),
                "title": title,
                "content": "\n\n".join(texts) or title,
                "authors": authors,
                "published_at": note.get("pdate"),
                "updated_at": note.get("tmdate") or note.get("mdate"),
                "version": revision,
                "note_type": kind,
                "forum": note.get("forum"),
                "replyto": note.get("replyto"),
                "provider_record": public_note,
                "content_representation": "public-note" if texts else "title-only",
                "deleted": bool(note.get("ddate")),
            }
        )
    return result, notes[-1]["id"] if len(notes) == limit else None
