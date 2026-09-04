"""Cross-language evidence records that never replace original-language content."""

from __future__ import annotations

import hashlib
import json
import time
import unicodedata

TEXT_CONTRACT = "noesis-language-text-v1"
ALIAS_CONTRACT = "noesis-multilingual-alias-v1"
ALIGNMENT_CONTRACT = "noesis-cross-language-claim-alignment-v1"
TRANSLATION_CONTRACT = "noesis-translation-record-v1"
SEARCH_CONTRACT = "noesis-multilingual-search-v1"
READ_SCOPE = "knowledge:cross-language:read"
WRITE_SCOPE = "knowledge:cross-language:write"
REVIEW_SCOPE = "knowledge:cross-language:review"

_DDL = """
CREATE TABLE IF NOT EXISTS language_texts(text_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,revision BIGINT NOT NULL,language TEXT NOT NULL,script TEXT NOT NULL,locale TEXT,direction TEXT NOT NULL,original_text TEXT NOT NULL,normalized_text TEXT NOT NULL,code_switches_json TEXT NOT NULL,metadata_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,object_type,object_id,revision));
CREATE TABLE IF NOT EXISTS multilingual_aliases(alias_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,entity_id TEXT NOT NULL,alias_text TEXT NOT NULL,language TEXT NOT NULL,script TEXT NOT NULL,transliteration_system TEXT,status TEXT NOT NULL,confidence DOUBLE NOT NULL,evidence_json TEXT NOT NULL,alternatives_json TEXT NOT NULL,review_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,entity_id,alias_text,language,script,transliteration_system));
CREATE TABLE IF NOT EXISTS cross_language_alignments(alignment_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,source_claim_id TEXT NOT NULL,target_claim_id TEXT NOT NULL,relation TEXT NOT NULL,confidence DOUBLE NOT NULL,source_text_id TEXT NOT NULL,target_text_id TEXT NOT NULL,evidence_json TEXT NOT NULL,analysis_json TEXT NOT NULL,status TEXT NOT NULL,review_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,source_claim_id,target_claim_id,relation));
CREATE TABLE IF NOT EXISTS translation_records(translation_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,source_text_id TEXT NOT NULL,target_language TEXT NOT NULL,version BIGINT NOT NULL,translated_text TEXT NOT NULL,passage_json TEXT NOT NULL,producer_json TEXT NOT NULL,confidence DOUBLE NOT NULL,alternatives_json TEXT NOT NULL,status TEXT NOT NULL,review_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,source_text_id,target_language,version));
CREATE TABLE IF NOT EXISTS cross_language_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class CrossLanguageError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _load(value, default):
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes, required):
    if required not in scopes and "operator" not in scopes:
        raise CrossLanguageError("unauthorized", f"missing required scope {required}")


def _bound(value, maximum=500):
    return min(max(int(value), 1), maximum)


class CrossLanguageStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        self.conn.execute(
            "INSERT OR IGNORE INTO cross_language_audit VALUES (?,?,?,?,?,?,?)",
            [
                "language-audit:"
                + _hash([namespace, operation, object_id, detail])[:24],
                namespace,
                operation,
                object_id,
                principal_id,
                _canon(detail),
                now,
            ],
        )

    def record_text(
        self,
        namespace,
        object_type,
        object_id,
        original_text,
        *,
        language="und",
        script="Zyyy",
        locale=None,
        direction="auto",
        code_switches=(),
        metadata=None,
        revision=1,
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        if not original_text:
            raise CrossLanguageError("empty_original_text", "original text is required")
        if direction not in {"ltr", "rtl", "auto"}:
            raise CrossLanguageError(
                "invalid_direction", "direction must be ltr, rtl, or auto"
            )
        normalized = unicodedata.normalize("NFC", original_text)
        existing = self.conn.execute(
            "SELECT text_id,original_text,normalized_text,language,script,locale,direction,code_switches_json,metadata_json FROM language_texts WHERE namespace=? AND object_type=? AND object_id=? AND revision=?",
            [namespace, object_type, object_id, revision],
        ).fetchone()
        if existing:
            if existing[1] != original_text:
                raise CrossLanguageError(
                    "immutable_original",
                    "a revision's original text cannot be overwritten",
                )
            return self._text_result(
                existing, namespace, object_type, object_id, revision, True
            )
        text_id = (
            "language-text:"
            + _hash([namespace, object_type, object_id, revision, original_text])[:24]
        )
        now = self.now()
        switches = list(code_switches)
        meta = dict(metadata or {})
        self.conn.execute(
            "INSERT INTO language_texts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                text_id,
                namespace,
                object_type,
                object_id,
                revision,
                language or "und",
                script or "Zyyy",
                locale,
                direction,
                original_text,
                normalized,
                _canon(switches),
                _canon(meta),
                now,
            ],
        )
        self._audit(
            namespace, "record_text", text_id, principal_id, {"revision": revision}, now
        )
        return {
            "contract": TEXT_CONTRACT,
            "text_id": text_id,
            "namespace": namespace,
            "object_type": object_type,
            "object_id": object_id,
            "revision": revision,
            "language": language or "und",
            "script": script or "Zyyy",
            "locale": locale,
            "direction": direction,
            "original_text": original_text,
            "normalized_text": normalized,
            "code_switches": switches,
            "metadata": meta,
            "idempotent": False,
        }

    def _text_result(
        self, row, namespace, object_type, object_id, revision, idem=False
    ):
        return {
            "contract": TEXT_CONTRACT,
            "text_id": row[0],
            "namespace": namespace,
            "object_type": object_type,
            "object_id": object_id,
            "revision": int(revision),
            "language": row[3],
            "script": row[4],
            "locale": row[5],
            "direction": row[6],
            "original_text": row[1],
            "normalized_text": row[2],
            "code_switches": _load(row[7], []),
            "metadata": _load(row[8], {}),
            "idempotent": idem,
        }

    def get_text(self, namespace, text_id, *, scopes):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT object_type,object_id,revision,original_text,normalized_text,language,script,locale,direction,code_switches_json,metadata_json FROM language_texts WHERE namespace=? AND text_id=?",
            [namespace, text_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError("text_not_found", f"text {text_id} not found")
        shaped = (
            text_id,
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
        )
        return self._text_result(shaped, namespace, row[0], row[1], row[2])

    def record_alias(
        self,
        namespace,
        entity_id,
        alias_text,
        language,
        script,
        *,
        transliteration_system=None,
        confidence=0.0,
        evidence=(),
        alternatives=(),
        status="candidate",
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        if status not in {"candidate", "accepted", "rejected", "ambiguous"}:
            raise CrossLanguageError("invalid_alias_status", "unsupported alias status")
        confidence = min(max(float(confidence), 0.0), 1.0)
        system = transliteration_system or ""
        alias_id = (
            "multilingual-alias:"
            + _hash([namespace, entity_id, alias_text, language, script, system])[:24]
        )
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO multilingual_aliases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                alias_id,
                namespace,
                entity_id,
                alias_text,
                language,
                script,
                system,
                status,
                confidence,
                _canon(list(evidence)),
                _canon(list(alternatives)),
                "{}",
                now,
            ],
        )
        self._audit(
            namespace, "record_alias", alias_id, principal_id, {"status": status}, now
        )
        return self._alias(namespace, alias_id, scopes={READ_SCOPE})

    def review_alias(
        self,
        namespace,
        alias_id,
        decision,
        reviewer_id,
        *,
        rationale="",
        principal_id,
        scopes,
    ):
        _require(scopes, REVIEW_SCOPE)
        if decision not in {"accepted", "rejected", "ambiguous"}:
            raise CrossLanguageError(
                "invalid_alias_decision", "unsupported alias decision"
            )
        row = self.conn.execute(
            "SELECT review_json FROM multilingual_aliases WHERE namespace=? AND alias_id=?",
            [namespace, alias_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError("alias_not_found", f"alias {alias_id} not found")
        history = _load(row[0], {}).get("history", [])
        event = {
            "decision": decision,
            "reviewer_id": reviewer_id,
            "rationale": rationale,
            "observed_at_ms": self.now(),
        }
        if not history or history[-1] != event:
            history.append(event)
        self.conn.execute(
            "UPDATE multilingual_aliases SET status=?,review_json=? WHERE namespace=? AND alias_id=?",
            [decision, _canon({"history": history}), namespace, alias_id],
        )
        self._audit(
            namespace, "review_alias", alias_id, principal_id, event, self.now()
        )
        return self._alias(namespace, alias_id, scopes={READ_SCOPE})

    def _alias(self, namespace, alias_id, *, scopes):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT entity_id,alias_text,language,script,transliteration_system,status,confidence,evidence_json,alternatives_json,review_json FROM multilingual_aliases WHERE namespace=? AND alias_id=?",
            [namespace, alias_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError("alias_not_found", f"alias {alias_id} not found")
        return {
            "contract": ALIAS_CONTRACT,
            "alias_id": alias_id,
            "namespace": namespace,
            "entity_id": row[0],
            "alias_text": row[1],
            "language": row[2],
            "script": row[3],
            "transliteration_system": row[4] or None,
            "status": row[5],
            "confidence": row[6],
            "evidence": _load(row[7], []),
            "alternatives": _load(row[8], []),
            "review": _load(row[9], {}),
        }

    def align_claims(
        self,
        namespace,
        source_claim_id,
        target_claim_id,
        relation,
        source_text_id,
        target_text_id,
        *,
        confidence=0.0,
        evidence=(),
        analysis=None,
        status="candidate",
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        if relation not in {
            "translated",
            "equivalent",
            "narrower",
            "broader",
            "divergent",
        }:
            raise CrossLanguageError(
                "invalid_relation", "unsupported alignment relation"
            )
        source = self.get_text(namespace, source_text_id, scopes={READ_SCOPE})
        target = self.get_text(namespace, target_text_id, scopes={READ_SCOPE})
        alignment_id = (
            "claim-alignment:"
            + _hash([namespace, source_claim_id, target_claim_id, relation])[:24]
        )
        analysis = dict(analysis or {})
        for flag in ("negation", "modality", "idiom", "numeric_format"):
            analysis.setdefault(flag, "not_assessed")
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO cross_language_alignments VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                alignment_id,
                namespace,
                source_claim_id,
                target_claim_id,
                relation,
                min(max(float(confidence), 0.0), 1.0),
                source_text_id,
                target_text_id,
                _canon(list(evidence)),
                _canon(analysis),
                status,
                "{}",
                now,
            ],
        )
        self._audit(
            namespace,
            "align_claims",
            alignment_id,
            principal_id,
            {"relation": relation},
            now,
        )
        return self._alignment(
            namespace, alignment_id, scopes={READ_SCOPE}, source=source, target=target
        )

    def review_alignment(
        self,
        namespace,
        alignment_id,
        decision,
        reviewer_id,
        *,
        rationale="",
        principal_id,
        scopes,
    ):
        _require(scopes, REVIEW_SCOPE)
        if decision not in {"accepted", "rejected", "ambiguous"}:
            raise CrossLanguageError(
                "invalid_alignment_decision", "unsupported alignment decision"
            )
        event = {
            "decision": decision,
            "reviewer_id": reviewer_id,
            "rationale": rationale,
            "observed_at_ms": self.now(),
        }
        row = self.conn.execute(
            "SELECT review_json FROM cross_language_alignments WHERE namespace=? AND alignment_id=?",
            [namespace, alignment_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError(
                "alignment_not_found", f"alignment {alignment_id} not found"
            )
        history = _load(row[0], {}).get("history", []) + [event]
        self.conn.execute(
            "UPDATE cross_language_alignments SET status=?,review_json=? WHERE namespace=? AND alignment_id=?",
            [decision, _canon({"history": history}), namespace, alignment_id],
        )
        self._audit(
            namespace, "review_alignment", alignment_id, principal_id, event, self.now()
        )
        return self._alignment(namespace, alignment_id, scopes={READ_SCOPE})

    def _alignment(self, namespace, alignment_id, *, scopes, source=None, target=None):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT source_claim_id,target_claim_id,relation,confidence,source_text_id,target_text_id,evidence_json,analysis_json,status,review_json FROM cross_language_alignments WHERE namespace=? AND alignment_id=?",
            [namespace, alignment_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError(
                "alignment_not_found", f"alignment {alignment_id} not found"
            )
        source = source or self.get_text(namespace, row[4], scopes={READ_SCOPE})
        target = target or self.get_text(namespace, row[5], scopes={READ_SCOPE})
        return {
            "contract": ALIGNMENT_CONTRACT,
            "alignment_id": alignment_id,
            "namespace": namespace,
            "source_claim_id": row[0],
            "target_claim_id": row[1],
            "relation": row[2],
            "confidence": row[3],
            "source_text": source,
            "target_text": target,
            "evidence": _load(row[6], []),
            "analysis": _load(row[7], {}),
            "status": row[8],
            "review": _load(row[9], {}),
        }

    def record_translation(
        self,
        namespace,
        source_text_id,
        target_language,
        translated_text,
        producer,
        *,
        version=1,
        passage=None,
        confidence=0.0,
        alternatives=(),
        status="unreviewed",
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        self.get_text(namespace, source_text_id, scopes={READ_SCOPE})
        translation_id = (
            "translation:"
            + _hash(
                [
                    namespace,
                    source_text_id,
                    target_language,
                    version,
                    translated_text,
                    producer,
                ]
            )[:24]
        )
        existing = self.conn.execute(
            "SELECT translation_id,translated_text FROM translation_records WHERE namespace=? AND source_text_id=? AND target_language=? AND version=?",
            [namespace, source_text_id, target_language, version],
        ).fetchone()
        if existing:
            if existing[1] != translated_text:
                raise CrossLanguageError(
                    "translation_version_conflict", "translation version is immutable"
                )
            return self._translation(
                namespace, existing[0], scopes={READ_SCOPE}, idempotent=True
            )
        now = self.now()
        self.conn.execute(
            "INSERT INTO translation_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                translation_id,
                namespace,
                source_text_id,
                target_language,
                version,
                translated_text,
                _canon(dict(passage or {})),
                _canon(dict(producer)),
                min(max(float(confidence), 0.0), 1.0),
                _canon(list(alternatives)),
                status,
                "{}",
                now,
            ],
        )
        self._audit(
            namespace,
            "record_translation",
            translation_id,
            principal_id,
            {"version": version},
            now,
        )
        return self._translation(namespace, translation_id, scopes={READ_SCOPE})

    def review_translation(
        self,
        namespace,
        translation_id,
        decision,
        reviewer_id,
        *,
        rationale="",
        principal_id,
        scopes,
    ):
        _require(scopes, REVIEW_SCOPE)
        if decision not in {"accepted", "disputed", "rejected"}:
            raise CrossLanguageError(
                "invalid_translation_decision", "unsupported translation decision"
            )
        row = self.conn.execute(
            "SELECT review_json FROM translation_records WHERE namespace=? AND translation_id=?",
            [namespace, translation_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError(
                "translation_not_found", f"translation {translation_id} not found"
            )
        event = {
            "decision": decision,
            "reviewer_id": reviewer_id,
            "rationale": rationale,
            "observed_at_ms": self.now(),
        }
        history = _load(row[0], {}).get("history", []) + [event]
        self.conn.execute(
            "UPDATE translation_records SET status=?,review_json=? WHERE namespace=? AND translation_id=?",
            [decision, _canon({"history": history}), namespace, translation_id],
        )
        self._audit(
            namespace,
            "review_translation",
            translation_id,
            principal_id,
            event,
            self.now(),
        )
        return self._translation(namespace, translation_id, scopes={READ_SCOPE})

    def _translation(self, namespace, translation_id, *, scopes, idempotent=False):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT source_text_id,target_language,version,translated_text,passage_json,producer_json,confidence,alternatives_json,status,review_json FROM translation_records WHERE namespace=? AND translation_id=?",
            [namespace, translation_id],
        ).fetchone()
        if not row:
            raise CrossLanguageError(
                "translation_not_found", f"translation {translation_id} not found"
            )
        source = self.get_text(namespace, row[0], scopes={READ_SCOPE})
        return {
            "contract": TRANSLATION_CONTRACT,
            "translation_id": translation_id,
            "namespace": namespace,
            "source_text_id": row[0],
            "source_original_text": source["original_text"],
            "target_language": row[1],
            "version": int(row[2]),
            "translated_text": row[3],
            "passage": _load(row[4], {}),
            "producer": _load(row[5], {}),
            "confidence": row[6],
            "alternatives": _load(row[7], []),
            "status": row[8],
            "review": _load(row[9], {}),
            "idempotent": idempotent,
        }

    def compare_claims(self, namespace, alignment_id, *, scopes):
        return self._alignment(namespace, alignment_id, scopes=scopes)

    def search(
        self,
        namespace,
        query,
        *,
        languages=(),
        include_translations=True,
        limit=20,
        scopes,
    ):
        _require(scopes, READ_SCOPE)
        limit = _bound(limit, 100)
        needle = unicodedata.normalize("NFC", query).casefold()
        params = [namespace]
        language_sql = ""
        if languages:
            language_sql = " AND language IN (" + ",".join("?" for _ in languages) + ")"
            params.extend(languages)
        rows = self.conn.execute(
            "SELECT text_id,language,script,original_text,normalized_text,object_type,object_id FROM language_texts WHERE namespace=?"
            + language_sql,
            params,
        ).fetchall()
        hits = []
        for row in rows:
            value = row[4].casefold()
            if needle in value:
                hits.append(
                    {
                        "kind": "original",
                        "text_id": row[0],
                        "language": row[1],
                        "script": row[2],
                        "original_text": row[3],
                        "object_type": row[5],
                        "object_id": row[6],
                        "score": 1.0 if value == needle else 0.8,
                    }
                )
        alias_rows = self.conn.execute(
            "SELECT alias_id,entity_id,alias_text,language,script,status,confidence FROM multilingual_aliases WHERE namespace=?",
            [namespace],
        ).fetchall()
        for row in alias_rows:
            if (
                not languages or row[3] in languages
            ) and needle in unicodedata.normalize("NFC", row[2]).casefold():
                hits.append(
                    {
                        "kind": "alias",
                        "alias_id": row[0],
                        "entity_id": row[1],
                        "language": row[3],
                        "script": row[4],
                        "original_text": row[2],
                        "status": row[5],
                        "score": float(row[6]),
                    }
                )
        if include_translations:
            for row in self.conn.execute(
                "SELECT translation_id,source_text_id,target_language,translated_text,confidence,status FROM translation_records WHERE namespace=?",
                [namespace],
            ).fetchall():
                if (
                    not languages or row[2] in languages
                ) and needle in unicodedata.normalize("NFC", row[3]).casefold():
                    original = self.get_text(namespace, row[1], scopes={READ_SCOPE})
                    hits.append(
                        {
                            "kind": "translation",
                            "translation_id": row[0],
                            "text_id": row[1],
                            "language": row[2],
                            "original_language": original["language"],
                            "original_text": original["original_text"],
                            "translated_text": row[3],
                            "status": row[5],
                            "score": float(row[4]),
                        }
                    )
        hits.sort(
            key=lambda x: (
                -x["score"],
                x["language"],
                x.get("text_id", x.get("alias_id", "")),
            )
        )
        # Round-robin languages prevents a high-volume language from crowding out others.
        buckets = {}
        for hit in hits:
            buckets.setdefault(hit["language"], []).append(hit)
        fair = []
        while buckets and len(fair) < limit:
            for language in sorted(buckets):
                fair.append(buckets[language].pop(0))
                if not buckets[language]:
                    del buckets[language]
                if len(fair) == limit:
                    break
        return {
            "contract": SEARCH_CONTRACT,
            "namespace": namespace,
            "query": query,
            "languages": list(languages),
            "results": fair,
            "total_candidates": len(hits),
            "ranking": {
                "method": "score-then-language-round-robin",
                "translation_results_preserve_original": True,
            },
        }
