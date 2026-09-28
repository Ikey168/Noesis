"""Cross-language links through the existing multilingual records (LG09, #2187).

The Linguistics pack writes no alias, translation or search table of its own.
Every cross-language artefact goes through :mod:`src.kb.cross_language`:

* **sourced links** (:meth:`CrossLanguageLinks.links`) are read from the
  Linguistics records themselves. A Wiktionary translation-table row
  (``sourced_translation``) links the entry's sense to a word in another
  language. Two senses that state the same Wikidata item (P5137 or a
  Wiktextract ``wikidata`` link) are linked. Each link cites the records it
  rests on;
* **search**: :meth:`CrossLanguageLinks.index` writes each current definition
  revision and lemma as a ``noesis-language-text-v1`` text (object types
  ``linguistics-definition`` and ``linguistics-lemma``). ``multilingual_search``
  then surfaces lexeme senses with its own index and fairness ranking. The
  text keeps its licence in its metadata;
* **aliases**: when a lexeme's sense states an item that names a canonical
  entity, :meth:`CrossLanguageLinks.propose_alias` records the lemma as a
  *candidate* ``noesis-multilingual-alias-v1`` through ``record_alias``. Only
  ``review_multilingual_alias`` can accept it;
* **Noesis translations** of a definition
  (:meth:`CrossLanguageLinks.record_translation`) are
  ``noesis-translation-record-v1`` records with their producer and an
  ``unreviewed`` status. They are always labelled as such and never appear as
  a sourced definition.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.linguistics_identity import LinguisticsIdentity
from src.kb.linguistics_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    LinguisticsError,
    authorize,
    citation,
    licence_block,
    require_scope,
    script_of,
)

CL_READ, CL_WRITE = "knowledge:cross-language:read", "knowledge:cross-language:write"
DEFINITION_TEXT, LEMMA_TEXT = "linguistics-definition", "linguistics-lemma"


class CrossLanguageLinks:
    def __init__(self, conn: Any, *, initialize: bool = False, now=None) -> None:
        self.conn = conn
        self.identity = LinguisticsIdentity(conn, initialize=initialize, now=now)
        self.store = self.identity.store
        self.now = self.store.now

    def _cross(self, initialize: bool = True):
        from src.kb.cross_language import CrossLanguageStore

        return CrossLanguageStore(self.conn, initialize=initialize, now=self.now)

    # ------------------------------------------------------------ sourced links

    def links(
        self, namespace: str, key: str, *, scopes: Iterable[str], as_of: Any = None
    ) -> dict[str, Any]:
        """Source-stated cross-language links of a lexeme (all its senses) or of one sense."""
        from src.kb.linguistics_etymology import Etymologies

        authorize(namespace, set(scopes), READ_SCOPE)
        self.store.require_ready()
        senses = self.store.currents(namespace, kind="sense", as_of=as_of)
        lexeme_keys = set(self.identity.equivalents(namespace, key, kind="lexeme"))
        if key.startswith("sense:"):
            ours = {key}
            owner = next(
                (s["body"]["lexeme"] for s in senses if s["record_key"] == key), None
            )
            lexeme_keys = (
                set(self.identity.equivalents(namespace, owner, kind="lexeme"))
                if owner
                else set()
            )
        else:
            ours = {
                s["record_key"] for s in senses if s["body"]["lexeme"] in lexeme_keys
            }
        etymologies = Etymologies(self.conn, now=self.now)
        links, cited = [], []
        for translation in self.store.currents(
            namespace, kind="sourced_translation", as_of=as_of
        ):
            body = translation["body"]
            target = body["target"]
            found = etymologies.lexemes_named(
                namespace, target["language"], target["text"], as_of=as_of
            )
            outgoing = body["sense"] in ours or body["sense"] in lexeme_keys
            incoming = bool({f["record_key"] for f in found} & lexeme_keys)
            if not (outgoing or incoming):
                continue
            cited.append(translation)
            links.append(
                {
                    "kind": "sourced-translation",
                    "direction": "outgoing" if outgoing else "incoming",
                    "from": body["sense"],
                    "resolved_to": body["resolved_to"],
                    "target": target,
                    "target_lexemes": sorted(f["record_key"] for f in found),
                    "sense_label": body.get("sense_label"),
                    "citation": citation(translation),
                }
            )
        items: dict[str, list[dict[str, Any]]] = {}
        for sense in senses:
            for link in sense["body"].get("item_links") or []:
                items.setdefault(link["value"], []).append(sense)
        for item, holders in sorted(items.items()):
            mine = [s for s in holders if s["record_key"] in ours]
            others = [s for s in holders if s["body"]["lexeme"] not in lexeme_keys]
            if not mine or not others:
                continue
            cited += mine + others
            links.append(
                {
                    "kind": "shared-sense-item",
                    "item": item,
                    "senses": sorted(s["record_key"] for s in mine),
                    "linked_senses": sorted(s["record_key"] for s in others),
                    "linked_lexemes": sorted({s["body"]["lexeme"] for s in others}),
                    "citations": [citation(s) for s in mine + others],
                    "note": "both sources state this item for the sense; a source-stated link, not an identity",
                }
            )
        return {
            "key": key,
            "lexemes": sorted(lexeme_keys),
            "links": links,
            "n": len(links),
            "licences": licence_block(cited),
        }

    # ------------------------------------------------------------ search through cross_language

    def index(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Write current definition revisions and lemmas as language texts (idempotent; one per revision)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, CL_WRITE)
        self.store.require_ready()
        cross = self._cross()
        written = 0
        for kind, object_type in (
            ("definition_revision", DEFINITION_TEXT),
            ("lexeme", LEMMA_TEXT),
        ):
            for record in self.store.currents(namespace, kind=kind):
                body = record["body"]
                if kind == "lexeme":
                    if body.get("status") == "deleted" or not body.get("lemma"):
                        continue
                    text, language = body["lemma"]["text"], body["lemma"]["language"]
                else:
                    text, language = body["text"], body["language"]
                result = cross.record_text(
                    namespace,
                    object_type,
                    record["revision_id"],
                    text,
                    language=language,
                    script=script_of(text)["script"],
                    metadata={
                        "record_key": record["record_key"],
                        "provider": record["provider"],
                        "source_revision": record["source_revision"],
                        "licence": record["source"]["licence"],
                        "origin": "source-published",
                        **(
                            {"sense": body["sense"]}
                            if kind == "definition_revision"
                            else {}
                        ),
                    },
                    principal_id=principal_id,
                    scopes={CL_WRITE},
                )
                written += 0 if result["idempotent"] else 1
        return {"written": written, "object_types": [DEFINITION_TEXT, LEMMA_TEXT]}

    def search(
        self,
        namespace: str,
        query: str,
        *,
        scopes: Iterable[str],
        languages=(),
        limit: int = 20,
    ) -> dict[str, Any]:
        """``multilingual_search`` over the indexed texts; hits are mapped back to their records."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, CL_READ)
        result = self._cross(initialize=False).search(
            namespace, query, languages=languages, limit=limit, scopes={CL_READ}
        )
        hits = []
        for hit in result["results"]:
            if hit.get("object_type") in {DEFINITION_TEXT, LEMMA_TEXT}:
                text = self._cross(initialize=False).get_text(
                    namespace, hit["text_id"], scopes={CL_READ}
                )
                hits.append({**hit, "linguistics": text["metadata"]})
            elif hit["kind"] == "translation":
                hits.append(
                    {**hit, "label": "Noesis translation (not a sourced definition)"}
                )
            else:
                hits.append(hit)
        return {**result, "results": hits}

    # ------------------------------------------------------------ aliases and translations

    def propose_alias(
        self,
        namespace: str,
        lexeme_key: str,
        entity_id: str,
        item: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Record a lexeme's lemma as a *candidate* alias of a canonical entity whose item a sense states."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, CL_WRITE)
        self.store.require_ready()
        lexeme = self.store.current(namespace, lexeme_key)
        if lexeme is None or lexeme["body"].get("status") == "deleted":
            raise LinguisticsError("not_found", "the lexeme is not on record")
        stating = [
            s
            for s in self.store.currents(namespace, kind="sense")
            if s["body"]["lexeme"] == lexeme_key
            and any(link["value"] == item for link in s["body"].get("item_links") or [])
        ]
        if not stating:
            raise LinguisticsError(
                "insufficient_evidence",
                "no sense of this lexeme states the item; an alias needs a source-stated link",
            )
        lemma = lexeme["body"]["lemma"]
        return self._cross().record_alias(
            namespace,
            entity_id,
            lemma["text"],
            lemma["language"],
            lemma["script"],
            confidence=0.5,
            evidence=[
                {
                    "lexeme": citation(lexeme),
                    "senses": [citation(s) for s in stating],
                    "item": item,
                    "basis": "sense states the item (source-stated link)",
                }
            ],
            status="candidate",
            principal_id=principal_id,
            scopes={CL_WRITE},
        )

    def record_translation(
        self,
        namespace: str,
        definition_key: str,
        target_language: str,
        translated_text: str,
        producer: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        version: int = 1,
    ) -> dict[str, Any]:
        """A Noesis translation of a sourced definition: a translation record, unreviewed, with its producer."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, CL_WRITE)
        if not producer.get("name") or not producer.get("kind"):
            raise LinguisticsError(
                "invalid_producer", "a translation names its producer and producer kind"
            )
        definition = self.store.current(namespace, definition_key)
        if definition is None or definition["kind"] != "definition_revision":
            raise LinguisticsError("not_found", "the definition is not on record")
        self.index(namespace, principal_id=principal_id, scopes=scopes)
        row = self.conn.execute(
            "SELECT text_id FROM language_texts WHERE namespace=? AND object_type=? AND object_id=?",
            [namespace, DEFINITION_TEXT, definition["revision_id"]],
        ).fetchone()
        record = self._cross().record_translation(
            namespace,
            row[0],
            target_language,
            translated_text,
            dict(producer),
            version=version,
            status="unreviewed",
            principal_id=principal_id,
            scopes={CL_WRITE},
        )
        return {
            **record,
            "label": noesis_label(record),
            "source_definition": citation(definition),
        }

    def translations(
        self, namespace: str, definition: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Noesis translations of one definition revision, each labelled with producer and review status."""
        from src.kb.linguistics_store import table_exists

        if not table_exists(self.conn, "translation_records"):
            return []
        rows = self.conn.execute(
            "SELECT t.translation_id FROM translation_records t JOIN language_texts l ON l.text_id=t.source_text_id "
            "WHERE t.namespace=? AND l.object_type=? AND l.object_id=? ORDER BY t.translation_id",
            [namespace, DEFINITION_TEXT, definition["revision_id"]],
        ).fetchall()
        cross = self._cross(initialize=False)
        out = []
        for (translation_id,) in rows:
            record = cross._translation(namespace, translation_id, scopes={CL_READ})
            out.append(
                {
                    "translation_id": translation_id,
                    "contract": record["contract"],
                    "target_language": record["target_language"],
                    "text": record["translated_text"],
                    "producer": record["producer"],
                    "status": record["status"],
                    "label": noesis_label(record),
                }
            )
        return out


def noesis_label(record: Mapping[str, Any]) -> str:
    producer = record.get("producer") or {}
    return (
        f"Noesis translation by {producer.get('name')} ({producer.get('kind')}), status {record.get('status')}; "
        "not a sourced definition"
    )
