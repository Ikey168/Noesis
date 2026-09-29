"""Linguistics acquisition (LG03-LG05, #2181-#2183): Wikidata lexemes, Wiktextract, Glottolog, WALS, CLDR, ISO 639-3.

One native connector, ``linguistics``, fetches the declared documents of a
source one page at a time and parses them into ``noesis-linguistic-record-v1``
records (:mod:`src.kb.linguistics_records`), which
:class:`src.kb.linguistics_store.LinguisticsProjector` stores as sightings.
Six documented formats are parsed:

* ``wikidata-lexeme-json``: ``Special:EntityData`` JSON for declared L-ids
  (through :func:`src.ingestion.wikidata.normalize_lexeme`) and for declared
  language items (through :func:`src.ingestion.wikidata.normalize_entity`, P1394
  Glottocode and P220 ISO 639-3). The revision is ``lastrevid`` and the date is
  ``modified``. A missing lexeme is recorded as deleted. Etymology statements
  keep their references and are flagged ``unreferenced`` without any;
* ``kaikki-jsonl``: kaikki.org Wiktextract JSONL, one entry per line, bounded
  to declared words. The release is ``<extract date>/<dump date>``. Only
  structured etymology templates become assertions; ``etymology_text`` is kept
  verbatim as a cited note. ``sounds`` (speaker recordings) are dropped and
  counted. Every record carries CC BY-SA attribution and ``share_alike``;
* ``glottolog-cldf``: ``languages.csv`` plus the ``level`` and
  ``classification`` values of a pinned release, bounded to declared subtrees.
  The ``aes`` (endangerment) parameter is never read;
* ``wals-cldf``: languages (WALS code with its Glottocode as published),
  parameters with codes, and values with their source references, bounded to
  declared features and Glottocodes;
* ``cldr-json``: language and script display names and cardinal plural rules
  of a pinned CLDR release, bounded to declared locales and subjects;
* ``iso639-3-tab``: the SIL code, macrolanguage and retirement tables, bounded
  to declared codes.

Rows and entries never inherit values from the one before them. A row that
cannot be parsed or validated becomes a ``rejection`` record, which the runtime
quarantines. Every other skipped row is counted in the page receipt
(``out_of_scope``) and never dropped silently.

``PROVIDER_CONTRACTS`` restates the LG01 decisions
(``docs/development/linguistics-evidence/source-audit.md``); every implemented
source is ``unverified-live`` until a dated live run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.linguistics_records import (
    CONTRACT,
    ETYMOLOGY_RELATIONS,
    GLOTTOCODE,
    ISO639_3,
    ISO_RETIREMENT_REASONS,
    ISO_SCOPES,
    WALS_FEATURE,
    LinguisticsError,
    comparable,
    digest,
    iso_date,
    language_tag,
    nfc,
    record_key,
    representation,
    script_of,
    validate_gloss,
    validate_record,
)

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
CONNECTOR = "linguistics"
FORMATS = {
    "wikidata-lexeme-json": "wikidata-lexemes",
    "kaikki-jsonl": "kaikki-wiktextract",
    "glottolog-cldf": "glottolog",
    "wals-cldf": "wals",
    "cldr-json": "cldr",
    "iso639-3-tab": "sil-iso639-3",
}
DOCUMENT_URLS = {
    "glottolog-cldf": ("languages_url", "values_url"),
    "wals-cldf": ("languages_url", "parameters_url", "codes_url", "values_url"),
}
MAX_LINES = 200_000
LICENCES = {
    "wikidata-lexemes": {
        "id": "CC0-1.0",
        "share_alike": False,
        "url": "https://creativecommons.org/publicdomain/zero/1.0/",
    },
    "wikidata-items": {
        "id": "CC0-1.0",
        "share_alike": False,
        "url": "https://creativecommons.org/publicdomain/zero/1.0/",
    },
    "kaikki-wiktextract": {
        "id": "CC-BY-SA-4.0 OR GFDL-1.3",
        "share_alike": True,
        "url": "https://creativecommons.org/licenses/by-sa/4.0/",
    },
    "glottolog": {
        "id": "CC-BY-4.0",
        "share_alike": False,
        "url": "https://creativecommons.org/licenses/by/4.0/",
    },
    "wals": {
        "id": "CC-BY-4.0",
        "share_alike": False,
        "url": "https://creativecommons.org/licenses/by/4.0/",
    },
    "cldr": {
        "id": "Unicode-3.0",
        "share_alike": False,
        "url": "https://www.unicode.org/license.txt",
    },
    "sil-iso639-3": {
        "id": "SIL-ISO-639-3-terms",
        "share_alike": False,
        "url": "https://iso639-3.sil.org/code_tables/download_tables",
    },
}
# Wiktextract etymology template names -> normalised relation (the native name is always kept).
ETYMOLOGY_TEMPLATES = {
    "inh": "inherited",
    "inherited": "inherited",
    "inh+": "inherited",
    "bor": "borrowed",
    "borrowed": "borrowed",
    "lbor": "borrowed",
    "slbor": "borrowed",
    "bor+": "borrowed",
    "der": "derived",
    "derived": "derived",
    "der+": "derived",
    "cal": "calque",
    "calque": "calque",
    "cog": "cognate",
    "cognate": "cognate",
    "com": "compound-of",
    "compound": "compound-of",
    "af": "compound-of",
    "affix": "compound-of",
}
_SOURCE_LANG_TEMPLATES = {"inherited", "borrowed", "derived", "calque"}
# Wiktextract pseudo-forms (inflection-table metadata), never word forms.
_PSEUDO_FORM_TAGS = {"table-tags", "inflection-template", "class"}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "wikidata-lexemes": {
        "publisher": "Wikidata (Wikimedia Foundation), lexemes and language items",
        "endpoint": "https://www.wikidata.org/wiki/Special:EntityData/",
        "format": "wikidata-lexeme-json",
        "licence": "CC0-1.0",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser over the documented entity JSON; no dated live run yet",
    },
    "kaikki-wiktextract": {
        "publisher": "kaikki.org Wiktextract extracts of Wiktionary",
        "endpoint": "https://kaikki.org/dictionary/",
        "format": "kaikki-jsonl",
        "licence": "CC-BY-SA-4.0 OR GFDL-1.3 (share-alike)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser over the documented JSONL fields; extract path per language (verify)",
    },
    "glottolog": {
        "publisher": "Glottolog (MPI EVA), CLDF release",
        "endpoint": "https://github.com/glottolog/glottolog-cldf",
        "format": "glottolog-cldf",
        "licence": "CC-BY-4.0",
        "access_decision": "unverified-live",
        "reason": "fixture-verified CLDF parser; column names per release (verify)",
    },
    "wals": {
        "publisher": "WALS Online (MPI EVA), CLDF release",
        "endpoint": "https://github.com/cldf-datasets/wals",
        "format": "wals-cldf",
        "licence": "CC-BY-4.0",
        "access_decision": "unverified-live",
        "reason": "fixture-verified CLDF parser; no dated live run yet",
    },
    "cldr": {
        "publisher": "Unicode CLDR, cldr-json release",
        "endpoint": "https://github.com/unicode-org/cldr-json",
        "format": "cldr-json",
        "licence": "Unicode-3.0",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; no dated live run yet",
    },
    "sil-iso639-3": {
        "publisher": "SIL International, ISO 639-3 Registration Authority code tables",
        "endpoint": "https://iso639-3.sil.org/code_tables/download_tables",
        "format": "iso639-3-tab",
        "licence": "SIL terms of use for the code tables (verify)",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; terms of use to be confirmed before live use",
    },
    "leipzig-glossing-rules": {
        "publisher": "MPI EVA, The Leipzig Glossing Rules",
        "endpoint": "https://www.eva.mpg.de/lingua/resources/glossing-rules.php",
        "access_decision": "link-only",
        "reason": "a PDF; the standard abbreviation list is a bundled vocabulary (linguistics-leipzig-glossing)",
    },
    "ethnologue": {
        "publisher": "SIL International, Ethnologue",
        "access_decision": "not-implemented",
        "reason": "proprietary subscription; language status verdicts are a tracker non-goal",
    },
    "speaker-recordings": {
        "publisher": "Wikimedia Commons audio, Lingua Libre",
        "access_decision": "not-implemented",
        "reason": "recordings of identifiable speakers are excluded (no speaker personal data)",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live"
        if contract["access_decision"] == "unverified-live"
        else contract["access_decision"],
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}


class LinguisticsFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ helpers


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise LinguisticsFormatError("schema_drift", "document is not UTF-8") from exc


def _rows(
    raw: bytes, *, delimiter: str = ",", required: Sequence[str] = ()
) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(_text(raw)), delimiter=delimiter)
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [c for c in required if c not in header]
    if missing:
        raise LinguisticsFormatError(
            "schema_drift", f"table lacks the column(s) {', '.join(missing)}"
        )
    rows = []
    for index, row in enumerate(reader):
        if index >= MAX_LINES:
            raise LinguisticsFormatError("input_limit", "table exceeds the row limit")
        # each row stands alone: empty cells are absent, never carried over from the previous row
        rows.append(
            {
                str(k).strip(): v.strip()
                for k, v in row.items()
                if k is not None and v is not None and v.strip()
            }
        )
    return rows


def _licence(provider: str, attribution: str) -> dict[str, Any]:
    return {**LICENCES[provider], "attribution": attribution}


def _source(
    provider: str,
    source_id: str,
    revision: Any,
    attribution: str,
    *,
    revision_date: Any = None,
    url: str | None = None,
) -> dict[str, Any]:
    source = {
        "source_id": source_id,
        "revision": str(revision),
        "licence": _licence(provider, attribution),
    }
    if revision_date is not None:
        source["revision_date"] = iso_date(revision_date)
    if url:
        source["url"] = url
    return source


def _record(
    kind: str, provider: str, source: Mapping[str, Any], body: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "kind": kind,
        "provider": provider,
        "source": dict(source),
        "body": dict(body),
    }


class _Page:
    """Collects the records and rejections of one page; records are validated here, as the store will."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.counts: dict[str, int] = {"out_of_scope": 0}

    def add(self, record: dict[str, Any], locator: str) -> None:
        try:
            validate_record(record)
        except LinguisticsError as exc:
            self.rejected.append(
                {"locator": locator, "code": exc.code, "reason": str(exc)[:200]}
            )
            return
        self.records.append(record)

    def reject(self, locator: str, code: str, reason: str) -> None:
        self.rejected.append({"locator": locator, "code": code, "reason": reason[:200]})

    def count(self, name: str, amount: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + amount


# ------------------------------------------------------------------ Wikidata


def _references(statement: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Reference snaks as published: stated-in item, DOI, page, URL (other properties kept by id)."""
    from src.ingestion.wikidata import snak_value

    out = []
    for reference in statement.get("references") or []:
        snaks = {}
        for prop, values in sorted((reference.get("snaks") or {}).items()):
            found = [snak_value(v) for v in values if snak_value(v) is not None]
            if found:
                snaks[prop] = found
        if snaks:
            ref = {"snaks": snaks}
            if "P356" in snaks:
                ref["doi"] = str(snaks["P356"][0]).lower()
            if "P248" in snaks:
                ref["stated_in"] = str(snaks["P248"][0])
            out.append(ref)
    return out


def parse_wikidata(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any], *, status: int
) -> _Page:
    from src.ingestion.wikidata import normalize_entity, normalize_lexeme, snak_value

    page = _Page()
    entity_id = str(document["entity"])
    payload = json.loads(_text(raw)) if status == 200 and raw else {"entities": {}}
    if entity_id.startswith("Q"):
        provider = "wikidata-items"
        item = normalize_entity(
            payload, entity_id, properties=["P1394", "P220"], languages=["en"]
        )
        if item["status"] != "available":
            page.reject(entity_id, "unavailable", "language item is unavailable")
            return page
        body: dict[str, Any] = {"item": entity_id}
        if item["labels"].get("en"):
            body["label"] = item["labels"]["en"]
        for statement in item["statements"]:
            prop = statement["mainsnak"]["property"]
            value = snak_value(statement["mainsnak"])
            if value is None or statement["rank"] == "deprecated":
                continue
            field = "glottocodes" if prop == "P1394" else "iso639_3"
            body.setdefault(field, []).append(
                {
                    "value": str(value),
                    "statement_id": statement["id"],
                    "reference_status": statement["reference_status"],
                }
            )
        page.add(
            _record(
                "language_item",
                provider,
                _source(
                    provider,
                    entity_id,
                    item["revision"],
                    f"Wikidata, {entity_id} revision {item['revision']}, CC0",
                    revision_date=item.get("modified"),
                    url=f"https://www.wikidata.org/wiki/{entity_id}",
                ),
                body,
            ),
            entity_id,
        )
        return page
    provider = "wikidata-lexemes"
    lexeme = normalize_lexeme(payload, entity_id)
    url = f"https://www.wikidata.org/wiki/Lexeme:{entity_id}"
    if lexeme["status"] == "deleted":
        # A deleted lexeme is an observation, never a removal; its earlier revisions stay.
        page.add(
            _record(
                "lexeme",
                provider,
                _source(
                    provider,
                    entity_id,
                    "deleted",
                    f"Wikidata, {entity_id} (deleted), CC0",
                    url=url,
                ),
                {
                    "status": "deleted",
                    "lemma": {},
                    "language": {},
                    "lexical_category": {},
                },
            ),
            entity_id,
        )
        return page
    revision, modified = lexeme["revision"], lexeme.get("modified")
    attribution = f"Wikidata, {entity_id} revision {revision}, CC0"

    def src(source_id: str) -> dict[str, Any]:
        return _source(
            provider, source_id, revision, attribution, revision_date=modified, url=url
        )

    lemmas = [representation(text, lang) for lang, text in lexeme["lemmas"].items()]
    if not lemmas or not lexeme.get("language"):
        page.reject(
            entity_id, "invalid_entry", "a lexeme states a lemma and a language item"
        )
        return page
    lexeme_key = f"lexeme:{provider}:{entity_id}"
    page.add(
        _record(
            "lexeme",
            provider,
            src(entity_id),
            {
                "status": "active",
                "lemma": lemmas[0],
                "lemmas": lemmas,
                "language": {"scheme": "wikidata-item", "value": lexeme["language"]},
                "lexical_category": {
                    "scheme": "wikidata-item",
                    "value": lexeme["lexical_category"],
                },
            },
        ),
        entity_id,
    )
    for form in lexeme["forms"]:
        page.add(
            _record(
                "form",
                provider,
                src(form["id"]),
                {
                    "lexeme": lexeme_key,
                    "representations": [
                        representation(t, lang)
                        for lang, t in form["representations"].items()
                    ],
                    "grammatical_features": [
                        {"scheme": "wikidata-item", "value": q}
                        for q in form["grammatical_features"]
                    ],
                },
            ),
            form["id"],
        )
    for sense in lexeme["senses"]:
        body = {"lexeme": lexeme_key, "identity_basis": "source-id"}
        links = [
            snak_value(s["mainsnak"])
            for s in sense["statements"]
            if s["rank"] != "deprecated"
        ]
        links = [str(v) for v in links if v]
        if links:
            body["item_links"] = [
                {"scheme": "wikidata-item", "value": q, "stated_by": "P5137"}
                for q in links
            ]
        page.add(_record("sense", provider, src(sense["id"]), body), sense["id"])
        for lang, gloss in sense["glosses"].items():
            page.add(
                _record(
                    "definition_revision",
                    provider,
                    src(f"{sense['id']}:{language_tag(lang)}"),
                    {
                        "sense": f"sense:{provider}:{sense['id']}",
                        "language": language_tag(lang),
                        "text": gloss,
                        "script": script_of(gloss)["script"],
                    },
                ),
                f"{sense['id']}:{lang}",
            )
    modes = {
        str(k): str(v) for k, v in dict(declared.get("derivation_modes") or {}).items()
    }
    for statement in lexeme["statements"]:
        prop = statement["mainsnak"]["property"]
        target = snak_value(statement["mainsnak"])
        mode_items = [
            snak_value(q) for q in (statement.get("qualifiers") or {}).get("P5886", [])
        ]
        mode_items = [str(m) for m in mode_items if m]
        relation = (
            "compound-of"
            if prop == "P5238"
            else next(
                (modes[m] for m in mode_items if modes.get(m) in ETYMOLOGY_RELATIONS),
                "derived",
            )
        )
        body = {
            "lexeme": lexeme_key,
            "relation": relation,
            "native_relation": prop + "".join(f"/P5886:{m}" for m in mode_items),
            "target": {"source_lexeme_id": str(target), "provider": provider}
            if target
            else {"unknown": statement["mainsnak"]["snaktype"]},
            "rank": statement["rank"],
            "references": _references(statement),
            "reference_status": statement["reference_status"],
        }
        page.add(
            _record("etymology_assertion", provider, src(statement["id"]), body),
            statement["id"],
        )
    return page


# ------------------------------------------------------------------ Wiktextract (kaikki.org)


def _wiktionary_url(edition: str, word: str) -> str:
    return f"https://{edition}.wiktionary.org/wiki/{word.replace(' ', '_')}"


def parse_kaikki(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Page:
    page = _Page()
    provider = "kaikki-wiktextract"
    edition = str(declared.get("edition") or "en")
    lang_code = str(document["language_code"])
    words = {nfc(w) for w in (declared.get("words") or {}).get(lang_code, [])}
    extract, dump = iso_date(document["extract_date"]), iso_date(document["dump_date"])
    revision = f"{extract}/{dump}"
    for number, line in enumerate(_text(raw).splitlines(), start=1):
        if number > MAX_LINES:
            raise LinguisticsFormatError(
                "input_limit", "extract exceeds the line limit"
            )
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            page.reject(f"line {number}", "invalid_line", "line is not JSON")
            continue
        if not isinstance(entry, dict) or not entry.get("word") or not entry.get("pos"):
            page.reject(
                f"line {number}", "invalid_entry", "an entry states word and pos"
            )
            continue
        word = nfc(entry["word"])
        if entry.get("lang_code") != lang_code or word not in words:
            page.count("out_of_scope")
            continue
        if entry.get("sounds"):
            page.count("dropped_speaker_data", len(entry["sounds"]))
        _kaikki_entry(
            page,
            entry,
            word,
            lang_code,
            edition,
            revision,
            extract,
            dump,
            f"line {number}",
        )
    return page


def _kaikki_entry(
    page: _Page,
    entry: Mapping[str, Any],
    word: str,
    lang_code: str,
    edition: str,
    revision: str,
    extract: str,
    dump: str,
    locator: str,
) -> None:
    provider = "kaikki-wiktextract"
    etymology_number = int(entry.get("etymology_number") or 0)
    lexeme_id = f"{lang_code}:{word}:{entry['pos']}:{etymology_number}"
    lexeme_key = f"lexeme:{provider}:{lexeme_id}"
    url = _wiktionary_url(edition, word)
    attribution = (
        f"Wiktionary contributors, entry '{word}' ({entry.get('lang') or lang_code}), dump {dump}; "
        f"extracted by kaikki.org Wiktextract {extract}; CC BY-SA 4.0"
    )

    def src(source_id: str) -> dict[str, Any]:
        return _source(
            provider, source_id, revision, attribution, revision_date=extract, url=url
        )

    language = {"scheme": "wiktionary-code", "value": lang_code}
    if entry.get("lang"):
        language["name"] = str(entry["lang"])
    page.add(
        _record(
            "lexeme",
            provider,
            src(lexeme_id),
            {
                "status": "active",
                "lemma": representation(word, lang_code),
                "language": language,
                "lexical_category": {
                    "scheme": "wiktionary-pos",
                    "value": str(entry["pos"]),
                },
                "etymology_number": etymology_number,
            },
        ),
        locator,
    )
    for form in entry.get("forms") or []:
        text, tags = form.get("form"), sorted(str(t) for t in form.get("tags") or [])
        if not text or _PSEUDO_FORM_TAGS & set(tags):
            continue
        form_id = f"{lexeme_id}#form:{nfc(text)}|{'.'.join(tags)}"
        page.add(
            _record(
                "form",
                provider,
                src(form_id),
                {
                    "lexeme": lexeme_key,
                    "representations": [representation(text, lang_code)],
                    "grammatical_features": [
                        {"scheme": "wiktextract-tag", "value": t} for t in tags
                    ],
                },
            ),
            locator,
        )
    sense_glosses: dict[str, str] = {}
    for index, sense in enumerate(entry.get("senses") or []):
        if sense.get("id"):
            sid, basis = str(sense["id"]), "source-id"
        elif sense.get("senseid"):
            sid, basis = str((sense["senseid"] or [""])[0]), "senseid"
        else:
            sid, basis = f"#{index + 1}", "position"
        sense_id = f"{lexeme_id}#sense:{sid}"
        sense_key = f"sense:{provider}:{sense_id}"
        body: dict[str, Any] = {"lexeme": lexeme_key, "identity_basis": basis}
        if basis == "position":
            body["identity_note"] = (
                "no source sense id; position is unstable across releases"
            )
        if sense.get("tags"):
            body["tags"] = sorted(str(t) for t in sense["tags"])
        items = [
            str(q)
            for q in sense.get("wikidata") or []
            if re.fullmatch(r"Q[1-9]\d*", str(q))
        ]
        if items:
            body["item_links"] = [
                {"scheme": "wikidata-item", "value": q, "stated_by": "wiktionary"}
                for q in items
            ]
        page.add(_record("sense", provider, src(sense_id), body), locator)
        glosses = [str(g) for g in sense.get("glosses") or [] if str(g).strip()]
        if glosses:
            sense_glosses[comparable(glosses[-1]).casefold()] = sense_key
            page.add(
                _record(
                    "definition_revision",
                    provider,
                    src(f"{sense_id}:{edition}"),
                    {
                        "sense": sense_key,
                        "language": edition,
                        "text": glosses[-1],
                        **({"gloss_path": glosses[:-1]} if len(glosses) > 1 else {}),
                    },
                ),
                locator,
            )
        for example in sense.get("examples") or []:
            text = example.get("text")
            if not text:
                continue
            example_id = f"{sense_id}#example:{digest(comparable(text))[:12]}"
            example_body: dict[str, Any] = {
                "sense": sense_key,
                "text": text,
                "script": script_of(text)["script"],
            }
            published = example.get("english") or example.get("translation")
            if published:
                example_body["published_translation"] = {
                    "text": published,
                    "origin": "source-published",
                }
            if example.get("ref"):
                example_body["citation"] = str(example["ref"])
            page.add(
                _record("usage_example", provider, src(example_id), example_body),
                locator,
            )
            if example.get("gloss"):
                check = validate_gloss(str(example["gloss"]))
                page.add(
                    _record(
                        "gloss",
                        provider,
                        src(f"{example_id}#gloss"),
                        {
                            "example": f"usage_example:{provider}:{example_id}",
                            "line": str(example["gloss"]),
                            "categories": check["categories"],
                            "unknown_abbreviations": check["unknown"],
                            "validation": check["status"],
                            "vocabulary": check["vocabulary"],
                        },
                    ),
                    locator,
                )
    if entry.get("etymology_text"):
        page.add(
            _record(
                "etymology_note",
                provider,
                src(f"{lexeme_id}#etymology-text"),
                {
                    "lexeme": lexeme_key,
                    "text": str(entry["etymology_text"]),
                    "note": "verbatim etymology text; never parsed into links",
                },
            ),
            locator,
        )
    for template in entry.get("etymology_templates") or []:
        name = str(template.get("name") or "")
        relation = ETYMOLOGY_TEMPLATES.get(name)
        if relation is None:
            continue  # mentions, links and dates are not assertions
        args = {
            str(k): str(v)
            for k, v in dict(template.get("args") or {}).items()
            if str(v).strip()
        }
        if relation in _SOURCE_LANG_TEMPLATES:
            targets = [(args.get("2"), args.get("3"), 0)]
        elif relation == "cognate":
            targets = [(args.get("1"), args.get("2"), 0)]
        else:
            parts = sorted(
                (int(k), v) for k, v in args.items() if k.isdigit() and int(k) >= 2
            )
            targets = [
                (args.get("1"), value, position)
                for position, (_, value) in enumerate(parts, start=1)
            ]
        for target_lang, term, position in targets:
            if not target_lang:
                page.reject(locator, "invalid_template", f"{name} names no language")
                continue
            target: dict[str, Any] = {"language": language_tag(target_lang)}
            if term and term != "-":
                target["text"] = term
                target["nfc"] = nfc(term)
            if args.get("t") or args.get("gloss"):
                target["gloss"] = args.get("t") or args.get("gloss")
            assertion_id = f"{lexeme_id}#etymology:{name}:{target['language']}:{target.get('nfc', '')}:{position}"
            body = {
                "lexeme": lexeme_key,
                "relation": relation,
                "native_relation": name,
                "target": target,
                "references": [],
                "reference_status": "cited-to-entry",
            }
            if position:
                body["position"] = position
            if template.get("expansion"):
                body["expansion"] = str(template["expansion"])
            page.add(
                _record("etymology_assertion", provider, src(assertion_id), body),
                locator,
            )
    for translation in entry.get("translations") or []:
        code, text = translation.get("code"), translation.get("word")
        if not code or not text:
            continue
        label = str(translation.get("sense") or "")
        sense_key = sense_glosses.get(comparable(label).casefold()) if label else None
        translation_id = f"{lexeme_id}#translation:{language_tag(code)}:{nfc(text)}:{digest(comparable(label))[:8]}"
        body = {
            "sense": sense_key or lexeme_key,
            "resolved_to": "sense" if sense_key else "lexeme",
            "target": {"language": language_tag(code), "text": text, "nfc": nfc(text)},
        }
        if label:
            body["sense_label"] = label
        if translation.get("lang"):
            body["target"]["language_name"] = str(translation["lang"])
        page.add(
            _record("sourced_translation", provider, src(translation_id), body), locator
        )


# ------------------------------------------------------------------ Glottolog / WALS (CLDF)


def _coordinates(row: Mapping[str, str]) -> dict[str, str] | None:
    lat, lon = row.get("Latitude"), row.get("Longitude")
    if lat is None or lon is None:
        return None
    try:
        if not (-90 <= float(lat) <= 90 and -180 <= float(lon) <= 180):
            return None
    except ValueError:
        return None
    return {"latitude": lat, "longitude": lon}


def parse_glottolog(
    tables: Mapping[str, bytes],
    declared: Mapping[str, Any],
    document: Mapping[str, Any],
) -> _Page:
    page, provider = _Page(), "glottolog"
    roots = {str(r) for r in declared.get("roots") or []}
    languages = _rows(tables["languages_url"], required=("ID", "Name", "Glottocode"))
    values = _rows(
        tables["values_url"], required=("Language_ID", "Parameter_ID", "Value")
    )
    level, classification = {}, {}
    for row in values:
        if row["Parameter_ID"] == "level":
            level[row["Language_ID"]] = row["Value"]
        elif row["Parameter_ID"] == "classification":
            classification[row["Language_ID"]] = [
                c for c in row["Value"].split("/") if c
            ]
        # every other parameter (the aes endangerment status included) is never read
    release, released = str(document["release"]), document.get("release_date")
    attribution = str(
        declared.get("attribution") or f"Glottolog {release}, MPI EVA, CC BY 4.0"
    )
    for index, row in enumerate(languages, start=2):
        code = row["Glottocode"]
        path = classification.get(row["ID"], [])
        if code not in roots and not roots & set(path):
            page.count("out_of_scope")
            continue
        if not GLOTTOCODE.fullmatch(code):
            page.reject(
                f"languages.csv row {index}", "invalid_row", "invalid Glottocode"
            )
            continue
        body: dict[str, Any] = {
            "glottocode": code,
            "name": row["Name"],
            "level": level.get(row["ID"], "language"),
            "classification": path,
        }
        if path:
            body["parent"] = path[-1]
        if row.get("ISO639P3code"):
            body["iso639_3"] = row["ISO639P3code"]
        if row.get("Macroarea"):
            body["macroareas"] = [
                m.strip() for m in row["Macroarea"].split(";") if m.strip()
            ]
        if row.get("Language_ID") and row["Language_ID"] != row["ID"]:
            body["language_of"] = row["Language_ID"]
        coordinates = _coordinates(row)
        if coordinates:
            body["coordinates"] = coordinates
        page.add(
            _record(
                "languoid",
                provider,
                _source(
                    provider,
                    code,
                    release,
                    attribution,
                    revision_date=released,
                    url=f"https://glottolog.org/resource/languoid/id/{code}",
                ),
                body,
            ),
            f"languages.csv row {index}",
        )
    return page


def _wals_sources(text: str | None) -> list[dict[str, str]]:
    out = []
    for item in (text or "").split(";"):
        item = item.strip()
        if not item:
            continue
        match = re.fullmatch(r"([^\[]+)(?:\[([^\]]*)\])?", item)
        ref = {"key": match.group(1).strip()} if match else {"key": item}
        if match and match.group(2):
            ref["pages"] = match.group(2)
        out.append(ref)
    return out


def parse_wals(
    tables: Mapping[str, bytes],
    declared: Mapping[str, Any],
    document: Mapping[str, Any],
) -> _Page:
    page, provider = _Page(), "wals"
    features = {str(f) for f in declared.get("features") or []}
    glottocodes = {str(g) for g in declared.get("glottocodes") or []}
    release, released = str(document["release"]), document.get("release_date")
    attribution = str(
        declared.get("attribution") or f"WALS Online {release}, MPI EVA, CC BY 4.0"
    )

    def src(source_id: str, url: str) -> dict[str, Any]:
        return _source(
            provider, source_id, release, attribution, revision_date=released, url=url
        )

    languages = {}
    for index, row in enumerate(
        _rows(tables["languages_url"], required=("ID", "Name")), start=2
    ):
        if row.get("Glottocode") not in glottocodes:
            page.count("out_of_scope")
            continue
        languages[row["ID"]] = row
        body: dict[str, Any] = {"wals_code": row["ID"], "name": row["Name"]}
        for column, field in (
            ("Glottocode", "glottocode"),
            ("ISO639P3code", "iso639_3"),
        ):
            if row.get(column):
                body[field] = row[column]
        coordinates = _coordinates(row)
        if coordinates:
            body["coordinates"] = coordinates
        page.add(
            _record(
                "typological_language",
                provider,
                src(
                    row["ID"], f"https://wals.info/languoid/lect/wals_code_{row['ID']}"
                ),
                body,
            ),
            f"languages.csv row {index}",
        )
    codes: dict[str, list[dict[str, str]]] = {}
    code_names = {}
    for row in _rows(tables["codes_url"], required=("ID", "Parameter_ID", "Name")):
        code_names[row["ID"]] = row
        codes.setdefault(row["Parameter_ID"], []).append(
            {
                "code_id": row["ID"],
                "name": row["Name"],
                **({"number": row["Number"]} if row.get("Number") else {}),
            }
        )
    for index, row in enumerate(
        _rows(tables["parameters_url"], required=("ID", "Name")), start=2
    ):
        if row["ID"] not in features:
            page.count("out_of_scope")
            continue
        page.add(
            _record(
                "typological_parameter",
                provider,
                src(row["ID"], f"https://wals.info/feature/{row['ID']}"),
                {
                    "parameter": row["ID"],
                    "name": row["Name"],
                    "codes": codes.get(row["ID"], []),
                },
            ),
            f"parameters.csv row {index}",
        )
    for index, row in enumerate(
        _rows(tables["values_url"], required=("Language_ID", "Parameter_ID", "Value")),
        start=2,
    ):
        if row["Parameter_ID"] not in features or row["Language_ID"] not in languages:
            page.count("out_of_scope")
            continue
        if not WALS_FEATURE.fullmatch(row["Parameter_ID"]):
            page.reject(
                f"values.csv row {index}", "invalid_row", "invalid WALS parameter id"
            )
            continue
        code = code_names.get(row.get("Code_ID", ""))
        body = {
            "wals_code": row["Language_ID"],
            "parameter": row["Parameter_ID"],
            "value": code["Name"] if code else row["Value"],
            "references": _wals_sources(row.get("Source")),
        }
        if languages[row["Language_ID"]].get("Glottocode"):
            body["glottocode"] = languages[row["Language_ID"]]["Glottocode"]
        if row.get("Code_ID"):
            body["code_id"] = row["Code_ID"]
        if row.get("Comment"):
            body["comment"] = row["Comment"]
        page.add(
            _record(
                "typological_value",
                provider,
                src(
                    f"{row['Language_ID']}:{row['Parameter_ID']}",
                    f"https://wals.info/datapoint/{row['Parameter_ID']}/wals_code_{row['Language_ID']}",
                ),
                body,
            ),
            f"values.csv row {index}",
        )
    return page


# ------------------------------------------------------------------ CLDR


def parse_cldr(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Page:
    page, provider = _Page(), "cldr"
    data = json.loads(_text(raw))
    release, kind = str(document["release"]), document["kind"]
    subjects = dict(declared.get("subjects") or {})
    locales = {str(x) for x in declared.get("locales") or []}
    attribution = f"Unicode CLDR {release}, © Unicode, Inc., Unicode License v3"

    def check(stated: Any) -> None:
        if stated is not None and str(stated).split(".")[0] != release.split(".")[0]:
            raise LinguisticsFormatError(
                "schema_drift", f"document states CLDR {stated}, declared {release}"
            )

    def add(locale: str, field: str, subject: str, value: Any) -> None:
        page.add(
            _record(
                "locale_data",
                provider,
                _source(
                    provider,
                    f"{locale}:{field}:{subject}",
                    release,
                    attribution,
                    revision_date=document.get("release_date"),
                ),
                {"locale": locale, "field": field, "subject": subject, "value": value},
            ),
            f"{locale}:{subject}",
        )

    if kind == "plurals":
        block = data.get("supplemental") or {}
        check((block.get("version") or {}).get("_cldrVersion"))
        for locale, rules in sorted((block.get("plurals-type-cardinal") or {}).items()):
            if locale not in locales:
                page.count("out_of_scope")
                continue
            add(
                locale,
                "plural_rules",
                locale,
                {
                    k.replace("pluralRule-count-", ""): v
                    for k, v in sorted(rules.items())
                },
            )
        return page
    field, section = {
        "languages": ("language_display_name", "languages"),
        "scripts": ("script_display_name", "scripts"),
    }[kind]
    wanted = {str(x) for x in subjects.get(section) or []}
    for locale, block in sorted((data.get("main") or {}).items()):
        if locale not in locales:
            page.count("out_of_scope")
            continue
        check(((block.get("identity") or {}).get("version") or {}).get("_cldrVersion"))
        for subject, name in sorted(
            ((block.get("localeDisplayNames") or {}).get(section) or {}).items()
        ):
            if subject in wanted:
                add(locale, field, subject, name)
            else:
                page.count("out_of_scope")
    return page


# ------------------------------------------------------------------ SIL ISO 639-3


def parse_iso639(
    raw: bytes, declared: Mapping[str, Any], document: Mapping[str, Any]
) -> _Page:
    page, provider = _Page(), "sil-iso639-3"
    codes = {str(c) for c in declared.get("codes") or []}
    table, dated = document["table"], document["table_date"]
    attribution = (
        f"ISO 639-3 code tables, SIL International (Registration Authority), {dated}"
    )

    def src(source_id: str, code: str) -> dict[str, Any]:
        return _source(
            provider,
            source_id,
            dated,
            attribution,
            revision_date=dated,
            url=f"https://iso639-3.sil.org/code/{code}",
        )

    if table == "codes":
        for index, row in enumerate(
            _rows(raw, delimiter="\t", required=("Id", "Scope", "Ref_Name")), start=2
        ):
            if row["Id"] not in codes:
                page.count("out_of_scope")
                continue
            if not ISO639_3.fullmatch(row["Id"]) or row["Scope"] not in ISO_SCOPES:
                page.reject(f"row {index}", "invalid_row", "invalid code or scope")
                continue
            body = {
                "code": row["Id"],
                "scope": ISO_SCOPES[row["Scope"]],
                "name": row["Ref_Name"],
            }
            for column, field in (
                ("Part1", "part1"),
                ("Part2b", "part2b"),
                ("Language_Type", "language_type"),
            ):
                if row.get(column):
                    body[field] = row[column]
            page.add(
                _record("iso_code", provider, src(row["Id"], row["Id"]), body),
                f"row {index}",
            )
    elif table == "macrolanguages":
        for index, row in enumerate(
            _rows(raw, delimiter="\t", required=("M_Id", "I_Id", "I_Status")), start=2
        ):
            if row["M_Id"] not in codes and row["I_Id"] not in codes:
                page.count("out_of_scope")
                continue
            page.add(
                _record(
                    "iso_macrolanguage",
                    provider,
                    src(f"{row['M_Id']}:{row['I_Id']}", row["M_Id"]),
                    {
                        "macrolanguage": row["M_Id"],
                        "member": row["I_Id"],
                        "status": {"A": "active", "R": "retired"}.get(
                            row["I_Status"], row["I_Status"]
                        ),
                    },
                ),
                f"row {index}",
            )
    else:
        required = ("Id", "Ref_Name", "Ret_Reason", "Effective")
        for index, row in enumerate(
            _rows(raw, delimiter="\t", required=required), start=2
        ):
            change_to = row.get("Change_To")
            remedy = row.get("Ret_Remedy", "")
            split_into = re.findall(r"\[([a-z]{3})\]", remedy)
            if (
                row["Id"] not in codes
                and change_to not in codes
                and not codes & set(split_into)
            ):
                page.count("out_of_scope")
                continue
            if row["Ret_Reason"] not in ISO_RETIREMENT_REASONS:
                page.reject(f"row {index}", "invalid_row", "unknown retirement reason")
                continue
            body = {
                "code": row["Id"],
                "name": row["Ref_Name"],
                "reason": ISO_RETIREMENT_REASONS[row["Ret_Reason"]],
                "reason_code": row["Ret_Reason"],
                "effective": iso_date(row["Effective"]),
            }
            if change_to:
                body["change_to"] = change_to
            if remedy:
                body["remedy"] = remedy
            if split_into:
                body["split_into"] = split_into
            page.add(
                _record(
                    "iso_code_change",
                    provider,
                    src(f"{row['Id']}:{body['effective']}", row["Id"]),
                    body,
                ),
                f"row {index}",
            )
    return page


# ------------------------------------------------------------------ runtime adapter


def linguistics_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("linguistics") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or declared.get("provider") != FORMATS[fmt]:
        raise SourcePackError(
            "invalid_mapping",
            "linguistics sources declare a supported format and its provider",
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_mapping", "linguistics sources declare their documents"
        )
    for document in documents:
        if not document.get("label"):
            raise SourcePackError("invalid_mapping", "documents carry a label")
        for field in DOCUMENT_URLS.get(fmt, ("url",)):
            parts = urlsplit(str(document.get(field) or ""))
            if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
                raise SourcePackError(
                    "invalid_mapping", f"{field} is an HTTPS URL on the endpoint host"
                )
        required = {
            "wikidata-lexeme-json": ("entity",),
            "kaikki-jsonl": ("language_code", "extract_date", "dump_date"),
            "glottolog-cldf": ("release",),
            "wals-cldf": ("release",),
            "cldr-json": ("release", "kind"),
            "iso639-3-tab": ("table", "table_date"),
        }[fmt]
        if any(not document.get(f) for f in required):
            raise SourcePackError(
                "invalid_mapping", f"{fmt} documents declare {', '.join(required)}"
            )
        if fmt == "wikidata-lexeme-json" and not re.fullmatch(
            r"[LQ][1-9]\d*", str(document["entity"])
        ):
            raise SourcePackError(
                "invalid_mapping", "Wikidata documents name one L- or Q-id"
            )
        if fmt == "cldr-json" and document["kind"] not in {
            "languages",
            "scripts",
            "plurals",
        }:
            raise SourcePackError(
                "invalid_mapping", "CLDR documents are languages, scripts or plurals"
            )
        if fmt == "iso639-3-tab" and document["table"] not in {
            "codes",
            "macrolanguages",
            "retirements",
        }:
            raise SourcePackError(
                "invalid_mapping",
                "ISO 639-3 tables are codes, macrolanguages or retirements",
            )
    return declared


class LinguisticsAdapter:
    """Fetch the declared documents one page at a time on the runtime's same-host transport."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = linguistics_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "linguistics": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "documents": [d["label"] for d in self.declared["documents"]],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "linguistics runs fetch the declared documents only",
            )

    def _fetch(self, url: str) -> tuple[int, bytes]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(url).hostname or "").casefold()
        response = self.transport(
            url=url,
            params={},
            headers={
                "Accept": "application/json, text/csv, text/plain",
                "User-Agent": "Noesis/0.1 (https://github.com/Ikey168/Noesis)",
            },
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        if (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold() != host:
            raise SourcePackError(
                "network_policy", "document was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "document exceeds its byte limit"
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400 and not (
            status == 404 and self.declared["format"] == "wikidata-lexeme-json"
        ):
            raise SourcePackError("schema_drift", f"document returned HTTP {status}")
        return status, raw

    def _parse(self, document: Mapping[str, Any]) -> tuple[_Page, str, int]:
        fmt = self.declared["format"]
        if fmt in DOCUMENT_URLS:
            tables = {
                field: self._fetch(document[field])[1] for field in DOCUMENT_URLS[fmt]
            }
            size = sum(len(v) for v in tables.values())
            sha = _sha(b"".join(tables[f] for f in DOCUMENT_URLS[fmt]))
            parser = parse_glottolog if fmt == "glottolog-cldf" else parse_wals
            return parser(tables, self.declared, document), sha, size
        status, raw = self._fetch(document["url"])
        if fmt == "wikidata-lexeme-json":
            page = parse_wikidata(raw, self.declared, document, status=status)
        else:
            page = {
                "kaikki-jsonl": parse_kaikki,
                "cldr-json": parse_cldr,
                "iso639-3-tab": parse_iso639,
            }[fmt](raw, self.declared, document)
        return page, _sha(raw), len(raw)

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = self.declared["documents"]
        try:
            index = 0 if cursor is None else int(cursor)
        except ValueError as exc:
            raise SourcePackError(
                "cursor_drift", "cursor is not a document index"
            ) from exc
        if not 0 <= index < len(documents):
            raise SourcePackError(
                "cursor_drift", "cursor is outside the declared documents"
            )
        document = documents[index]
        try:
            page, sha, size = self._parse(document)
        except (LinguisticsFormatError, ValueError, KeyError) as exc:
            code = getattr(exc, "code", "schema_drift")
            raise SourcePackError(
                "response_too_large" if code == "input_limit" else "schema_drift",
                f"{code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(page.records) > limit:
            raise SourcePackError(
                "budget_exhausted",
                "document has more records than the run's result budget",
            )
        records = []
        for record in page.records:
            key = record_key(record)
            body = record["body"]
            title = (
                body.get("text")
                or (body.get("lemma") or {}).get("text")
                or body.get("name")
                or key
            )
            records.append(
                {
                    "id": key,
                    "title": f"{record['kind']}: {title}"[:300],
                    "url": record["source"].get("url")
                    or document.get("url")
                    or self.source["endpoint"],
                    "language": "und",
                    "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                    "linguistic_record": record,
                }
            )
        for item in page.rejected:
            records.append(
                {
                    "id": f"{self.declared['provider']}:rejected:{document['label']}:{item['locator']}",
                    "url": document.get("url") or self.source["endpoint"],
                    "rejection": {
                        "code": item["code"],
                        "reason": item["reason"],
                        "row": item["locator"],
                    },
                }
            )
        receipt = {
            "status": 200,
            "document": document["label"],
            "file_sha256": sha,
            "bytes": size,
            "release": document.get("release")
            or document.get("table_date")
            or (
                f"{document['extract_date']}/{document['dump_date']}"
                if document.get("extract_date")
                else None
            )
            or document.get("entity"),
            "counts": {
                **page.counts,
                "records": len(page.records),
                "rejected": len(page.rejected),
            },
            "final_page": index + 1 >= len(documents),
        }
        receipt = {k: v for k, v in receipt.items() if v is not None}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: LinguisticsAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored documents keyed by URL path (+ query)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + parts.query if parts.query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = LinguisticsAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            return records
