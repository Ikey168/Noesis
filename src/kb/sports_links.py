"""Sports records linked to News, Geospatial and entity records by citation (#2144, SP09).

* **News**: an ingested article (``news.articles``) becomes a *candidate* for
  a match only when an extracted actor mention (``document_actors``) names
  both teams (as their sources publish them, or an entity joined to them by an
  accepted identity decision), the competition is named in the article, and
  the article is dated within the match's window; for a transfer, the player
  and the new club are mentioned within the window. Shared words without
  mentions are never a candidate. A reviewer accepts (``linked``) or rejects a
  candidate; every link keeps its evidence and can be reverted.
* **Venues** link to Geospatial places through
  :meth:`src.kb.sports_identity.SportsIdentity.link_venue_place`; the pack
  stores the place reference only.
* **Transfers** are recorded only from an openly published announcement
  (governing body, league or club); a transfer reported only by news stays a
  cited news claim and is never a transfer record. A fee is kept only when
  the announcement states it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.ingestion.sports_sources import compact, iso_datetime, record_key, to_ms
from src.kb.sports_identity import normalize_name
from src.kb.sports_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    SportsError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.sports_store import SportsStore

NEWS_READ = "knowledge:read"
DAY_MS = 86_400_000
TRANSFER_SOURCE_KINDS = ("governing-body", "league", "club")
_DDL = """
CREATE TABLE IF NOT EXISTS sports_news_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, revision_no INTEGER NOT NULL, target_type TEXT NOT NULL,
  target_key TEXT NOT NULL, document_id TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  reason TEXT, recorded_by TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id, revision_no)
);
"""


def _date(ms: Any) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).date().isoformat()


class SportsLinks:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SportsStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ news

    def _names(
        self, namespace: str, team_key: str, identity
    ) -> tuple[set[str], set[str]]:
        keys = identity.linked(namespace, team_key) if identity else [team_key]
        names, entities = set(), set()
        for key in keys:
            for revision in self.store.history(namespace, "team", key):
                names.add(normalize_name(revision["body"].get("name"), club=True))
        if identity and table_exists(self.conn, "ownership_identity_candidates"):
            rows = self.conn.execute(
                "SELECT left_key, right_key, left_entity, right_entity FROM ownership_identity_candidates WHERE "
                "namespace=? AND state='accepted' AND (list_contains(?, left_key) OR list_contains(?, right_key))",
                [namespace, keys, keys],
            ).fetchall()
            for left, right, left_entity, right_entity in rows:
                if left.startswith("canonical:"):
                    entities.add(left_entity)
                if right.startswith("canonical:"):
                    entities.add(right_entity)
        return {n for n in names if n}, entities

    def _articles(self, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "documents"):
            return []
        rows = self.conn.execute(
            "SELECT document_id, url, content_hash, title, content, coalesce(created_at, ingested_at), source_id, "
            "metadata FROM documents WHERE coalesce(created_at, ingested_at) BETWEEN ? AND ? AND source_type IN "
            "('news', 'web', 'blog') ORDER BY document_id",
            [start_ms, end_ms],
        ).fetchall()
        out = []
        for (
            document_id,
            url,
            content_hash,
            title,
            content,
            created,
            source_id,
            metadata,
        ) in rows:
            meta = (
                json.loads(metadata)
                if isinstance(metadata, str) and metadata
                else dict(metadata or {})
            )
            actors = []
            if table_exists(self.conn, "document_actors"):
                actors = self.conn.execute(
                    "SELECT actor_name, entity_id FROM document_actors WHERE document_id=? ORDER BY actor_name",
                    [document_id],
                ).fetchall()
            out.append(
                {
                    "document_id": document_id,
                    "url": url,
                    "title": title or "",
                    "content": content or "",
                    "date": _date(created),
                    "outlet": meta.get("outlet") or source_id,
                    "source_revision": {
                        "document_id": document_id,
                        "content_hash": content_hash,
                        "url": url,
                    },
                    "actors": [(normalize_name(a, club=True), e) for a, e in actors],
                }
            )
        return out

    def refresh_news_links(
        self,
        namespace: str,
        *,
        target_type: str,
        target_key: str,
        principal_id: str,
        scopes: Iterable[str],
        identity=None,
        window_days: int = 2,
    ) -> dict[str, Any]:
        """Offer reviewable candidates for a match or a transfer; mentions, date window and competition required."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, NEWS_READ)
        self.store.require_ready(namespace)
        if target_type == "fixture":
            fixture = self.store.body(namespace, "fixture", target_key)
            schedule = self.store.body(
                namespace, "fixture_schedule_revision", target_key
            )
            if (
                fixture is None
                or schedule is None
                or set(dict(fixture.get("sides") or {})) != {"home", "away"}
            ):
                raise SportsError(
                    "not_found", "a home/away fixture with a schedule is required"
                )
            anchor = to_ms(schedule["kickoff"][:10])
            required = [
                self._names(namespace, fixture["sides"][s], identity)
                for s in ("home", "away")
            ]
            season = self.store.body(namespace, "season", fixture["season_key"]) or {}
            competition = (
                self.store.body(
                    namespace, "competition", season.get("competition_key") or ""
                )
                or {}
            )
            context = normalize_name(competition.get("name"))
        elif target_type == "transfer":
            transfer = self.store.body(namespace, "transfer_assertion", target_key)
            if transfer is None:
                raise SportsError(
                    "not_found", "transfer is not visible in this namespace"
                )
            anchor = to_ms(transfer["date"][:10])
            player = self.store.body(namespace, "player", transfer["player_key"]) or {}
            required = [
                ({normalize_name(player.get("name"))}, set()),
                self._names(namespace, transfer["to_team_key"], identity),
            ]
            context = None
        else:
            raise SportsError("invalid_target", "link news to a fixture or a transfer")
        created, keyword_only = [], 0
        for article in self._articles(
            anchor - window_days * DAY_MS, anchor + (window_days + 1) * DAY_MS - 1
        ):
            names = {n for n, _ in article["actors"]}
            ids = {e for _, e in article["actors"] if e}
            matched = [
                sorted((names & wanted) | (ids & entities))
                for wanted, entities in required
            ]
            text = normalize_name(article["title"] + " " + article["content"])
            if not all(matched):
                if all(any(w in text for w in wanted) for wanted, _ in required):
                    keyword_only += (
                        1  # shared words without mentions: never a candidate
                    )
                continue
            if context and context not in text:
                continue
            evidence = compact(
                {
                    "mentions": matched,
                    "competition": context,
                    "window": {
                        "anchor": _date(anchor),
                        "days": window_days,
                        "article_date": article["date"],
                    },
                    "article": {
                        k: article[k]
                        for k in ("title", "url", "outlet", "date", "source_revision")
                    },
                    "basis": "explicit actor mentions of both sides within the date window"
                    + (" and the competition named" if context else ""),
                }
            )
            link = self._append(
                namespace,
                target_type,
                target_key,
                article["document_id"],
                "candidate",
                evidence,
                None,
                principal_id,
                only_new=True,
            )
            if link:
                created.append(link["link_id"])
        return {
            "created": created,
            "keyword_only_not_linked": keyword_only,
            "links": self.links(namespace, target_key, scopes=scopes),
        }

    def _link_id(self, namespace, target_key, document_id):
        return "sports-news-link:" + digest([namespace, target_key, document_id])[:24]

    def _current(self, namespace: str, link_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "sports_news_links"):
            return None
        row = self.conn.execute(
            "SELECT link_id, revision_no, target_type, target_key, document_id, state, evidence_json, reason, "
            "recorded_by, recorded_at_ms FROM sports_news_links WHERE namespace=? AND link_id=? "
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
                    "target_type",
                    "target_key",
                    "document_id",
                    "state",
                    "evidence",
                    "reason",
                    "recorded_by",
                    "recorded_at_ms",
                ),
                row,
            )
        )
        view["evidence"] = json.loads(view["evidence"])
        return compact(view)

    def _append(
        self,
        namespace,
        target_type,
        target_key,
        document_id,
        state,
        evidence,
        reason,
        principal_id,
        *,
        only_new=False,
    ):
        link_id = self._link_id(namespace, target_key, document_id)
        current = self._current(namespace, link_id)
        if current is not None and (only_new or current["state"] == state):
            return None if only_new else current
        number = 1 if current is None else current["revision_no"] + 1
        self.conn.execute(
            "INSERT INTO sports_news_links VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                number,
                target_type,
                target_key,
                document_id,
                state,
                canonical(evidence),
                reason,
                principal_id,
                self.now(),
            ],
        )
        return self._current(namespace, link_id)

    def review(self, namespace, link_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        current = self._current(namespace, link_id)
        if current is None or current["state"] not in {"candidate", "reverted"}:
            raise SportsError(
                "invalid_state", "only a candidate (or a reverted link) can be reviewed"
            )
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise SportsError("invalid_decision", "accept or reject with a reason")
        return self._append(
            namespace,
            current["target_type"],
            current["target_key"],
            current["document_id"],
            "linked" if decision == "accept" else "rejected",
            current["evidence"],
            reason.strip(),
            principal_id,
        )

    def revert(self, namespace, link_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        current = self._current(namespace, link_id)
        if current is None or current["state"] not in {"linked", "rejected"}:
            raise SportsError("invalid_state", "only a reviewed link can be reverted")
        if not str(reason or "").strip():
            raise SportsError("invalid_decision", "a revert needs a reason")
        return self._append(
            namespace,
            current["target_type"],
            current["target_key"],
            current["document_id"],
            "reverted",
            current["evidence"],
            reason.strip(),
            principal_id,
        )

    def links(self, namespace, target_key, *, scopes) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "sports_news_links"):
            return []
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT link_id FROM sports_news_links WHERE namespace=? AND target_key=? ORDER BY link_id",
                [namespace, target_key],
            ).fetchall()
        ]
        return [self._current(namespace, i) for i in ids]

    # ------------------------------------------------------------------ transfers

    def record_transfer(
        self,
        namespace: str,
        *,
        player_name: str,
        to_team_key: str,
        date: str,
        source: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        from_team_key: str | None = None,
        birth_date: str | None = None,
        fee: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """An openly published transfer, cited; a news report alone is refused (it stays a news claim)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready(namespace)
        source = dict(source)
        if source.get("kind") not in TRANSFER_SOURCE_KINDS:
            raise SportsError(
                "not_openly_published",
                "a transfer is recorded only from a governing-body, league or club announcement; a transfer reported "
                "only by news stays a cited news claim",
            )
        if not str(source.get("url") or "").startswith("https://") or not source.get(
            "title"
        ):
            raise SportsError(
                "invalid_transfer",
                "a transfer cites its announcement (https url and title)",
            )
        for key in filter(None, (to_team_key, from_team_key)):
            if self.store.current(namespace, "team", key) is None:
                raise SportsError(
                    "not_found", f"{key} is not a team record in this namespace"
                )
        if fee is not None and (
            set(fee) != {"amount", "currency"} or not source.get("states_fee")
        ):
            raise SportsError(
                "invalid_transfer",
                "a fee is kept only when the announcement states it (states_fee)",
            )
        day = iso_datetime(date)[:10]
        player_key = record_key(
            "transfers-official",
            "player",
            player_name,
            birth_date or "birth-date-not-published",
        )
        transfer_key = record_key(
            "transfers-official",
            "transfer",
            player_name,
            day,
            to_team_key.removeprefix("sports:"),
        )
        published = iso_datetime(source.get("published_at") or day)
        citation = compact(
            {
                "kind": source["kind"],
                "url": source["url"],
                "title": source["title"],
                "publisher": source.get("publisher"),
                "published_at": published,
            }
        )
        observations = [
            {
                "record_type": "player",
                "record_key": player_key,
                "source_record_id": source["url"],
                "locator": source["url"],
                "body": compact(
                    {
                        "provider": "transfers-official",
                        "name": player_name.strip(),
                        "birth_date": birth_date,
                    }
                ),
            },
            {
                "record_type": "transfer_assertion",
                "record_key": transfer_key,
                "source_record_id": source["url"],
                "locator": source["url"],
                "published_at": published,
                "body": compact(
                    {
                        "player_key": player_key,
                        "from_team_key": from_team_key,
                        "to_team_key": to_team_key,
                        "date": day,
                        "fee": None if fee is None else dict(fee),
                        "source": citation,
                    }
                ),
            },
        ]
        applied = self.store.record_manual(
            namespace,
            "transfers-official",
            observations,
            attribution=source.get("publisher") or source["title"],
            citation={"url": source["url"], "published_at": published},
            principal_id=principal_id,
        )
        return {**applied, "player_key": player_key, "transfer_key": transfer_key}


__all__ = ["SportsLinks", "TRANSFER_SOURCE_KINDS"]
