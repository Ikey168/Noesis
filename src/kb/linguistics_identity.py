"""Reviewable language and lexeme identity (LG06, #2184).

**Languoids.** A language reference resolves deterministically only when a
source states a code:

* a Glottocode resolves to the Glottolog languoid with that code;
* an ISO 639-3 code resolves to the one languoid Glottolog states it for;
* a Wikidata language item resolves through the Glottocode (P1394) or ISO 639-3
  code (P220) the item states;
* a Wiktionary language code resolves through a documented mapping
  (:data:`WIKTIONARY_CODE_MAPPING`): three-letter codes are ISO 639-3,
  two-letter codes are ISO 639-1 through the SIL table, and Wiktionary-specific
  codes (``xx-pro`` proto-languages, etymology-only codes) never resolve;
* a WALS language resolves through the Glottocode WALS publishes for it.

Ambiguous cases never resolve silently. A macrolanguage code, a retired or
split ISO code, or an ISO code Glottolog states for several languoids becomes
**candidates** that stay ``proposed`` until a reviewer decides. ISO 639-3
retirements, changes and splits from the SIL table are identity-history events
(:meth:`LinguisticsIdentity.record_iso_events`).

**Lexemes.** Lexemes are not canonical entities and are never merged. Wikidata
and Wiktionary lexemes with the same resolved languoid, NFC case-folded lemma
and lexical category are *proposed* as the same word; a shared source-stated
sense item (P5137 on Wikidata, ``wikidata`` on a Wiktextract sense) is stronger
evidence and upgrades a pending candidate. Homographs (the same lemma with a
different lexical category or etymology) are never proposed. Candidates are
only ever proposed across providers.

**State.** Candidates follow ``src/kb/ownership_identity.py``: proposed,
accepted, rejected, reverted. Every transition is a decision in the shared
:class:`src.kb.entity_history.EntityHistoryStore` under the candidate's event
key: ``review`` (proposed), ``match`` (accepted), ``non-match`` (rejected),
``undo`` (reverted). The state is always read back from that history, so a
reviewer can decide through the review inbox (``src/kb/review_targets.py``
routes ``entity`` targets to the same event key). A revert never reactivates
an earlier decision, and a rejected or reverted candidate is proposed again
only with new evidence. :meth:`LinguisticsIdentity.equivalents` is the single
equivalence definition that lookups and monitors share: it groups keys over
accepted candidates, from either side.

**Places.** Glottolog coordinates project into Geospatial reviewably
(:meth:`LinguisticsIdentity.project_locations`): the cited point is stored as a
geometry, a place is registered for it, and a saved resolution waits for a
Geospatial review. It stays ``proposed`` until the review accepts it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.linguistics_records import (
    GLOTTOCODE,
    ISO639_3,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    LinguisticsError,
    authorize,
    canonical,
    digest,
    fold,
    require_scope,
)
from src.kb.linguistics_store import LinguisticsStore, table_exists

CONTRACT = "noesis-linguistic-identity-candidate-v1"
CONFIDENCE = {
    "source-stated-sense-item": 0.8,
    "same-languoid-lemma-category": 0.6,
    "macrolanguage-member": 0.4,
    "retired-code-successor": 0.4,
    "shared-iso-code": 0.4,
}
DECISION_STATES = {
    "review": "proposed",
    "match": "accepted",
    "non-match": "rejected",
    "undo": "reverted",
}
WIKTIONARY_CODE_MAPPING = {
    "three-letter": "ISO 639-3 code, resolved through Glottolog",
    "two-letter": "ISO 639-1 code, mapped to ISO 639-3 through the SIL code table (Part1)",
    "other": "Wiktionary-specific code (proto-languages, etymology-only codes): unresolved, never guessed",
}
# Lexical categories as the sources state them -> a comparison label (Wikidata items: verify ids).
LEXICAL_CATEGORIES = {
    ("wikidata-item", "Q1084"): "noun",
    ("wikidata-item", "Q24905"): "verb",
    ("wikidata-item", "Q34698"): "adjective",
    ("wikidata-item", "Q380057"): "adverb",
    ("wiktionary-pos", "noun"): "noun",
    ("wiktionary-pos", "verb"): "verb",
    ("wiktionary-pos", "adj"): "adjective",
    ("wiktionary-pos", "adv"): "adverb",
}
_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}
GEO_SCOPES = ("knowledge:geospatial:read", "knowledge:geospatial:write")
GEO_REVIEW = "knowledge:geospatial:review"
_DDL = """
CREATE TABLE IF NOT EXISTS ling_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, kind TEXT NOT NULL, left_key TEXT NOT NULL,
  right_key TEXT NOT NULL, basis TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, candidate_id)
);
CREATE TABLE IF NOT EXISTS ling_place_links (
  namespace TEXT NOT NULL, glottocode TEXT NOT NULL, point_hash TEXT NOT NULL, source_revision TEXT NOT NULL,
  geo_namespace TEXT NOT NULL, place_id TEXT NOT NULL, geometry_id TEXT NOT NULL, resolution_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, glottocode, point_hash)
);
"""


def lexical_category(value: Mapping[str, Any]) -> str | None:
    return LEXICAL_CATEGORIES.get((str(value.get("scheme")), str(value.get("value"))))


class LinguisticsIdentity:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = LinguisticsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "ling_identity_candidates")

    # ------------------------------------------------------------ languoid resolution

    def _languoids(
        self, namespace: str, as_of: Any = None
    ) -> dict[str, dict[str, Any]]:
        return {
            r["body"]["glottocode"]: r
            for r in self.store.currents(
                namespace, kind="languoid", provider="glottolog", as_of=as_of
            )
        }

    def _iso(self, namespace: str) -> dict[str, Any]:
        codes = {
            r["body"]["code"]: r
            for r in self.store.currents(namespace, kind="iso_code")
        }
        members: dict[str, list[str]] = {}
        for record in self.store.currents(namespace, kind="iso_macrolanguage"):
            if record["body"]["status"] == "active":
                members.setdefault(record["body"]["macrolanguage"], []).append(
                    record["body"]["member"]
                )
        changes: dict[str, list[dict[str, Any]]] = {}
        for record in self.store.currents(namespace, kind="iso_code_change"):
            changes.setdefault(record["body"]["code"], []).append(record)
        return {
            "codes": codes,
            "members": {k: sorted(v) for k, v in members.items()},
            "changes": changes,
        }

    def resolve_languoid(
        self, namespace: str, scheme: str, value: str, *, as_of: Any = None
    ) -> dict[str, Any]:
        """Deterministic resolution of one language reference; ambiguity is reported, never resolved."""
        languoids = self._languoids(namespace, as_of)
        result: dict[str, Any] = {"reference": {"scheme": scheme, "value": value}}

        def by_glottocode(code: str, basis: str) -> dict[str, Any]:
            if code in languoids:
                return {
                    **result,
                    "status": "resolved",
                    "glottocode": code,
                    "basis": basis,
                    "cites": languoids[code]["revision_id"],
                }
            return {
                **result,
                "status": "unresolved",
                "reason": f"Glottocode {code} is not acquired",
                "basis": basis,
            }

        if scheme == "glottocode":
            return by_glottocode(value, "stated-glottocode")
        if scheme == "wikidata-item":
            item = self.store.current(
                namespace, f"language_item:wikidata-items:{value}"
            )
            if item is None:
                return {
                    **result,
                    "status": "unresolved",
                    "reason": "language item not acquired",
                }
            stated = sorted({g["value"] for g in item["body"].get("glottocodes") or []})
            if len(stated) == 1:
                return {
                    **by_glottocode(stated[0], "item-stated-glottocode"),
                    "item_revision": item["revision_id"],
                }
            isos = sorted({g["value"] for g in item["body"].get("iso639_3") or []})
            if not stated and len(isos) == 1:
                return {
                    **self.resolve_languoid(
                        namespace, "iso639-3", isos[0], as_of=as_of
                    ),
                    "reference": result["reference"],
                    "item_revision": item["revision_id"],
                }
            return {
                **result,
                "status": "ambiguous" if stated or isos else "unresolved",
                "reason": "the item states several codes"
                if stated or isos
                else "the item states no code",
                "candidates": [{"glottocode": g} for g in stated],
            }
        if scheme == "wiktionary-code":
            if ISO639_3.fullmatch(value):
                return {
                    **self.resolve_languoid(namespace, "iso639-3", value, as_of=as_of),
                    "reference": result["reference"],
                    "mapping": WIKTIONARY_CODE_MAPPING["three-letter"],
                }
            if len(value) == 2 and value.isalpha():
                iso = self._iso(namespace)["codes"]
                matches = [c for c, r in iso.items() if r["body"].get("part1") == value]
                if len(matches) == 1:
                    return {
                        **self.resolve_languoid(
                            namespace, "iso639-3", matches[0], as_of=as_of
                        ),
                        "reference": result["reference"],
                        "mapping": WIKTIONARY_CODE_MAPPING["two-letter"],
                    }
                return {
                    **result,
                    "status": "unresolved",
                    "mapping": WIKTIONARY_CODE_MAPPING["two-letter"],
                    "reason": "no acquired ISO 639-3 code states this ISO 639-1 code",
                }
            return {
                **result,
                "status": "unresolved",
                "mapping": WIKTIONARY_CODE_MAPPING["other"],
                "reason": "Wiktionary-specific code without a documented mapping",
            }
        if scheme == "wals-code":
            language = self.store.current(
                namespace, f"typological_language:wals:{value}"
            )
            if language is None or not language["body"].get("glottocode"):
                return {
                    **result,
                    "status": "unresolved",
                    "reason": "WALS language or its Glottocode not acquired",
                }
            return by_glottocode(
                language["body"]["glottocode"], "wals-published-glottocode"
            )
        if scheme != "iso639-3":
            raise LinguisticsError(
                "invalid_reference", f"unknown language reference scheme {scheme!r}"
            )
        iso = self._iso(namespace)
        record = iso["codes"].get(value)
        if value in iso["changes"]:
            events = [self._change_event(c) for c in iso["changes"][value]]
            successors = sorted({s for e in events for s in e["successors"]})
            return {
                **result,
                "status": "retired",
                "events": events,
                "candidates": [
                    {
                        "iso639_3": s,
                        **(
                            {"glottocode": g}
                            if (g := self._by_iso(languoids, s))
                            else {}
                        ),
                    }
                    for s in successors
                ],
                "reason": "the ISO 639-3 code is retired; successors are candidates for review",
            }
        if record is not None and record["body"]["scope"] == "macrolanguage":
            members = iso["members"].get(value, [])
            return {
                **result,
                "status": "ambiguous",
                "reason": "a macrolanguage never resolves to one language",
                "candidates": [
                    {
                        "iso639_3": m,
                        **(
                            {"glottocode": g}
                            if (g := self._by_iso(languoids, m))
                            else {}
                        ),
                    }
                    for m in members
                ],
            }
        matches = sorted(
            code for code, r in languoids.items() if r["body"].get("iso639_3") == value
        )
        if len(matches) == 1:
            return by_glottocode(matches[0], "glottolog-stated-iso639-3")
        if matches:
            return {
                **result,
                "status": "ambiguous",
                "reason": "Glottolog states this ISO code for several languoids",
                "candidates": [{"glottocode": g} for g in matches],
            }
        return {
            **result,
            "status": "unresolved",
            "reason": "no acquired languoid states this ISO 639-3 code",
        }

    @staticmethod
    def _by_iso(languoids: Mapping[str, Mapping[str, Any]], code: str) -> str | None:
        found = sorted(
            g for g, r in languoids.items() if r["body"].get("iso639_3") == code
        )
        return found[0] if len(found) == 1 else None

    @staticmethod
    def _change_event(record: Mapping[str, Any]) -> dict[str, Any]:
        body = record["body"]
        successors = list(body.get("split_into") or []) + (
            [body["change_to"]] if body.get("change_to") else []
        )
        return {
            "code": body["code"],
            "reason": body["reason"],
            "effective": body["effective"],
            "successors": successors,
            "remedy": body.get("remedy"),
            "cites": record["revision_id"],
        }

    def lexeme_languoid(
        self, namespace: str, lexeme: Mapping[str, Any], *, as_of: Any = None
    ) -> dict[str, Any]:
        language = lexeme["body"].get("language") or {}
        if not language.get("scheme"):
            return {"status": "unresolved", "reason": "the lexeme states no language"}
        return self.resolve_languoid(
            namespace, str(language["scheme"]), str(language["value"]), as_of=as_of
        )

    # ------------------------------------------------------------ ISO events

    def record_iso_events(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        """Each acquired ISO 639-3 retirement becomes an identity-history decision (idempotent)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        out = []
        for record in self.store.currents(namespace, kind="iso_code_change"):
            event = self._change_event(record)
            subjects = [
                f"iso639-3:{event['code']}",
                *[f"iso639-3:{s}" for s in event["successors"]],
            ]
            for subject in subjects:
                self.history.register_entity(
                    namespace,
                    subject,
                    [subject],
                    principal_id=principal_id,
                    scopes=_HISTORY_SCOPES,
                )
            decision_type = {"split": "split", "non-existent": "review"}.get(
                event["reason"], "redirect"
            )
            decided = self.history.decide(
                namespace,
                decision_type,
                subjects,
                {
                    "source": "sil-iso639-3",
                    "reason": event["reason"],
                    "effective": event["effective"],
                    "successors": event["successors"],
                    "remedy": event["remedy"],
                    "cites": event["cites"],
                    "valid_time": {"from": event["effective"]},
                    "provenance": {
                        "producer": "linguistics.languoids",
                        "record": record["record_key"],
                    },
                    "policy": {
                        "merge": False,
                        "note": "source-stated code history; languoids stay separate",
                    },
                },
                reviewer_id=principal_id,
                principal_id=principal_id,
                scopes=_HISTORY_SCOPES,
                event_key=f"linguistics-iso:{namespace}:{event['code']}:{event['effective']}",
            )
            out.append(
                {
                    "code": event["code"],
                    "decision_id": decided["decision_id"],
                    "decision_type": decision_type,
                    "idempotent": decided.get("idempotent", False),
                    "event": event,
                }
            )
        return out

    # ------------------------------------------------------------ candidates

    def _event_key(self, namespace: str, candidate_id: str) -> str:
        return f"linguistics-identity:{namespace}:{candidate_id}"

    def _latest(self, namespace: str, candidate_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "entity_identity_decisions"):
            return None
        row = self.conn.execute(
            "SELECT payload_json FROM entity_identity_decisions WHERE namespace=? AND event_key=? "
            "ORDER BY revision DESC LIMIT 1",
            [namespace, self._event_key(namespace, candidate_id)],
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT kind, left_key, right_key, basis, confidence, evidence_json, created_by, created_at_ms, history_json "
            "FROM ling_identity_candidates WHERE namespace=? AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        if row is None:
            raise LinguisticsError(
                "not_found", "identity candidate is not visible in this namespace"
            )
        latest = self._latest(namespace, candidate_id)
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            "candidate_id": candidate_id,
            "kind": row[0],
            "left_key": row[1],
            "right_key": row[2],
            "basis": row[3],
            "confidence": row[4],
            "evidence": json.loads(row[5]),
            "created_by": row[6],
            "created_at_ms": int(row[7]),
            "history": json.loads(row[8]),
            "state": DECISION_STATES.get(
                (latest or {}).get("decision_type"), "proposed"
            ),
            "decision_id": (latest or {}).get("decision_id"),
            "event_key": self._event_key(namespace, candidate_id),
            "notice": "a candidate is a reviewable proposal; records are never merged",
        }

    def offer(
        self,
        namespace: str,
        *,
        kind: str,
        left_key: str,
        right_key: str,
        basis: str,
        evidence: list[Mapping[str, Any]],
        principal_id: str,
    ) -> dict[str, Any]:
        """Add a candidate, upgrade a pending one with stronger evidence, or re-propose after rejection/revert."""
        if basis not in CONFIDENCE or left_key == right_key or not evidence:
            raise LinguisticsError(
                "invalid_candidate",
                "a candidate needs two records, a known basis and evidence",
            )
        a, b = sorted((left_key, right_key))
        candidate_id = "ling-idc:" + digest([namespace, kind, a, b])[:24]
        evidence = [dict(e) for e in evidence]
        existing = self.conn.execute(
            "SELECT basis, evidence_json, history_json FROM ling_identity_candidates "
            "WHERE namespace=? AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        now = self.now()
        if existing is None:
            self.conn.execute(
                "INSERT INTO ling_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    candidate_id,
                    kind,
                    a,
                    b,
                    basis,
                    CONFIDENCE[basis],
                    canonical(evidence),
                    principal_id,
                    now,
                    canonical(
                        [{"state": "proposed", "by": principal_id, "at_ms": now}]
                    ),
                ],
            )
            change = "created"
        else:
            state = self._row(namespace, candidate_id)["state"]
            old_basis, old_evidence, history = (
                existing[0],
                json.loads(existing[1]),
                json.loads(existing[2]),
            )
            new = digest(evidence) != digest(old_evidence) or basis != old_basis
            if state == "proposed" and CONFIDENCE[basis] > CONFIDENCE[old_basis]:
                change = "upgraded"
            elif state in {"rejected", "reverted"} and new:
                change = "reproposed"
            else:
                return {"candidate_id": candidate_id, "change": None}
            history.append(
                {
                    "state": "proposed",
                    "by": principal_id,
                    "at_ms": now,
                    "change": change,
                    "previous_state": state,
                    "previous_basis": old_basis,
                    "previous_evidence": old_evidence,
                }
            )
            self.conn.execute(
                "UPDATE ling_identity_candidates SET basis=?, confidence=?, evidence_json=?, "
                "history_json=? WHERE namespace=? AND candidate_id=?",
                [
                    basis,
                    CONFIDENCE[basis],
                    canonical(evidence),
                    canonical(history),
                    namespace,
                    candidate_id,
                ],
            )
        for key in (a, b):
            self.history.register_entity(
                namespace, key, [key], principal_id=principal_id, scopes=_HISTORY_SCOPES
            )
        self.history.decide(
            namespace,
            "review",
            [a, b],
            {
                "candidate_id": candidate_id,
                "basis": basis,
                "evidence": evidence,
                "change": change,
                "provenance": {"producer": "linguistics.lexicon"},
                "policy": {
                    "merge": False,
                    "note": "lexemes and languoids are never merged",
                },
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_HISTORY_SCOPES,
            event_key=self._event_key(namespace, candidate_id),
        )
        return {"candidate_id": candidate_id, "change": change}

    def propose_languoid_matches(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Candidates for every acquired language reference that does not resolve deterministically."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        references = set()
        for lexeme in self.store.currents(namespace, kind="lexeme"):
            language = lexeme["body"].get("language") or {}
            if language.get("scheme"):
                references.add((str(language["scheme"]), str(language["value"])))
        for code in self._iso(namespace)["codes"]:
            references.add(("iso639-3", code))
        for code in self._iso(namespace)["changes"]:
            references.add(("iso639-3", code))
        changes, resolutions = [], {}
        for scheme, value in sorted(references):
            resolved = self.resolve_languoid(namespace, scheme, value)
            resolutions[f"{scheme}:{value}"] = resolved["status"]
            if resolved["status"] not in {"ambiguous", "retired"}:
                continue
            basis = (
                "retired-code-successor"
                if resolved["status"] == "retired"
                else (
                    "macrolanguage-member"
                    if "macrolanguage" in resolved.get("reason", "")
                    else "shared-iso-code"
                )
            )
            for candidate in resolved.get("candidates") or []:
                if not candidate.get("glottocode"):
                    continue
                offered = self.offer(
                    namespace,
                    kind="languoid",
                    left_key=f"language-ref:{scheme}:{value}",
                    right_key=f"languoid:glottolog:{candidate['glottocode']}",
                    basis=basis,
                    evidence=[
                        {
                            "reference": resolved["reference"],
                            "reason": resolved["reason"],
                            **(
                                {"events": resolved["events"]}
                                if resolved.get("events")
                                else {}
                            ),
                            "candidate": candidate,
                        }
                    ],
                    principal_id=principal_id,
                )
                if offered["change"]:
                    changes.append(offered)
        return {
            "proposed": changes,
            "resolutions": resolutions,
            "candidates": self.candidates(namespace, scopes=scopes, kind="languoid"),
        }

    def propose_lexeme_matches(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Cross-provider lexeme candidates: same languoid, lemma and category; shared sense items upgrade them."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        unmatched = []
        for lexeme in self.store.currents(
            namespace, kind="lexeme"
        ):  # current per source first, then filtered
            if lexeme["body"].get("status") == "deleted":
                continue
            languoid = self.lexeme_languoid(namespace, lexeme)
            category = lexical_category(lexeme["body"]["lexical_category"])
            if languoid["status"] != "resolved" or category is None:
                unmatched.append(
                    {
                        "record_key": lexeme["record_key"],
                        "languoid": languoid["status"],
                        "category": category,
                    }
                )
                continue
            groups.setdefault(
                (
                    languoid["glottocode"],
                    fold(lexeme["body"]["lemma"]["text"]),
                    category,
                ),
                [],
            ).append(lexeme)
        items = self._sense_items(namespace)
        changes = []
        for (glottocode, lemma, category), lexemes in sorted(groups.items()):
            for i, left in enumerate(lexemes):
                for right in lexemes[i + 1 :]:
                    if left["provider"] == right["provider"]:
                        continue  # homographs within one source stay separate
                    shared = sorted(
                        items.get(left["record_key"], set())
                        & items.get(right["record_key"], set())
                    )
                    basis = (
                        "source-stated-sense-item"
                        if shared
                        else "same-languoid-lemma-category"
                    )
                    evidence = [
                        {
                            "glottocode": glottocode,
                            "lemma_folded": lemma,
                            "category": category,
                            "left": {
                                "record_key": left["record_key"],
                                "revision_id": left["revision_id"],
                            },
                            "right": {
                                "record_key": right["record_key"],
                                "revision_id": right["revision_id"],
                            },
                            **({"shared_sense_items": shared} if shared else {}),
                        }
                    ]
                    offered = self.offer(
                        namespace,
                        kind="lexeme",
                        left_key=left["record_key"],
                        right_key=right["record_key"],
                        basis=basis,
                        evidence=evidence,
                        principal_id=principal_id,
                    )
                    if offered["change"]:
                        changes.append(offered)
        return {
            "proposed": changes,
            "unmatched": unmatched,
            "candidates": self.candidates(namespace, scopes=scopes, kind="lexeme"),
        }

    def _sense_items(self, namespace: str) -> dict[str, set[str]]:
        items: dict[str, set[str]] = {}
        for sense in self.store.currents(namespace, kind="sense"):
            for link in sense["body"].get("item_links") or []:
                items.setdefault(sense["body"]["lexeme"], set()).add(link["value"])
        return items

    def candidates(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        kind: str | None = None,
        state: str | None = None,
        record_key: str | None = None,
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM ling_identity_candidates WHERE namespace=? AND (? IS NULL OR kind=?) "
            "AND (? IS NULL OR left_key=? OR right_key=?) ORDER BY candidate_id",
            [namespace, kind, kind, record_key, record_key, record_key],
        ).fetchall()
        out = [self._row(namespace, r[0]) for r in rows]
        return [c for c in out if state is None or c["state"] == state]

    def review(
        self,
        namespace: str,
        candidate_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LinguisticsError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise LinguisticsError(
                "invalid_state",
                f"candidate is {candidate['state']}; propose again to re-review",
            )
        self.history.decide(
            namespace,
            "match" if decision == "accept" else "non-match",
            [candidate["left_key"], candidate["right_key"]],
            {
                "candidate_id": candidate_id,
                "basis": candidate["basis"],
                "evidence": candidate["evidence"],
                "reason": reason.strip(),
                "policy": {"merge": False},
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_HISTORY_SCOPES,
            event_key=candidate["event_key"],
        )
        return self._row(namespace, candidate_id)

    def revert(
        self,
        namespace: str,
        candidate_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LinguisticsError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise LinguisticsError(
                "invalid_state",
                "only an accepted or rejected candidate can be reverted",
            )
        self.history.decide(
            namespace,
            "undo",
            [candidate["left_key"], candidate["right_key"]],
            {
                "candidate_id": candidate_id,
                "undoes": candidate["decision_id"],
                "reason": reason.strip(),
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_HISTORY_SCOPES,
            event_key=candidate["event_key"],
        )
        return self._row(namespace, candidate_id)

    def equivalents(
        self, namespace: str, key: str, *, kind: str | None = None
    ) -> list[str]:
        """Keys grouped with ``key`` over accepted candidates, found from either side (the one equivalence)."""
        if not self._ready():
            return [key]
        rows = self.conn.execute(
            "SELECT candidate_id, left_key, right_key FROM ling_identity_candidates "
            "WHERE namespace=? AND (? IS NULL OR kind=?)",
            [namespace, kind, kind],
        ).fetchall()
        edges = [
            (left, right)
            for cid, left, right in rows
            if self._row(namespace, cid)["state"] == "accepted"
        ]
        group, frontier = {key}, [key]
        while frontier:
            current = frontier.pop()
            for left, right in edges:
                for a, b in ((left, right), (right, left)):
                    if a == current and b not in group:
                        group.add(b)
                        frontier.append(b)
        return sorted(group)

    def languoid_for(self, namespace: str, reference_key: str) -> str | None:
        """The Glottocode an ambiguous reference was reviewed to (accepted candidate), if any."""
        glottocodes = [
            k.split(":", 2)[2]
            for k in self.equivalents(namespace, reference_key, kind="languoid")
            if k.startswith("languoid:glottolog:")
        ]
        return glottocodes[0] if len(glottocodes) == 1 else None

    # ------------------------------------------------------------ places

    def project_locations(
        self,
        namespace: str,
        *,
        geo_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Store each languoid's cited point and save a place resolution that waits for Geospatial review."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        for scope in GEO_SCOPES:
            require_scope(scopes, scope)
        self.store.require_ready("glottolog")
        geo = GeospatialStore(self.conn, now=self.now)
        links = []
        for record in self.store.currents(
            namespace, kind="languoid", provider="glottolog"
        ):
            body = record["body"]
            if not body.get("coordinates") or not GLOTTOCODE.fullmatch(
                body["glottocode"]
            ):
                continue
            point = [
                float(body["coordinates"]["longitude"]),
                float(body["coordinates"]["latitude"]),
            ]
            point_hash = digest(point)[:16]
            # A later release restating the same point reuses the reviewed link; a moved point is a new link.
            if self.conn.execute(
                "SELECT 1 FROM ling_place_links WHERE namespace=? AND glottocode=? AND point_hash=?",
                [namespace, body["glottocode"], point_hash],
            ).fetchone():
                links.append(
                    self._place_link(namespace, body["glottocode"], point_hash)
                )
                continue
            place = geo.register_place(
                geo_namespace,
                f"{body['name']} (Glottolog point)",
                "languoid-point",
                names=[{"value": body["name"], "kind": "canonical", "language": "und"}],
                source_ids={"glottocode": body["glottocode"]},
                parent_ids=[],
                principal_id=principal_id,
                scopes=set(GEO_SCOPES),
                place_key=f"glottolog:{body['glottocode']}:{point_hash}",
                geometry={"type": "Point", "coordinates": point},
                observed_at_ms=record["observed_at_ms"],
                producer={"name": "linguistics.languoids", "version": "1.0.0"},
                provenance={
                    "source": "glottolog",
                    "revision_id": record["revision_id"],
                    "attribution": record["source"]["licence"]["attribution"],
                },
            )
            geometry = geo.geometries(
                geo_namespace, place["place_id"], scopes={GEO_SCOPES[0]}
            )[0]
            request = {
                "namespace": geo_namespace,
                "mention": f"glottocode:{body['glottocode']}",
                "point": point_hash,
                "candidates": [place["place_id"]],
            }
            input_hash = digest(request)
            saved = geo.save_resolution(
                {
                    "resolution_id": "geocode-resolution:" + input_hash[:24],
                    "namespace": geo_namespace,
                    "mention": request["mention"],
                    "context": {
                        "system": "glottocode",
                        "producer": "linguistics.languoids",
                        "release": record["source_revision"],
                    },
                    "candidates": [
                        {
                            "place_id": place["place_id"],
                            "namespace": geo_namespace,
                            "confidence": 1.0,
                            "reasons": ["glottolog-stated-point"],
                        }
                    ],
                    "status": "ambiguous",
                    "selected_place_id": None,
                    "confidence": 0.0,
                    "evidence": [
                        {
                            "revision_id": record["revision_id"],
                            "record_key": record["record_key"],
                        }
                    ],
                    "method": {
                        "name": "glottolog-point",
                        "version": "1",
                        "review": "required",
                    },
                    "input_hash": input_hash,
                },
                principal_id=principal_id,
                scopes={GEO_SCOPES[1]},
            )
            self.conn.execute(
                "INSERT INTO ling_place_links VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    namespace,
                    body["glottocode"],
                    point_hash,
                    record["source_revision"],
                    geo_namespace,
                    place["place_id"],
                    geometry["geometry_id"],
                    saved["resolution_id"],
                    self.now(),
                ],
            )
            links.append(self._place_link(namespace, body["glottocode"], point_hash))
        return {"links": links}

    def _place_link(
        self, namespace: str, glottocode: str, point_hash: str
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT geo_namespace, place_id, geometry_id, resolution_id, source_revision FROM ling_place_links "
            "WHERE namespace=? AND glottocode=? AND point_hash=?",
            [namespace, glottocode, point_hash],
        ).fetchone()
        review = (
            self.conn.execute(
                "SELECT decision, selected_place_id FROM geocode_reviews WHERE resolution_id=? ORDER BY revision DESC "
                "LIMIT 1",
                [row[3]],
            ).fetchone()
            if table_exists(self.conn, "geocode_reviews")
            else None
        )
        state = {"accept": "accepted", "reject": "rejected", "defer": "proposed"}.get(
            review[0] if review else "", "proposed"
        )
        return {
            "glottocode": glottocode,
            "first_glottolog_release": row[4],
            "geo_namespace": row[0],
            "place_id": row[1],
            "geometry_id": row[2],
            "resolution_id": row[3],
            "state": state,
            "selected_place_id": review[1] if review and state == "accepted" else None,
        }

    def place_links(self, namespace: str, glottocode: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "ling_place_links"):
            return []
        return [
            self._place_link(namespace, glottocode, r[0])
            for r in self.conn.execute(
                "SELECT point_hash FROM ling_place_links WHERE namespace=? AND glottocode=? ORDER BY created_at_ms, point_hash",
                [namespace, glottocode],
            ).fetchall()
        ]

    def review_location(
        self,
        namespace: str,
        resolution_id: str,
        decision: str,
        *,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Review a languoid place resolution through the Geospatial owner."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, GEO_REVIEW)
        row = (
            self.conn.execute(
                "SELECT geo_namespace, place_id FROM ling_place_links WHERE namespace=? AND "
                "resolution_id=?",
                [namespace, resolution_id],
            ).fetchone()
            if table_exists(self.conn, "ling_place_links")
            else None
        )
        if row is None:
            raise LinguisticsError(
                "not_found",
                "the resolution is not one of this namespace's languoid places",
            )
        return GeospatialStore(self.conn, now=self.now).review(
            row[0],
            resolution_id,
            decision,
            selected_place_id=row[1] if decision == "accept" else None,
            reason=reason,
            principal_id=principal_id,
            scopes={GEO_REVIEW},
        )
