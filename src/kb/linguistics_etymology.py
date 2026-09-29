"""Etymology links as cited assertions and assembled chains (LG07, #2185).

An etymology is a chain of ``etymology_assertion`` records. Each assertion
links a lexeme to a source form with a relation (inherited, borrowed, derived,
calque, cognate, compound-of). The source's native value is kept beside it: a
Wikidata property with its mode-of-derivation qualifier, or a Wiktionary
template name. Chains are assembled **only** from assertions:

* an endpoint resolves to a lexeme when the source names its id (a Wikidata
  L-id), or when exactly one acquired lexeme group has the stated language and
  lemma. Groups are formed over accepted identity candidates (LG06). Otherwise
  the endpoint stays cited text: a reconstructed ``*`` form, a term in an
  unacquired language, or an ambiguous homograph. No lexeme is ever invented;
* cognates are listed beside the chain, never followed as ancestry;
* sources that disagree (borrowed from X vs inherited from Y) are reported
  side by side as a conflict and all branches are shown. Nothing is resolved
  or ranked;
* a cycle is reported where it closes, and assembly stops there;
* a reference with a DOI resolves to Science literature records through
  :func:`src.domains.research.analytics.papers_by_identifier`. A reference
  without one is quoted as published.

Free-text etymology notes are returned verbatim with their citation.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import (
    READ_SCOPE,
    authorize,
    citation,
    fold,
    licence_block,
)

CONTRACT = "noesis-linguistic-etymology-v1"
CHAIN_RELATIONS = ("inherited", "borrowed", "derived", "calque", "compound-of")
MAX_DEPTH = 12


class Etymologies:
    def __init__(self, conn: Any, *, initialize: bool = False, now=None) -> None:
        self.conn = conn
        self.identity = LinguisticsIdentity(conn, initialize=initialize, now=now)
        self.store = self.identity.store

    # ------------------------------------------------------------ endpoints

    def lexemes_named(
        self, namespace: str, language: str, text: str, *, as_of: Any = None
    ) -> list[dict[str, Any]]:
        """Acquired lexemes (current, not deleted) with this lemma in this language (Wiktionary code or ISO)."""
        wanted = self.identity.resolve_languoid(
            namespace, "wiktionary-code", language, as_of=as_of
        )
        out = []
        for lexeme in self.store.currents(namespace, kind="lexeme", as_of=as_of):
            body = lexeme["body"]
            if body.get("status") == "deleted" or fold(
                (body.get("lemma") or {}).get("text", "")
            ) != fold(text):
                continue
            languoid = self.identity.lexeme_languoid(namespace, lexeme, as_of=as_of)
            same = (
                wanted["status"] == "resolved"
                and languoid.get("glottocode") == wanted["glottocode"]
            ) or (
                (body.get("language") or {}).get("value") == language
                and (body.get("language") or {}).get("scheme") == "wiktionary-code"
            )
            if same:
                out.append(lexeme)
        return out

    def resolve_target(
        self, namespace: str, assertion: dict[str, Any], *, as_of: Any = None
    ) -> dict[str, Any]:
        target = assertion["body"]["target"]
        if target.get("source_lexeme_id"):
            key = f"lexeme:{target['provider']}:{target['source_lexeme_id']}"
            current = self.store.current(namespace, key, as_of=as_of)
            if current is None:
                return {
                    "status": "unresolved",
                    "cited": target,
                    "reason": "the cited lexeme is not acquired; kept as its id",
                }
            return {
                "status": "resolved",
                "lexeme": key,
                "group": self.identity.equivalents(namespace, key, kind="lexeme"),
                "lemma": (current["body"].get("lemma") or {}).get("text"),
                "basis": "source-stated-id",
            }
        text, language = target.get("text"), target.get("language")
        if not text:
            return {"status": "unresolved", "cited": target, "reason": "no term stated"}
        if text.startswith("*"):
            return {
                "status": "unresolved",
                "cited": target,
                "reason": "reconstructed form; kept as cited text",
            }
        found = self.lexemes_named(namespace, language, text, as_of=as_of)
        groups = {
            tuple(self.identity.equivalents(namespace, f["record_key"], kind="lexeme"))
            for f in found
        }
        if len(groups) == 1:
            group = list(next(iter(groups)))
            return {
                "status": "resolved",
                "lexeme": group[0],
                "group": group,
                "lemma": text,
                "basis": "stated-language-and-lemma",
            }
        if groups:
            return {
                "status": "ambiguous",
                "cited": target,
                "candidates": sorted(f["record_key"] for f in found),
                "reason": "several acquired lexemes (homographs or unreviewed matches) carry this lemma",
            }
        return {
            "status": "unresolved",
            "cited": target,
            "reason": "no acquired lexeme in this language has this lemma",
        }

    def _references(self, assertion: dict[str, Any]) -> list[dict[str, Any]]:
        from src.domains.research.analytics import papers_by_identifier

        out = []
        for reference in assertion["body"].get("references") or []:
            item = dict(reference)
            if reference.get("doi"):
                item["literature"] = papers_by_identifier(
                    self.conn, doi=reference["doi"]
                )
            out.append(item)
        return out

    # ------------------------------------------------------------ chains

    def _assertions(
        self, namespace: str, group: Iterable[str], *, as_of: Any = None
    ) -> list[dict[str, Any]]:
        keys = set(group)
        return [
            a
            for a in self.store.currents(
                namespace, kind="etymology_assertion", as_of=as_of
            )
            if a["body"]["lexeme"] in keys
        ]

    def chain(
        self,
        namespace: str,
        lexeme_key: str,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready()
        root = self.store.current(namespace, lexeme_key, as_of=as_of)
        if root is None:
            return {
                "contract": CONTRACT,
                "lexeme": lexeme_key,
                "status": "not_on_record",
                "links": [],
                "cognates": [],
                "notes": [],
                "conflicts": [],
                "cycles": [],
                "licences": licence_block([]),
            }
        links, cognates, notes, conflicts, cycles, cited = [], [], [], [], [], [root]
        root_group = self.identity.equivalents(namespace, lexeme_key, kind="lexeme")

        def visit(group: list[str], depth: int, path: list[tuple[str, ...]]) -> None:
            assertions = self._assertions(namespace, group, as_of=as_of)
            for note in self.store.currents(
                namespace, kind="etymology_note", as_of=as_of
            ):
                if depth == 0 and note["body"]["lexeme"] in group:
                    notes.append(
                        {
                            "text": note["body"]["text"],
                            "note": "verbatim; never parsed into links",
                            "citation": citation(note),
                        }
                    )
                    cited.append(note)
            ancestry = []
            for assertion in sorted(assertions, key=lambda a: a["record_key"]):
                cited.append(assertion)
                target = self.resolve_target(namespace, assertion, as_of=as_of)
                link = {
                    "depth": depth,
                    "from": assertion["body"]["lexeme"],
                    "relation": assertion["body"]["relation"],
                    "native_relation": assertion["body"]["native_relation"],
                    "target": target,
                    "reference_status": assertion["body"].get("reference_status"),
                    "references": self._references(assertion),
                    "citation": citation(assertion),
                }
                if assertion["body"].get("expansion"):
                    link["as_published"] = assertion["body"]["expansion"]
                if assertion["body"]["relation"] == "cognate":
                    cognates.append(link)
                    continue
                links.append(link)
                ancestry.append((assertion, target, link))
            identities = {}
            for assertion, target, _ in ancestry:
                if target["status"] == "resolved":
                    ident = ("lexeme", tuple(target["group"]))
                else:
                    cited_target = target.get("cited") or {}
                    ident = (
                        "text",
                        cited_target.get("language"),
                        fold(cited_target.get("text", "")),
                        cited_target.get("source_lexeme_id"),
                    )
                identities.setdefault(
                    (assertion["body"]["relation"], ident), []
                ).append(assertion)
            if len(identities) > 1:
                conflicts.append(
                    {
                        "depth": depth,
                        "lexemes": group,
                        "note": "sources disagree; shown side by side, nothing resolved",
                        "alternatives": [
                            {
                                "relation": relation,
                                "target": ident[1]
                                if ident[0] == "lexeme"
                                else list(ident[1:]),
                                "providers": sorted({a["provider"] for a in items}),
                                "citations": [citation(a) for a in items],
                            }
                            for (relation, ident), items in sorted(
                                identities.items(), key=lambda kv: str(kv[0])
                            )
                        ],
                    }
                )
            if depth >= MAX_DEPTH:
                return
            for _, target, link in ancestry:
                if target["status"] != "resolved":
                    continue
                nxt = tuple(target["group"])
                if nxt in path:
                    cycles.append(
                        {
                            "at": link["from"],
                            "back_to": list(nxt),
                            "path": [list(p) for p in path],
                        }
                    )
                    link["cycle"] = True
                    continue
                visit(list(nxt), depth + 1, [*path, nxt])

        visit(root_group, 0, [tuple(root_group)])
        return {
            "contract": CONTRACT,
            "lexeme": lexeme_key,
            "lemma": (root["body"].get("lemma") or {}).get("text"),
            "equivalent_lexemes": root_group,
            "status": "on_record"
            if links or cognates or notes
            else "no_etymology_on_record",
            "links": links,
            "cognates": cognates,
            "notes": notes,
            "conflicts": conflicts,
            "cycles": cycles,
            "licences": licence_block(cited),
        }
