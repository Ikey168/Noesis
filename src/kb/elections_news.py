"""News framing and sentiment next to contests as dated evidence, never as a cause (#1908, L09).

An article from the ingested news documents (``news.articles``) is linked to a
contest only through

* an **explicit mention**: an extracted actor mention (``document_actors``)
  whose name is the label a result source published for a party or candidate
  in the contest, or whose entity id is one the election record is joined to
  by an accepted identity decision (L06); or
* a **reviewed assertion**: a user's link with a reason.

Shared words without a mention (the label appears in the title or text only)
yield a ``keyword-candidate`` that is shown as such and never counted as a
link. Links are revision-addressable and reversible, and each cites the
article's source revision (document id, content hash, URL, outlet, date).

Answers show existing ``document_frames`` rows and sentiment labels with the
article date, outlet and frame label *next to* the contest's result vintages
and poll readings for the same period; nothing is merged, correlated or read
as having moved a result or a poll.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.kb.elections import (
    READ_SCOPE,
    WRITE_SCOPE,
    ElectionError,
    ElectionStore,
    _day,
    _load,
    authorize,
    canonical,
    candidate_record_key,
    digest,
    normalize_name,
    party_record_key,
    require_scope,
    table_exists,
)

NEWS_READ = "knowledge:read"
LINK_STATES = ("linked", "candidate", "reverted")
SEPARATION = (
    "News framing and sentiment are shown beside results and polls for the same period as dated evidence only; "
    "no correlation or causal effect on any result or poll is stated or computed."
)
_DDL = """
CREATE TABLE IF NOT EXISTS election_news_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, revision_no INTEGER NOT NULL, contest_id TEXT NOT NULL,
  target_key TEXT NOT NULL, document_id TEXT NOT NULL, basis TEXT NOT NULL, state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, article_json TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id, revision_no)
);
"""


def _iso(ms: Any) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()


class ElectionNews:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ElectionStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ articles

    def article(self, document_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "documents"):
            return None
        row = self.conn.execute(
            "SELECT document_id, source_type, source_id, url, content_hash, title, created_at, ingested_at, metadata "
            "FROM documents WHERE document_id=?",
            [document_id],
        ).fetchone()
        if row is None:
            return None
        metadata = _load(row[8], {}) if isinstance(row[8], str) else dict(row[8] or {})
        return {
            "document_id": row[0],
            "source_type": row[1],
            "outlet": metadata.get("outlet") or row[2],
            "url": row[3],
            "title": row[5],
            "date": _iso(row[6] or row[7]),
            "source_revision": {
                "document_id": row[0],
                "content_hash": row[4],
                "url": row[3],
                "ingested_at_ms": row[7],
            },
        }

    def _articles(self, date_from: str, date_to: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "documents"):
            return []
        start = int(
            datetime.combine(
                date.fromisoformat(date_from), datetime.min.time(), timezone.utc
            ).timestamp()
            * 1000
        )
        end = int(
            datetime.combine(
                date.fromisoformat(date_to) + timedelta(days=1),
                datetime.min.time(),
                timezone.utc,
            ).timestamp()
            * 1000
        )
        rows = self.conn.execute(
            "SELECT document_id FROM documents WHERE coalesce(created_at, ingested_at) >= ? AND "
            "coalesce(created_at, ingested_at) < ? AND source_type IN ('news', 'web', 'blog') ORDER BY document_id",
            [start, end],
        ).fetchall()
        return [self.article(r[0]) for r in rows]

    def _window(
        self, contest: Mapping[str, Any], date_from: str | None, date_to: str | None
    ) -> tuple[str, str]:
        election = self.store.election(contest["namespace"], contest["election_id"])
        anchor = election["election_date"]
        if (date_from is None or date_to is None) and anchor is None:
            raise ElectionError(
                "invalid_window",
                "the election states no date; give date_from and date_to",
            )
        start = (
            _day(date_from)
            if date_from
            else (date.fromisoformat(anchor) - timedelta(days=60)).isoformat()
        )
        end = (
            _day(date_to)
            if date_to
            else (date.fromisoformat(anchor) + timedelta(days=30)).isoformat()
        )
        if end < start:
            raise ElectionError("invalid_window", "date_to precedes date_from")
        return start, end

    def _targets(
        self, namespace: str, contest: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Party and candidate labels a result source published for the contest, with accepted identity entities."""
        from src.kb.elections_identity import ElectionIdentity

        targets: dict[str, dict[str, Any]] = {}
        unit = {
            "scheme": contest["unit_scheme"],
            "native_id": contest["unit_native_id"],
        }
        for vintage in self.store.history(namespace, contest["contest_id"]):
            for entry in vintage["figures"].get("entries") or []:
                labels = [
                    (
                        entry["name"],
                        candidate_record_key(contest["election_id"], entry, unit),
                    )
                ]
                if entry.get("party"):
                    labels.append(
                        (
                            entry["party"],
                            party_record_key(contest["election_id"], entry["party"]),
                        )
                    )
                for label, key in labels:
                    targets.setdefault(key, {"label": label, "entity_ids": set()})
        if table_exists(self.conn, "ownership_identity_candidates"):
            identity = ElectionIdentity(self.conn, initialize=False)
            for key, target in targets.items():
                for other in identity.accepted_external(namespace, key):
                    target["entity_ids"].add(other.split(":", 1)[1])
        return targets

    # ------------------------------------------------------------------ links

    def refresh_links(
        self,
        namespace: str,
        contest_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        """Link articles in the period by explicit mentions; label-only overlaps become keyword candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, NEWS_READ)
        contest = self.store.contest(namespace, contest_id)
        start, end = self._window(contest, date_from, date_to)
        targets = self._targets(namespace, contest)
        actors = table_exists(self.conn, "document_actors")
        made = {"linked": 0, "candidate": 0, "unchanged": 0}
        for article in self._articles(start, end):
            mentions = []
            if actors:
                mentions = self.conn.execute(
                    "SELECT actor_name, entity_id, role FROM document_actors WHERE document_id=? ORDER BY actor_name",
                    [article["document_id"]],
                ).fetchall()
            text = normalize_name(
                " ".join(
                    filter(
                        None, [article["title"], self._content(article["document_id"])]
                    )
                )
            )
            for key, target in sorted(targets.items()):
                label = normalize_name(target["label"])
                explicit = [
                    {"actor_name": m[0], "entity_id": m[1], "role": m[2]}
                    for m in mentions
                    if normalize_name(m[0]) == label
                    or (m[1] and m[1] in target["entity_ids"])
                ]
                if explicit:
                    basis, state, evidence = (
                        "explicit-mention",
                        "linked",
                        {"mentions": explicit},
                    )
                elif label and f" {label} " in f" {text} ":
                    basis, state, evidence = (
                        "keyword-candidate",
                        "candidate",
                        {
                            "matched_text": target["label"],
                            "note": "the label appears in the text but no entity mention was extracted; not a link",
                        },
                    )
                else:
                    continue
                outcome = self._append(
                    namespace,
                    contest_id,
                    key,
                    article,
                    basis,
                    state,
                    evidence,
                    principal_id,
                    keep_reviewed=True,
                )
                made[outcome] += 1
        return {
            "contest_id": contest_id,
            "window": {"from": start, "to": end},
            **made,
            "links": self.links(namespace, contest_id, scopes=scopes),
        }

    def _content(self, document_id: str) -> str:
        row = self.conn.execute(
            "SELECT content FROM documents WHERE document_id=?", [document_id]
        ).fetchone()
        return str(row[0] or "") if row else ""

    def assert_link(
        self,
        namespace: str,
        contest_id: str,
        document_id: str,
        *,
        target_key: str,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer's assertion that an article concerns a contest's party or candidate (with a reason)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, NEWS_READ)
        contest = self.store.contest(namespace, contest_id)
        if not str(reason or "").strip():
            raise ElectionError(
                "invalid_link", "a reviewed assertion states its reason"
            )
        if target_key != contest_id and target_key not in self._targets(
            namespace, contest
        ):
            raise ElectionError(
                "invalid_link",
                "the target is the contest or one of its published parties",
            )
        article = self.article(document_id)
        if article is None:
            raise ElectionError("not_found", "article is not an ingested document")
        self._append(
            namespace,
            contest_id,
            target_key,
            article,
            "reviewed-assertion",
            "linked",
            {"reason": reason.strip(), "reviewer": principal_id},
            principal_id,
        )
        return self._current(
            namespace, self._link_id(namespace, contest_id, target_key, document_id)
        )

    def revert(
        self,
        namespace: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Revert a link or candidate (a new revision); answers no longer show it as linked."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        current = self._current(namespace, link_id)
        if current is None or current["state"] == "reverted":
            raise ElectionError(
                "invalid_state", "only a current link or candidate can be reverted"
            )
        if not str(reason or "").strip():
            raise ElectionError("invalid_link", "a revert states its reason")
        self._insert(
            namespace,
            link_id,
            current["revision_no"] + 1,
            current["contest_id"],
            current["target_key"],
            current["article"],
            current["basis"],
            "reverted",
            {**current["evidence"], "reverted_because": reason.strip()},
            principal_id,
        )
        return self._current(namespace, link_id)

    @staticmethod
    def _link_id(namespace, contest_id, target_key, document_id) -> str:
        return (
            "election-news-link:"
            + digest([namespace, contest_id, target_key, document_id])[:24]
        )

    def _append(
        self,
        namespace,
        contest_id,
        target_key,
        article,
        basis,
        state,
        evidence,
        principal_id,
        *,
        keep_reviewed: bool = False,
    ) -> str:
        link_id = self._link_id(
            namespace, contest_id, target_key, article["document_id"]
        )
        current = self._current(namespace, link_id)
        content = {
            "basis": basis,
            "state": state,
            "evidence": evidence,
            "article": article["source_revision"],
        }
        if current is not None:
            if keep_reviewed and (
                current["basis"] == "reviewed-assertion"
                or current["state"] == "reverted"
            ):
                return "unchanged"  # an automatic refresh never overrides a reviewer
            same = {
                "basis": current["basis"],
                "state": current["state"],
                "evidence": current["evidence"],
                "article": current["article"]["source_revision"],
            }
            if canonical(same) == canonical(content):
                return "unchanged"
        number = 1 if current is None else current["revision_no"] + 1
        self._insert(
            namespace,
            link_id,
            number,
            contest_id,
            target_key,
            article,
            basis,
            state,
            evidence,
            principal_id,
        )
        return state

    def _insert(
        self,
        namespace,
        link_id,
        number,
        contest_id,
        target_key,
        article,
        basis,
        state,
        evidence,
        principal_id,
    ):
        self.conn.execute(
            "INSERT INTO election_news_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                number,
                contest_id,
                target_key,
                article["document_id"],
                basis,
                state,
                canonical(evidence),
                canonical(article),
                principal_id,
                self.now(),
            ],
        )

    def _current(self, namespace: str, link_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "election_news_links"):
            return None
        row = self.conn.execute(
            "SELECT link_id, revision_no, contest_id, target_key, document_id, basis, state, evidence_json, "
            "article_json, recorded_by, recorded_at_ms FROM election_news_links WHERE namespace=? AND link_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            return None
        view = dict(
            zip(
                (
                    "link_id",
                    "revision_no",
                    "contest_id",
                    "target_key",
                    "document_id",
                    "basis",
                    "state",
                    "evidence",
                    "article",
                    "recorded_by",
                    "recorded_at_ms",
                ),
                row,
            )
        )
        view["evidence"], view["article"] = (
            _load(view["evidence"], {}),
            _load(view["article"], {}),
        )
        view["link_kind"] = {
            "linked": view["basis"],
            "candidate": "keyword-candidate",
            "reverted": "reverted",
        }[view["state"]]
        return view

    def links(
        self,
        namespace: str,
        contest_id: str,
        *,
        scopes: Iterable[str],
        states: Iterable[str] = LINK_STATES,
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "election_news_links"):
            return []
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT link_id FROM election_news_links WHERE namespace=? AND contest_id=? ORDER BY link_id",
                [namespace, contest_id],
            ).fetchall()
        ]
        wanted = set(states)
        return [
            v
            for v in (self._current(namespace, i) for i in ids)
            if v and v["state"] in wanted
        ]

    # ------------------------------------------------------------------ evidence

    def _frames(self, document_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "document_frames"):
            return []
        return [
            {"frame": r[0], "score": r[1], "classified_at": r[2]}
            for r in self.conn.execute(
                "SELECT frame, score, classified_at FROM document_frames WHERE document_id=? ORDER BY frame",
                [document_id],
            ).fetchall()
        ]

    def _sentiment(self, article: Mapping[str, Any]) -> dict[str, Any] | None:
        if not table_exists(self.conn, "news_articles"):
            return None
        row = self.conn.execute(
            "SELECT sentiment_label, sentiment_score FROM news_articles WHERE id=? OR url=? LIMIT 1",
            [article["document_id"], article["url"]],
        ).fetchone()
        return (
            None
            if row is None or row[0] is None
            else {"label": row[0], "score": row[1]}
        )

    def evidence(
        self,
        namespace: str,
        contest_id: str,
        *,
        scopes: Iterable[str],
        date_from: str | None = None,
        date_to: str | None = None,
        include_candidates: bool = True,
    ) -> dict[str, Any]:
        """Results, poll readings and linked news for one period, in separate sections; nothing is combined."""
        from src.kb.elections_polls import ElectionPolls

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, NEWS_READ)
        contest = self.store.contest(namespace, contest_id)
        start, end = self._window(contest, date_from, date_to)
        states = ("linked", "candidate") if include_candidates else ("linked",)
        news = []
        for link in self.links(namespace, contest_id, scopes=scopes, states=states):
            article = self.article(link["document_id"]) or {
                "document_id": link["document_id"]
            }
            if not article.get("date") or not start <= article["date"] <= end:
                continue
            news.append(
                {
                    "article": article,
                    "link": {
                        k: link[k]
                        for k in (
                            "link_id",
                            "revision_no",
                            "basis",
                            "state",
                            "link_kind",
                            "target_key",
                            "evidence",
                        )
                    },
                    "cited_revision": link["article"]["source_revision"],
                    "frames": self._frames(link["document_id"]),
                    "sentiment": self._sentiment(article),
                }
            )
        news.sort(
            key=lambda n: (
                n["article"].get("date") or "",
                n["article"]["document_id"],
                n["link"]["target_key"],
            )
        )
        results = [
            {k: v[k] for k in ("vintage_id", "kind", "published_on", "source_revision")}
            for v in self.store.history(namespace, contest_id)
            if start <= v["published_on"] <= end
        ]
        polls = ElectionPolls(self.conn, initialize=False).readings(
            namespace,
            scopes=scopes,
            election_id=contest["election_id"],
            date_from=start,
            date_to=end,
        )
        return {
            "contest_id": contest_id,
            "period": {"from": start, "to": end},
            "result_vintages": results,
            "poll_readings": [
                {
                    k: r[k]
                    for k in (
                        "reading_id",
                        "publisher",
                        "option",
                        "value",
                        "fieldwork_start",
                        "fieldwork_end",
                        "sample_size",
                        "typed_as",
                        "source_revision",
                    )
                }
                for r in polls
            ],
            "news_evidence": news,
            "separation": SEPARATION,
        }


__all__ = ["ElectionNews", "SEPARATION"]
