"""Word-centred linguistic records (LG02, #2180): lexemes, forms, senses, definition revisions, etymologies, languoids.

One contract, ``noesis-linguistic-record-v1``, covers every record the
Linguistics pack acquires. A record is an attributed statement of one source at
one source revision::

    {"contract": "noesis-linguistic-record-v1", "kind": "<kind>", "provider": "<provider>",
     "source": {"source_id": ..., "revision": ..., "revision_date": ..., "url": ..., "licence": {...}},
     "body": {...}}

The record key is ``<kind>:<provider>:<source_id>``, so sources that disagree
stay side by side and no consensus is synthesised. The source id carries every
distinguishing field: a Wiktionary lexeme id holds its language code, word, part
of speech and etymology number, and a definition id holds its sense and the
language of the definition.

Lexemes are **not** canonical entities. A sense may point to a Wikidata item
only as a source-stated link (``item_link.stated_by``). No record type can hold
machine-translated or model-generated text as sourced: :func:`validate_record`
refuses producer, model or machine-translation fields. A Noesis translation
stays a ``noesis-translation-record-v1`` (``src/kb/cross_language.py``) and is
referenced by its id.

Normalisation is explicit (:func:`nfc`, :func:`comparable`, :func:`fold`,
:func:`script_of`, :func:`language_tag`):

* text is stored exactly as published, with an NFC form next to it;
* change detection compares NFC text with its whitespace collapsed, so an
  NFD/NFC switch at the source is not a revision;
* lookup keys are case-folded after NFC; identity keys are not case-folded
  where the source is case-sensitive (Wiktionary titles, lemmas);
* scripts are ISO 15924 codes detected from Unicode character names, common
  characters and combining marks excluded (``Zyyy`` = only common characters,
  ``Zzzz`` = unknown), as ``src/kb/cross_language.py`` stores them;
* language tags use BCP 47 casing.

The Leipzig Glossing Rules abbreviation list is a bundled controlled vocabulary
(:data:`LEIPZIG_ABBREVIATIONS`), published as an ontology module by
:func:`publish_glossing_vocabulary` and read back by :func:`validate_gloss`.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

CONTRACT = "noesis-linguistic-record-v1"
SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts/schemas/jsonschema/noesis-linguistic-record-v1.json"
)
READ_SCOPE = "knowledge:linguistics:read"
WRITE_SCOPE = "knowledge:linguistics:write"
REVIEW_SCOPE = "knowledge:linguistics:review"
OWNER = "linguistics.lexicon"
GLOSSING_MODULE = "linguistics-leipzig-glossing"
GLOSSING_VERSION = "1.0.0"

# kind -> required body fields
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "lexeme": ("lemma", "language", "lexical_category"),
    "form": ("lexeme", "representations"),
    "sense": ("lexeme",),
    "definition_revision": ("sense", "language", "text"),
    "usage_example": ("sense", "text"),
    "gloss": ("line",),
    "etymology_assertion": ("lexeme", "relation", "native_relation", "target"),
    "etymology_note": ("lexeme", "text"),
    "sourced_translation": ("sense", "target"),
    "languoid": ("glottocode", "name", "level"),
    "language_item": ("item",),
    "iso_code": ("code", "scope", "name"),
    "iso_code_change": ("code", "reason", "effective"),
    "iso_macrolanguage": ("macrolanguage", "member", "status"),
    "typological_language": ("wals_code", "name"),
    "typological_parameter": ("parameter", "name", "codes"),
    "typological_value": ("wals_code", "parameter", "value"),
    "locale_data": ("locale", "field", "subject", "value"),
}
KINDS = tuple(KIND_FIELDS)
PROVIDERS = (
    "wikidata-lexemes",
    "wikidata-items",
    "kaikki-wiktextract",
    "glottolog",
    "wals",
    "cldr",
    "sil-iso639-3",
)
LEVELS = ("language", "dialect", "family")
ISO_SCOPES = {"I": "individual", "M": "macrolanguage", "S": "special"}
ISO_RETIREMENT_REASONS = {
    "C": "change",
    "D": "duplicate",
    "N": "non-existent",
    "S": "split",
    "M": "merge",
}
LOCALE_FIELDS = ("language_display_name", "script_display_name", "plural_rules")
# Normalised etymology relations; each record keeps the source's native value next to it.
ETYMOLOGY_RELATIONS = (
    "inherited",
    "borrowed",
    "derived",
    "calque",
    "cognate",
    "compound-of",
)
# Keys a record body may never carry: sourced records are never machine output.
FORBIDDEN_BODY_KEYS = frozenset(
    {
        "producer",
        "model",
        "machine_translated",
        "generated",
        "translation_id",
        "translated_text",
    }
)

LEXEME_ID = re.compile(r"^L[1-9][0-9]*$")
FORM_ID = re.compile(r"^L[1-9][0-9]*-F[1-9][0-9]*$")
SENSE_ID = re.compile(r"^L[1-9][0-9]*-S[1-9][0-9]*$")
ITEM_ID = re.compile(r"^Q[1-9][0-9]*$")
GLOTTOCODE = re.compile(r"^[a-z0-9]{4}[0-9]{4}$")
ISO639_3 = re.compile(r"^[a-z]{3}$")
WALS_FEATURE = re.compile(r"^[1-9][0-9]{0,2}[A-Z]$")
WALS_CODE = re.compile(r"^[a-z]{2,3}$")

# The Leipzig Glossing Rules, Appendix: list of standard abbreviations (MPI EVA).
LEIPZIG_ABBREVIATIONS: dict[str, str] = {
    "1": "first person",
    "2": "second person",
    "3": "third person",
    "A": "agent-like argument of canonical transitive verb",
    "ABL": "ablative",
    "ABS": "absolutive",
    "ACC": "accusative",
    "ADJ": "adjective",
    "ADV": "adverb(ial)",
    "AGR": "agreement",
    "ALL": "allative",
    "ANTIP": "antipassive",
    "APPL": "applicative",
    "ART": "article",
    "AUX": "auxiliary",
    "BEN": "benefactive",
    "CAUS": "causative",
    "CLF": "classifier",
    "COM": "comitative",
    "COMP": "complementizer",
    "COMPL": "completive",
    "COND": "conditional",
    "COP": "copula",
    "CVB": "converb",
    "DAT": "dative",
    "DECL": "declarative",
    "DEF": "definite",
    "DEM": "demonstrative",
    "DET": "determiner",
    "DIST": "distal",
    "DISTR": "distributive",
    "DU": "dual",
    "DUR": "durative",
    "ERG": "ergative",
    "EXCL": "exclusive",
    "F": "feminine",
    "FOC": "focus",
    "FUT": "future",
    "GEN": "genitive",
    "IMP": "imperative",
    "INCL": "inclusive",
    "IND": "indicative",
    "INDF": "indefinite",
    "INF": "infinitive",
    "INS": "instrumental",
    "INTR": "intransitive",
    "IPFV": "imperfective",
    "IRR": "irrealis",
    "LOC": "locative",
    "M": "masculine",
    "N": "neuter",
    "NEG": "negation, negative",
    "NMLZ": "nominalizer/nominalization",
    "NOM": "nominative",
    "OBJ": "object",
    "OBL": "oblique",
    "P": "patient-like argument of canonical transitive verb",
    "PASS": "passive",
    "PFV": "perfective",
    "PL": "plural",
    "POSS": "possessive",
    "PRED": "predicative",
    "PRF": "perfect",
    "PRS": "present",
    "PROG": "progressive",
    "PROH": "prohibitive",
    "PROX": "proximal/proximate",
    "PST": "past",
    "PTCP": "participle",
    "PURP": "purposive",
    "Q": "question particle/marker",
    "QUOT": "quotative",
    "RECP": "reciprocal",
    "REFL": "reflexive",
    "REL": "relative",
    "RES": "resultative",
    "S": "single argument of canonical intransitive verb",
    "SBJ": "subject",
    "SBJV": "subjunctive",
    "SG": "singular",
    "TOP": "topic",
    "TR": "transitive",
    "VOC": "vocative",
}
_GLOSS_SEPARATORS = re.compile(r"[-=.\\_>~:]")

_SCRIPT_PREFIXES = (
    ("CJK", "Hani"),
    ("LATIN", "Latn"),
    ("CYRILLIC", "Cyrl"),
    ("GREEK", "Grek"),
    ("ARABIC", "Arab"),
    ("HEBREW", "Hebr"),
    ("DEVANAGARI", "Deva"),
    ("GEORGIAN", "Geor"),
    ("ARMENIAN", "Armn"),
    ("THAI", "Thai"),
    ("HIRAGANA", "Hira"),
    ("KATAKANA", "Kana"),
    ("HANGUL", "Hang"),
    ("BENGALI", "Beng"),
    ("TAMIL", "Taml"),
    ("ETHIOPIC", "Ethi"),
    ("GURMUKHI", "Guru"),
    ("GUJARATI", "Gujr"),
    ("KANNADA", "Knda"),
    ("TELUGU", "Telu"),
    ("MALAYALAM", "Mlym"),
    ("SINHALA", "Sinh"),
    ("TIBETAN", "Tibt"),
    ("MONGOLIAN", "Mong"),
    ("KHMER", "Khmr"),
    ("LAO", "Laoo"),
    ("MYANMAR", "Mymr"),
    ("SYRIAC", "Syrc"),
    ("THAANA", "Thaa"),
    ("RUNIC", "Runr"),
    ("OGHAM", "Ogam"),
    ("GOTHIC", "Goth"),
    ("COPTIC", "Copt"),
    ("CHEROKEE", "Cher"),
    ("TIFINAGH", "Tfng"),
)


class LinguisticsError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.details = code, details


# ------------------------------------------------------------------ canonical helpers


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    """Linguistics scope plus namespace access (operator bypasses)."""
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise LinguisticsError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes or ())
    if required not in scopes and "operator" not in scopes:
        raise LinguisticsError("unauthorized", f"{required} is required")


# ------------------------------------------------------------------ text normalisation


def nfc(text: Any) -> str:
    """Unicode NFC (canonical composition); the stored comparison form of every text."""
    return unicodedata.normalize("NFC", str(text))


def comparable(text: Any) -> str:
    """NFC with whitespace collapsed: what change detection compares."""
    return " ".join(nfc(text).split())


def fold(text: Any) -> str:
    """Lookup key: NFC, full Unicode case folding, NFC again (``ß`` folds to ``ss``; not locale-aware)."""
    return nfc(nfc(text).casefold())


def script_of(text: Any) -> dict[str, Any]:
    """ISO 15924 script of a text from Unicode character names.

    Common characters (numbers, punctuation, symbols, separators) and combining
    marks are ignored. ``Zyyy`` means only common characters were seen,
    ``Zzzz`` a letter of an unmapped script. Mixed text is labelled with its
    majority script and ``mixed: True``.
    """
    counts: dict[str, int] = {}
    for char in nfc(text):
        category = unicodedata.category(char)
        if category[0] in {"N", "P", "S", "Z", "C"} or category in {
            "Mn",
            "Mc",
            "Me",
            "Lm",
        }:
            continue
        name = unicodedata.name(char, "")
        code = next(
            (c for prefix, c in _SCRIPT_PREFIXES if name.startswith(prefix)), "Zzzz"
        )
        counts[code] = counts.get(code, 0) + 1
    if not counts:
        return {"script": "Zyyy", "mixed": False, "scripts": []}
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return {
        "script": ranked[0][0],
        "mixed": len(ranked) > 1,
        "scripts": [code for code, _ in ranked],
    }


def language_tag(value: Any) -> str:
    """BCP 47 casing: lower-case language, title-case script, upper-case region; private use kept as given."""
    parts = [p for p in re.split(r"[-_]", str(value or "").strip()) if p]
    if not parts:
        return "und"
    out, private = [parts[0].lower()], False
    for part in parts[1:]:
        if private:
            out.append(part)
        elif part.lower() == "x":
            private = True
            out.append("x")
        elif len(part) == 4 and part.isalpha():
            out.append(part.title())
        elif (len(part) == 2 and part.isalpha()) or (len(part) == 3 and part.isdigit()):
            out.append(part.upper())
        else:
            out.append(part.lower())
    return "-".join(out)


def representation(text: Any, language: Any) -> dict[str, Any]:
    """A written representation as published, with its NFC form, language tag and detected script."""
    script = script_of(text)
    return {
        "text": str(text),
        "nfc": nfc(text),
        "language": language_tag(language),
        "script": script["script"],
        **({"mixed_script": True} if script["mixed"] else {}),
    }


# ------------------------------------------------------------------ dates


def iso_date(value: Any) -> str | None:
    """A source date or instant as ISO: ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); absent stays None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.replace(tzinfo=UTC)
        return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return date.fromisoformat(text).isoformat()
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LinguisticsError("invalid_date", f"{text!r} is not an ISO date") from exc
    return iso_date(stamp)


