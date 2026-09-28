"""As-of lexeme, sense and definition answers and cited typological profiles (LG08, #2186).

Pure reads over the Linguistics store; nothing here writes. Every answer:

* selects the current record **per source** first (as of ``as_of`` by the
  source's own revision date, and optionally restricted to what was acquired
  by ``acquired_by_ms``) and filters afterwards;
* keeps sources side by side (a Wikidata sense and a Wiktionary sense are
  never blended) and cites each record's revision, source revision, date and
  licence;
* carries a ``licences`` block listing attributions, with ``share_alike: true``
  whenever Wiktionary (CC BY-SA) content is cited;
* lists what is unknown (an unresolved language, a WALS feature with no value
  on record, an etymology endpoint kept as cited text) instead of omitting it.

Noesis translations of a definition are listed separately with their producer
and review status and are never presented as a definition.
:func:`export_lexeme_dossier` refuses exports that would break CC BY-SA:
another licence, or dropped attribution.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kb.linguistics_crosslang import CrossLanguageLinks
from src.kb.linguistics_etymology import Etymologies
from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import (
    GLOTTOCODE,
    ISO639_3,
    READ_SCOPE,
    LinguisticsError,
    authorize,
    citation,
    fold,
    glossing_vocabulary,
    licence_block,
    validate_gloss,
)

CONTRACT = "noesis-linguistic-answer-v1"
SHARE_ALIKE_COMPATIBLE = ("CC-BY-SA-4.0",)
NO_VALUE = "no value on record"


class LinguisticsQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.identity = LinguisticsIdentity(conn, initialize=False, now=now)
        self.store = self.identity.store
        self.etymologies = Etymologies(conn, now=now)
        self.crosslang = CrossLanguageLinks(conn, now=now)

    def _begin(self, namespace: str, scopes: Iterable[str]) -> None:
        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready()

    # ------------------------------------------------------------ lexemes

    def _languoid_filter(
        self, namespace: str, languoid: str | None, as_of: Any
    ) -> dict[str, Any]:
        if not languoid:
            return {"status": "any"}
        if GLOTTOCODE.fullmatch(languoid):
            return self.identity.resolve_languoid(
                namespace, "glottocode", languoid, as_of=as_of
            )
        if ISO639_3.fullmatch(languoid):
            return self.identity.resolve_languoid(
                namespace, "iso639-3", languoid, as_of=as_of
            )
        return self.identity.resolve_languoid(
            namespace, "wiktionary-code", languoid, as_of=as_of
        )

    def lookup_lexeme(
        self,
        namespace: str,
        lemma: str,
        languoid: str | None = None,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Every source's lexeme for a lemma (NFC, case-folded), with forms, paradigms, senses and sources."""
        self._begin(namespace, scopes)
        wanted = self._languoid_filter(namespace, languoid, as_of)
        if wanted["status"] in {"ambiguous", "retired"}:
            return {
                "contract": CONTRACT,
                "query": {"lemma": lemma, "languoid": languoid},
                "status": wanted["status"],
                "languoid": wanted,
                "lexemes": [],
                "n": 0,
                "unknowns": [
                    f"languoid {languoid} is {wanted['status']}: {wanted.get('reason')}"
                ],
                "licences": licence_block([]),
            }
        target = wanted.get("glottocode")
        lexemes, unknowns, cited = [], [], []
        for lexeme in self.store.currents(
            namespace, kind="lexeme", as_of=as_of, acquired_by_ms=acquired_by_ms
        ):
            history = self.store.history(
                namespace, lexeme["record_key"], acquired_by_ms=acquired_by_ms
            )
            lemmas = {
                fold(r["body"]["lemma"]["text"])
                for r in history
                if r["body"].get("lemma")
            }
            if fold(lemma) not in lemmas:
                continue
            # a deleted lexeme keeps the language its last stated revision gave it
            stated = next(
                (
                    r
                    for r in reversed(history)
                    if (r["body"].get("language") or {}).get("scheme")
                ),
                lexeme,
            )
            resolved = self.identity.lexeme_languoid(namespace, stated, as_of=as_of)
            if languoid and resolved.get("glottocode") != target:
                if (
                    resolved["status"] != "resolved"
                    and (lexeme["body"].get("language") or {}).get("value") == languoid
                ):
                    pass  # the source's own code matches; the language itself stays unresolved (listed below)
                else:
                    continue
            if resolved["status"] != "resolved":
                unknowns.append(
                    f"{lexeme['record_key']}: language unresolved ({resolved.get('reason')})"
                )
            lexemes.append(
                self._lexeme(namespace, lexeme, resolved, as_of, acquired_by_ms, cited)
            )
        lexemes.sort(
            key=lambda item: (tuple(item["equivalent_lexemes"]), item["record_key"])
        )
        return {
            "contract": CONTRACT,
            "query": {"lemma": lemma, "languoid": languoid, "as_of": as_of},
            "status": "on_record" if lexemes else "not_on_record",
            "languoid": wanted,
            "lexemes": lexemes,
            "n": len(lexemes),
            "unknowns": unknowns,
            "licences": licence_block(cited),
        }

    def _lexeme(
        self, namespace, lexeme, resolved, as_of, acquired_by_ms, cited
    ) -> dict[str, Any]:
        cited.append(lexeme)
        key = lexeme["record_key"]
        forms = [
            f
            for f in self.store.currents(
                namespace, kind="form", as_of=as_of, acquired_by_ms=acquired_by_ms
            )
            if f["body"]["lexeme"] == key
        ]
        cited += forms
        paradigm: dict[str, list[dict[str, Any]]] = {}
        for form in forms:
            features = (
                "+".join(
                    sorted(
                        f["value"]
                        for f in form["body"].get("grammatical_features") or []
                    )
                )
                or "unmarked"
            )
            paradigm.setdefault(features, []).extend(
                {"text": r["text"], "script": r["script"], "citation": citation(form)}
                for r in form["body"]["representations"]
            )
        senses = []
        for sense in self.store.currents(
            namespace, kind="sense", as_of=as_of, acquired_by_ms=acquired_by_ms
        ):
            if sense["body"]["lexeme"] != key:
                continue
            cited.append(sense)
            senses.append(self._sense(namespace, sense, as_of, acquired_by_ms, cited))
        return {
            "record_key": key,
            "provider": lexeme["provider"],
            "status": lexeme["body"].get("status", "active"),
            "lemma": lexeme["body"].get("lemma")
            or (self.store.history(namespace, key)[0]["body"].get("lemma")),
            "lexical_category": lexeme["body"].get("lexical_category"),
            "language": lexeme["body"].get("language"),
            "languoid": resolved,
            "equivalent_lexemes": self.identity.equivalents(
                namespace, key, kind="lexeme"
            ),
            "identity_candidates": [
                {
                    "candidate_id": c["candidate_id"],
                    "state": c["state"],
                    "basis": c["basis"],
                    "other": c["right_key"] if c["left_key"] == key else c["left_key"],
                }
                for c in self.identity.candidates(
                    namespace, scopes={"operator"}, kind="lexeme", record_key=key
                )
            ],
            "forms": [
                {
                    "record_key": f["record_key"],
                    "representations": f["body"]["representations"],
                    "grammatical_features": f["body"].get("grammatical_features") or [],
                    "citation": citation(f),
                }
                for f in forms
            ],
            "paradigm": dict(sorted(paradigm.items())),
            "senses": senses,
            "citation": citation(lexeme),
        }

    def _sense(self, namespace, sense, as_of, acquired_by_ms, cited) -> dict[str, Any]:
        key = sense["record_key"]
        definitions = []
        for definition in self.store.currents(
            namespace,
            kind="definition_revision",
            as_of=as_of,
            acquired_by_ms=acquired_by_ms,
        ):
            if definition["body"]["sense"] != key:
                continue
            cited.append(definition)
            definitions.append(
                {
                    "language": definition["body"]["language"],
                    "text": definition["body"]["text"],
                    "origin": "source-published",
                    "citation": citation(definition),
                    "noesis_translations": self.crosslang.translations(
                        namespace, definition
                    ),
                }
            )
        examples, vocabulary = [], None
        for example in self.store.currents(
            namespace, kind="usage_example", as_of=as_of, acquired_by_ms=acquired_by_ms
        ):
            if example["body"]["sense"] != key:
                continue
            cited.append(example)
            item = {"text": example["body"]["text"], "citation": citation(example)}
            for field in ("published_translation", "citation"):
                if example["body"].get(field):
                    item["source_" + field if field == "citation" else field] = example[
                        "body"
                    ][field]
            gloss = next(
                (
                    g
                    for g in self.store.currents(namespace, kind="gloss", as_of=as_of)
                    if g["body"].get("example") == example["record_key"]
                ),
                None,
            )
            if gloss is not None:
                vocabulary = vocabulary or glossing_vocabulary(self.conn)
                check = validate_gloss(gloss["body"]["line"], vocabulary)
                item["gloss"] = {
                    "line": gloss["body"]["line"],
                    "validation": check["status"],
                    "unknown_abbreviations": check["unknown"],
                    "vocabulary": check["vocabulary"],
                    "citation": citation(gloss),
                }
                cited.append(gloss)
            examples.append(item)
        return {
            "record_key": key,
            "identity_basis": sense["body"].get("identity_basis"),
            **(
                {"identity_note": sense["body"]["identity_note"]}
                if sense["body"].get("identity_note")
                else {}
            ),
            "item_links": sense["body"].get("item_links") or [],
            "definitions": definitions,
            "usage_examples": examples,
            "citation": citation(sense),
        }

    # ------------------------------------------------------------ senses

    def _definition_keys(self, namespace: str, sense_key: str) -> list[str]:
        return [
            k
            for k in self.store.keys(namespace, kind="definition_revision")
            if (self.store.history(namespace, k) or [{}])[0]
            .get("body", {})
            .get("sense")
            == sense_key
        ]

    def sense_history(
        self,
        namespace: str,
        sense_key: str,
        as_of: Any,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """The definition revision in force per source (and definition language) at a date."""
        self._begin(namespace, scopes)
        in_force, unknowns, cited = [], [], []
        keys = self._definition_keys(namespace, sense_key)
        for key in keys:
            current = self.store.current(
                namespace, key, as_of=as_of, acquired_by_ms=acquired_by_ms
            )
            if current is None:
                unknowns.append(f"{key}: no revision in force on {as_of}")
                continue
            cited.append(current)
            in_force.append(
                {
                    "provider": current["provider"],
                    "language": current["body"]["language"],
                    "text": current["body"]["text"],
                    "revision_id": current["revision_id"],
                    "in_force_since": current["revision_date"],
                    "citation": citation(current),
                }
            )
        if not keys:
            unknowns.append(f"{sense_key}: no definition on record")
        return {
            "contract": CONTRACT,
            "sense": sense_key,
            "as_of": as_of,
            "definitions": in_force,
            "n": len(in_force),
            "unknowns": unknowns,
            "licences": licence_block(cited),
        }

    def definition_changes(
        self,
        namespace: str,
        sense_key: str,
        *,
        scopes: Iterable[str],
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Every definition revision of a sense, per source and language, in source order."""
        self._begin(namespace, scopes)
        changes, cited = [], []
        for key in self._definition_keys(namespace, sense_key):
            history = self.store.history(namespace, key, acquired_by_ms=acquired_by_ms)
            cited += history
            changes.append(
                {
                    "definition": key,
                    "provider": history[0]["provider"],
                    "language": history[0]["body"]["language"],
                    "revisions": [
                        {
                            "revision_id": r["revision_id"],
                            "text": r["body"]["text"],
                            "source_revision": r["source_revision"],
                            "revision_date": r["revision_date"],
                            "previous_revision_id": r["previous_revision_id"],
                            "restated_in": r["restated_in"],
                            **(
                                {"conflict": r["conflict"]} if r.get("conflict") else {}
                            ),
                            "citation": citation(r),
                        }
                        for r in history
                    ],
                }
            )
        return {
            "contract": CONTRACT,
            "sense": sense_key,
            "changes": changes,
            "n": sum(len(c["revisions"]) for c in changes),
            "licences": licence_block(cited),
        }

    # ------------------------------------------------------------ etymology

    def lexeme_etymology(
        self,
        namespace: str,
        lexeme_key: str,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
    ) -> dict[str, Any]:
        self._begin(namespace, scopes)
        chain = self.etymologies.chain(
            namespace, lexeme_key, scopes=scopes, as_of=as_of
        )
        unknowns = [
            f"{link['target'].get('cited')}: {link['target'].get('reason')}"
            for link in chain["links"] + chain["cognates"]
            if link["target"]["status"] != "resolved"
        ]
        return {**chain, "n": len(chain["links"]), "unknowns": unknowns}

    # ------------------------------------------------------------ languoids

    def languoid_profile(
        self,
        namespace: str,
        languoid: str,
        *,
        scopes: Iterable[str],
        as_of: Any = None,
        acquired_by_ms: int | None = None,
    ) -> dict[str, Any]:
        """Identifiers, classification path, location and WALS values (with references) of a languoid."""
        self._begin(namespace, scopes)
        resolved = self._languoid_filter(namespace, languoid, as_of)
        if resolved["status"] != "resolved":
            return {
                "contract": CONTRACT,
                "languoid": languoid,
                "status": resolved["status"],
                "resolution": resolved,
                "n": 0,
                "unknowns": [f"{languoid}: {resolved.get('reason')}"],
                "licences": licence_block([]),
            }
        code = resolved["glottocode"]
        key = f"languoid:glottolog:{code}"
        record = self.store.current(
            namespace, key, as_of=as_of, acquired_by_ms=acquired_by_ms
        )
        cited, unknowns = [record], []
        languoids = {
            r["body"]["glottocode"]: r
            for r in self.store.currents(
                namespace,
                kind="languoid",
                provider="glottolog",
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            )
        }
        body = record["body"]
        path = [
            {
                "glottocode": g,
                "name": languoids[g]["body"]["name"] if g in languoids else None,
            }
            for g in body.get("classification") or []
        ]
        unknowns += [
            f"classification ancestor {p['glottocode']} not acquired"
            for p in path
            if p["name"] is None
        ]
        identifiers = {
            "glottocode": code,
            **({"iso639_3": body["iso639_3"]} if body.get("iso639_3") else {}),
        }
        iso = self.identity._iso(namespace)
        iso_info: dict[str, Any] = {}
        if body.get("iso639_3"):
            iso_record = iso["codes"].get(body["iso639_3"])
            if iso_record:
                cited.append(iso_record)
                iso_info = {
                    "code": body["iso639_3"],
                    "scope": iso_record["body"]["scope"],
                    "name": iso_record["body"]["name"],
                    "citation": citation(iso_record),
                    **(
                        {"part1": iso_record["body"]["part1"]}
                        if iso_record["body"].get("part1")
                        else {}
                    ),
                }
            iso_info["macrolanguages"] = sorted(
                m
                for m, members in iso["members"].items()
                if body["iso639_3"] in members
            )
            iso_info["retired_predecessors"] = [
                self.identity._change_event(c)
                for changes in iso["changes"].values()
                for c in changes
                if body["iso639_3"] in self.identity._change_event(c)["successors"]
            ]
        typology = self._typology(
            namespace, code, as_of, acquired_by_ms, cited, unknowns
        )
        wikidata = [
            {
                "item": r["body"]["item"],
                "label": r["body"].get("label"),
                "citation": citation(r),
            }
            for r in self.store.currents(namespace, kind="language_item", as_of=as_of)
            if code in {g["value"] for g in r["body"].get("glottocodes") or []}
        ]
        display = [
            {
                "locale": r["body"]["locale"],
                "name": r["body"]["value"],
                "citation": citation(r),
            }
            for r in self.store.currents(namespace, kind="locale_data", as_of=as_of)
            if r["body"]["field"] == "language_display_name"
            and r["body"]["subject"] in {body.get("iso639_3"), iso_info.get("part1")}
        ]
        history = self.store.history(namespace, key, acquired_by_ms=acquired_by_ms)
        location = (
            {
                "coordinates": body.get("coordinates"),
                "macroareas": body.get("macroareas") or [],
                "places": self.identity.place_links(namespace, code),
            }
            if body.get("coordinates")
            else None
        )
        if location is None:
            unknowns.append("no coordinates on record")
        return {
            "contract": CONTRACT,
            "languoid": code,
            "status": "on_record",
            "as_of": as_of,
            "name": body["name"],
            "level": body["level"],
            "identifiers": identifiers,
            "parent": body.get("parent"),
            "classification_path": path,
            "location": location,
            "iso639_3": iso_info or None,
            "dialects": sorted(
                g
                for g, r in languoids.items()
                if r["body"].get("parent") == code and r["body"]["level"] == "dialect"
            ),
            "classification_history": [
                {
                    "release": r["source_revision"],
                    "revision_date": r["revision_date"],
                    "parent": r["body"].get("parent"),
                    "classification": r["body"].get("classification") or [],
                    "citation": citation(r),
                }
                for r in history
            ],
            "typology": typology,
            "wikidata_items": wikidata,
            "display_names": display,
            "citation": citation(record),
            "n": len(typology["values"]),
            "unknowns": unknowns,
            "licences": licence_block(cited + [r for r in history]),
        }

    def _typology(
        self, namespace, code, as_of, acquired_by_ms, cited, unknowns
    ) -> dict[str, Any]:
        languages = [
            r
            for r in self.store.currents(
                namespace,
                kind="typological_language",
                as_of=as_of,
                acquired_by_ms=acquired_by_ms,
            )
            if r["body"].get("glottocode") == code
        ]
        wals_codes = sorted(r["body"]["wals_code"] for r in languages)
        cited += languages
        values = []
        parameters = self.store.currents(
            namespace,
            kind="typological_parameter",
            as_of=as_of,
            acquired_by_ms=acquired_by_ms,
        )
        for parameter in sorted(parameters, key=lambda r: r["body"]["parameter"]):
            pid = parameter["body"]["parameter"]
            found = [
                self.store.current(
                    namespace,
                    f"typological_value:wals:{w}:{pid}",
                    as_of=as_of,
                    acquired_by_ms=acquired_by_ms,
                )
                for w in wals_codes
            ]
            found = [f for f in found if f is not None]
            if not found:
                values.append(
                    {
                        "parameter": pid,
                        "name": parameter["body"]["name"],
                        "value": NO_VALUE,
                    }
                )
                unknowns.append(f"WALS {pid}: {NO_VALUE}")
                continue
            cited.append(parameter)
            for value in found:
                cited.append(value)
                values.append(
                    {
                        "parameter": pid,
                        "name": parameter["body"]["name"],
                        "value": value["body"]["value"],
                        "code_id": value["body"].get("code_id"),
                        "wals_code": value["body"]["wals_code"],
                        "references": value["body"].get("references") or [],
                        "citation": citation(value),
                    }
                )
        if not wals_codes:
            unknowns.append("no WALS language is published with this Glottocode")
        return {
            "wals_codes": wals_codes,
            "mapping": "WALS code to Glottocode as WALS publishes it",
            "values": values,
        }

    # ------------------------------------------------------------ export


def export_lexeme_dossier(
    queries: LinguisticsQueries,
    namespace: str,
    lemma: str,
    languoid: str | None,
    *,
    target_licence: str,
    scopes: Iterable[str],
    keep_attribution: bool = True,
    exclude_share_alike: bool = False,
    as_of: Any = None,
) -> dict[str, Any]:
    """A lexeme dossier for export, refused when it would break CC BY-SA or drop attribution."""
    if not keep_attribution:
        raise LinguisticsError(
            "export_refused",
            "attribution cannot be removed from an export of sourced records",
        )
    answer = queries.lookup_lexeme(
        namespace, lemma, languoid, scopes=scopes, as_of=as_of
    )
    etymologies = [
        queries.lexeme_etymology(
            namespace, lexeme["record_key"], scopes=scopes, as_of=as_of
        )
        for lexeme in answer["lexemes"]
    ]
    share_alike_keys = sorted(
        {
            lexeme["record_key"]
            for lexeme in answer["lexemes"]
            if lexeme["citation"]["share_alike"]
        }
    )
    excluded = []
    if (
        answer["licences"]["share_alike"]
        or any(e["licences"]["share_alike"] for e in etymologies)
    ) and target_licence not in SHARE_ALIKE_COMPATIBLE:
        if not exclude_share_alike:
            raise LinguisticsError(
                "export_refused",
                f"the dossier contains CC BY-SA Wiktionary content; {target_licence} is not share-alike compatible",
                records=share_alike_keys,
            )
        excluded = share_alike_keys
        answer = {
            **answer,
            "lexemes": [
                lx for lx in answer["lexemes"] if not lx["citation"]["share_alike"]
            ],
        }
        etymologies = [e for e in etymologies if e["lexeme"] not in excluded]
        for etymology in etymologies:
            for field in ("links", "cognates", "notes"):
                etymology[field] = [
                    item
                    for item in etymology[field]
                    if not item["citation"]["share_alike"]
                ]
    return {
        "contract": "noesis-linguistic-dossier-export-v1",
        "target_licence": target_licence,
        "lexemes": answer["lexemes"],
        "etymologies": etymologies,
        "excluded_share_alike": excluded,
        "n": len(answer["lexemes"]),
        "licences": _citation_licences(_citations([answer["lexemes"], etymologies])),
        "attribution_kept": True,
    }


def _citations(value: Any) -> list[dict[str, Any]]:
    """Every citation nested in an answer."""
    found = []
    if isinstance(value, dict):
        if (
            isinstance(value.get("citation"), dict)
            and "attribution" in value["citation"]
        ):
            found.append(value["citation"])
        for item in value.values():
            found += _citations(item)
    elif isinstance(value, list):
        for item in value:
            found += _citations(item)
    return found


def _citation_licences(citations: list[dict[str, Any]]) -> dict[str, Any]:
    share_alike = any(c["share_alike"] for c in citations)
    return {
        "n": len(citations),
        "licences": sorted({c["licence"] for c in citations}),
        "attributions": sorted({c["attribution"] for c in citations}),
        "share_alike": share_alike,
        "notice": "contains CC BY-SA Wiktionary content: reuse keeps the attribution and the same licence"
        if share_alike
        else "no share-alike content cited",
    }