def date_key(value: Any, *, end_of_day: bool = False) -> datetime:
    """Compare dates as dates: a date is midnight UTC (or the end of that day when ``end_of_day``)."""
    text = iso_date(value)
    if text is None:
        raise LinguisticsError("invalid_date", "a date is required")
    if len(text) == 10:
        day = date.fromisoformat(text)
        return datetime.combine(day, time.max if end_of_day else time.min, tzinfo=UTC)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def revision_order(revision: Any) -> str:
    """A sortable form of a source revision: integers zero-padded, dotted versions padded per part."""
    text = str(revision or "").strip().lstrip("v")
    if text.isdigit():
        return text.zfill(20)
    if re.fullmatch(r"\d+(?:\.\d+)+", text):
        return ".".join(part.zfill(8) for part in text.split("."))
    return text


# ------------------------------------------------------------------ records


def record_key(record: Mapping[str, Any]) -> str:
    return f"{record['kind']}:{record['provider']}:{record['source']['source_id']}"


def _no_none_strings(value: Any, path: str) -> None:
    if isinstance(value, str):
        if value in {"None", "null"}:
            raise LinguisticsError(
                "invalid_record",
                f"{path} is the string {value!r}; missing values are absent",
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if item is None:
                raise LinguisticsError(
                    "invalid_record", f"{path}.{key} is null; missing values are absent"
                )
            _no_none_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _no_none_strings(item, f"{path}[{index}]")


def _forbidden(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in FORBIDDEN_BODY_KEYS:
                raise LinguisticsError(
                    "machine_output_refused",
                    f"{path}.{key}: sourced linguistic records never hold machine-translated or generated text; "
                    "a Noesis translation is a noesis-translation-record-v1 referenced by id",
                )
            _forbidden(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _forbidden(item, f"{path}[{index}]")


def validate_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one record; returns a normalised copy (NFC forms added, dates ISO)."""
    if not isinstance(record, Mapping) or record.get("contract") != CONTRACT:
        raise LinguisticsError(
            "invalid_record", f"a record declares contract {CONTRACT}"
        )
    kind, provider = record.get("kind"), record.get("provider")
    if kind not in KIND_FIELDS:
        raise LinguisticsError("invalid_record", f"unknown record kind {kind!r}")
    if provider not in PROVIDERS:
        raise LinguisticsError("invalid_record", f"unknown provider {provider!r}")
    source, body = dict(record.get("source") or {}), dict(record.get("body") or {})
    for field in ("source_id", "revision", "licence"):
        if not source.get(field):
            raise LinguisticsError("invalid_record", f"source.{field} is required")
    licence = dict(source["licence"])
    if (
        not licence.get("id")
        or not licence.get("attribution")
        or not isinstance(licence.get("share_alike"), bool)
    ):
        raise LinguisticsError(
            "invalid_record",
            "a licence names its id, attribution text and share_alike flag",
        )
    missing = [f for f in KIND_FIELDS[kind] if f not in body]
    if missing:
        raise LinguisticsError(
            "invalid_record", f"{kind} body lacks {', '.join(missing)}"
        )
    _no_none_strings(source, "source")
    _no_none_strings(body, "body")
    _forbidden(body, "body")
    if body.get("text_origin", "source") != "source":
        raise LinguisticsError(
            "machine_output_refused",
            "text_origin is always 'source' for sourced records",
        )
    if kind == "languoid":
        if not GLOTTOCODE.fullmatch(str(body["glottocode"])):
            raise LinguisticsError("invalid_record", "a languoid carries a Glottocode")
        if body["level"] not in LEVELS:
            raise LinguisticsError("invalid_record", f"level is one of {LEVELS}")
        if body.get("iso639_3") and not ISO639_3.fullmatch(str(body["iso639_3"])):
            raise LinguisticsError("invalid_record", "iso639_3 is a three-letter code")
    if kind == "etymology_assertion" and body["relation"] not in ETYMOLOGY_RELATIONS:
        raise LinguisticsError(
            "invalid_record", f"relation is one of {ETYMOLOGY_RELATIONS}"
        )
    if kind in {"iso_code", "iso_code_change"} and not ISO639_3.fullmatch(
        str(body["code"])
    ):
        raise LinguisticsError(
            "invalid_record", "ISO 639-3 codes are three lower-case letters"
        )
    if kind == "typological_value" and not WALS_FEATURE.fullmatch(
        str(body["parameter"])
    ):
        raise LinguisticsError("invalid_record", "a WALS parameter id looks like 81A")
    if kind == "locale_data" and body["field"] not in LOCALE_FIELDS:
        raise LinguisticsError("invalid_record", f"field is one of {LOCALE_FIELDS}")
    if source.get("revision_date") is not None:
        source["revision_date"] = iso_date(source["revision_date"])
    for key in ("text", "line"):
        if key in body:
            body[f"{key}_nfc"] = nfc(body[key])
    return {
        "contract": CONTRACT,
        "kind": kind,
        "provider": provider,
        "source": source,
        "body": body,
    }


def comparable_body(kind: str, body: Mapping[str, Any]) -> Any:
    """The normalised, source-independent content change detection compares."""

    def norm(value: Any) -> Any:
        if isinstance(value, str):
            return comparable(value)
        if isinstance(value, Mapping):
            return {
                k: norm(v) for k, v in sorted(value.items()) if not k.endswith("_nfc")
            }
        if isinstance(value, list):
            return [norm(v) for v in value]
        return value

    return {"kind": kind, "body": norm(body)}


def content_hash(kind: str, body: Mapping[str, Any]) -> str:
    return digest(comparable_body(kind, body))


# ------------------------------------------------------------------ glossing


def gloss_tokens(line: str) -> list[str]:
    tokens = []
    for word in nfc(line).split():
        for token in _GLOSS_SEPARATORS.split(word.strip("[]()<>")):
            token = token.strip("[]()<>")
            if token:
                tokens.append(token)
    return tokens


def _category(token: str, vocabulary: Mapping[str, str]) -> bool:
    match = re.fullmatch(r"([123]*)([A-Z]*)", token)
    if not match:
        return False
    person, rest = match.groups()
    if not rest:
        return bool(person)
    if rest in vocabulary:
        return True
    return (
        rest.startswith("N") and rest[1:] in vocabulary and bool(rest[1:])
    )  # the N- (non-) prefix


def validate_gloss(
    line: str, vocabulary: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Check a Leipzig-style gloss line: category labels (all capitals and digits) must be in the vocabulary.

    Lexical glosses (any lower-case letter) are not checked. Unknown labels are
    reported, never rejected: sources may define their own abbreviations.
    """
    vocabulary = vocabulary or LEIPZIG_ABBREVIATIONS
    categories, unknown = [], []
    for token in gloss_tokens(line):
        if any(ch.islower() for ch in token):
            continue
        categories.append(token)
        if not _category(token, vocabulary):
            unknown.append(token)
    return {
        "line": line,
        "line_nfc": nfc(line),
        "categories": categories,
        "unknown": sorted(set(unknown)),
        "status": "validated" if not unknown else "unvalidated",
        "vocabulary": GLOSSING_MODULE,
    }


def glossing_concepts() -> list[dict[str, Any]]:
    return [
        {
            "concept_id": abbreviation,
            "definition": meaning,
            "labels": [
                {"value": abbreviation, "language": "und", "kind": "preferred"},
                {"value": meaning, "language": "en", "kind": "alternative"},
            ],
        }
        for abbreviation, meaning in sorted(LEIPZIG_ABBREVIATIONS.items())
    ]


def publish_glossing_vocabulary(
    conn: Any, *, principal_id: str, now=None
) -> dict[str, Any]:
    """Publish the abbreviation list as an ontology module (idempotent per version)."""
    from src.kb.ontology import OntologyAlignmentStore

    concepts = glossing_concepts()
    module = OntologyAlignmentStore(conn, now=now).publish(
        GLOSSING_MODULE,
        GLOSSING_VERSION,
        concepts,
        owner=OWNER,
        provenance={
            "kind": "builtin",
            "source": "The Leipzig Glossing Rules, Appendix: list of standard "
            "abbreviations (MPI EVA; transcribed, see source-audit.md)",
        },
        idempotency_key=f"{GLOSSING_MODULE}:{GLOSSING_VERSION}:{digest(concepts)[:16]}",
        principal_id=principal_id,
        scopes={"knowledge:schema:register", "knowledge:schema:read"},
        compatibility_policy="none",
        observed_at_ms=0,
    )
    return {
        "module": GLOSSING_MODULE,
        "version": GLOSSING_VERSION,
        "module_id": module["module_id"],
        "concepts": len(concepts),
    }


def glossing_vocabulary(conn: Any) -> dict[str, str]:
    """The published vocabulary module, else the bundled list."""
    try:
        from src.kb.schema_registry import SchemaRegistry

        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='knowledge_schema_modules'"
        ).fetchone()
        if not exists:
            return dict(LEIPZIG_ABBREVIATIONS)
        module = SchemaRegistry(conn, initialize=False).resolve(
            "ontology", GLOSSING_MODULE, "*", scopes={"knowledge:schema:read"}
        )
    except Exception:  # noqa: BLE001 - an unpublished module falls back to the bundled list
        return dict(LEIPZIG_ABBREVIATIONS)
    return {c["concept_id"]: c["definition"] for c in module["content"]["concepts"]}


# ------------------------------------------------------------------ schema registry


def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any
) -> list[dict[str, Any]]:
    """Register the record contract as a schema module in the shared registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    content = schema()
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "linguistic-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": content,
        "owner": OWNER,
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {
            "kind": "imported",
            "source": f"contracts/schemas/jsonschema/{CONTRACT}.json",
        },
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            f"linguistics-schema:linguistic-record:1.0.0:{digest(content)[:16]}",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]
