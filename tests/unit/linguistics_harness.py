"""Offline harness for the Linguistics pack (#2178): authored, synthetic documents through the real adapters.

Every document here is authored in the providers' documented formats and names
a **fictional** language family: Veltic (``velt1234``) with Northern Velan
(``nort3456``, ISO 639-3 ``qnv``), Southern Velan (``sout7890``, ``qsv``), the
dialect Coastal Velan (``coas1122``), the macrolanguage Velan (``qmv``) and the
retired code ``qrv``. The ISO codes are in the private-use range ``qaa``-``qtz``.
L-ids (``L900xx``) and Q-ids (``Q9000xx``) are synthetic. Nothing here is
captured data or live coverage.

Two stages simulate two acquisitions:

* stage 1 (observed 2026-03-05): Wikidata revisions ``2100000001``..., kaikki
  extract 2026-03-01 (dump 2026-02-20), Glottolog v5.0, WALS v2020.3, CLDR 45,
  SIL tables of 2026-01-15;
* stage 2 (observed 2026-07-15): a Wikidata revision that changes a gloss and
  deletes the verb homograph, a kaikki extract (2026-06-01, dump 2026-05-20)
  whose noun definition changed, Glottolog v5.1 moving the dialect, and a WALS
  release changing one value.

``python -m tests.unit.linguistics_harness`` rewrites the pinned stage-1
fixtures and their hashes in ``config/source_packs/linguistics.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.linguistics_sources import LinguisticsAdapter, fixture_transport
from src.ingestion.source_packs import _digest, validate_source_pack
from src.kb.linguistics_store import LinguisticsProjector

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/linguistics.json"
NS = "linguistics"
PRINCIPAL = "linguist"
SCOPES = {
    "knowledge:linguistics:read",
    "knowledge:linguistics:write",
    "knowledge:linguistics:review",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:cross-language:read",
    "knowledge:cross-language:write",
    "knowledge:cross-language:review",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:schema:read",
    "knowledge:schema:register",
    "namespace:global:read",
    "namespace:global:write",
}
SOURCES = {
    "wikidata": "wikidata-lexemes",
    "kaikki": "kaikki-wiktextract",
    "glottolog": "glottolog-cldf",
    "wals": "wals-cldf",
    "cldr": "cldr-json",
    "iso": "sil-iso639-3",
}
STAGE_DAYS = {1: "2026-03-05", 2: "2026-07-15"}


def ms(day: str, hour: int = 12) -> int:
    return int(
        datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC).timestamp() * 1000
    )


# ------------------------------------------------------------------ Wikidata


def _snak(prop, value, kind="wikibase-item"):
    if kind == "string":
        return {
            "snaktype": "value",
            "property": prop,
            "datavalue": {"type": "string", "value": value},
        }
    entity = "lexeme" if value.startswith("L") else "item"
    return {
        "snaktype": "value",
        "property": prop,
        "datavalue": {
            "type": "wikibase-entityid",
            "value": {"entity-type": entity, "id": value},
        },
    }


def _statement(
    owner, uuid, prop, value, *, kind="wikibase-item", qualifiers=None, references=None
):
    statement = {
        "id": f"{owner}${uuid}",
        "type": "statement",
        "rank": "normal",
        "mainsnak": _snak(prop, value, kind),
    }
    if qualifiers:
        statement["qualifiers"] = {q: [_snak(q, v)] for q, v in qualifiers.items()}
    statement["references"] = references or []
    return statement


def _lexeme(
    lid,
    *,
    revid,
    modified,
    lemma,
    lang_code,
    language,
    category,
    forms,
    senses,
    claims=None,
):
    return {
        "entities": {
            lid: {
                "type": "lexeme",
                "id": lid,
                "lastrevid": revid,
                "modified": modified,
                "lemmas": {lang_code: {"language": lang_code, "value": lemma}},
                "language": language,
                "lexicalCategory": category,
                "claims": claims or {},
                "forms": [
                    {
                        "id": f"{lid}-F{i + 1}",
                        "representations": {
                            lang_code: {"language": lang_code, "value": text}
                        },
                        "grammaticalFeatures": features,
                        "claims": {},
                    }
                    for i, (text, features) in enumerate(forms)
                ],
                "senses": [
                    {
                        "id": f"{lid}-S{i + 1}",
                        "glosses": {"en": {"language": "en", "value": gloss}},
                        "claims": {
                            "P5137": [
                                _statement(
                                    f"{lid}-S{i + 1}",
                                    f"sense-item-{i + 1}",
                                    "P5137",
                                    item,
                                )
                            ]
                        }
                        if item
                        else {},
                    }
                    for i, (gloss, item) in enumerate(senses)
                ],
            }
        }
    }


def _item(qid, *, revid, label, glottocode, iso):
    return {
        "entities": {
            qid: {
                "type": "item",
                "id": qid,
                "lastrevid": revid,
                "modified": "2026-01-20T08:00:00Z",
                "labels": {"en": {"language": "en", "value": label}},
                "claims": {
                    "P1394": [
                        _statement(
                            qid, "glottocode", "P1394", glottocode, kind="string"
                        )
                    ],
                    "P220": [_statement(qid, "iso", "P220", iso, kind="string")],
                },
            }
        }
    }


ETYMOLOGY_REFERENCE = [
    {
        "snaks": {
            "P248": [_snak("P248", "Q900200")],
            "P356": [_snak("P356", "10.5555/VELAN.1998.12", kind="string")],
        }
    }
]


def wikidata_documents(stage: int) -> dict[str, dict]:
    noun_gloss = "river" if stage == 1 else "river; a large stream"
    tamo_noun = _lexeme(
        "L90001",
        revid=2100000001 if stage == 1 else 2100000077,
        modified="2026-02-01T10:00:00Z" if stage == 1 else "2026-06-10T09:30:00Z",
        lemma="tamo",
        lang_code="qnv",
        language="Q900001",
        category="Q1084",
        forms=[("tamo", ["Q110786"]), ("tamoi", ["Q146786"])],
        senses=[(noun_gloss, "Q4022")],
        claims={
            "P5191": [
                _statement(
                    "L90001",
                    "etym-1",
                    "P5191",
                    "L90002",
                    qualifiers={"P5886": "Q900101"},
                    references=ETYMOLOGY_REFERENCE,
                )
            ]
        },
    )
    tamu = _lexeme(
        "L90002",
        revid=2100000002,
        modified="2026-02-01T10:05:00Z",
        lemma="tamu",
        lang_code="qsv",
        language="Q900002",
        category="Q1084",
        forms=[("tamu", ["Q110786"])],
        senses=[("river", "Q4022")],
        claims={"P5191": [_statement("L90002", "etym-2", "P5191", "L90005")]},
    )  # unreferenced, target not acquired
    docs = {
        "L90001": tamo_noun,
        "L90002": tamu,
        "Q900001": _item(
            "Q900001",
            revid=2000000001,
            label="Northern Velan",
            glottocode="nort3456",
            iso="qnv",
        ),
        "Q900002": _item(
            "Q900002",
            revid=2000000002,
            label="Southern Velan",
            glottocode="sout7890",
            iso="qsv",
        ),
    }
    if stage == 1:
        docs["L90003"] = _lexeme(
            "L90003",
            revid=2100000003,
            modified="2026-02-01T10:10:00Z",
            lemma="tamo",
            lang_code="qnv",
            language="Q900001",
            category="Q24905",
            forms=[("tamo", [])],
            senses=[("to flow", None)],
        )
    else:
        docs["L90003"] = None  # deleted: HTTP 404
    return docs


# ------------------------------------------------------------------ kaikki.org Wiktextract


def kaikki_entries(stage: int) -> dict[str, list[dict]]:
    noun_gloss = "a river" if stage == 1 else "a large river; a stream"
    qnv = [
        {
            "word": "tamo",
            "lang": "Northern Velan",
            "lang_code": "qnv",
            "pos": "noun",
            "etymology_number": 1,
            "etymology_text": "Inherited from Proto-Velan *tamə. Cognate with Southern Velan tamu.",
            "etymology_templates": [
                {
                    "name": "inh",
                    "args": {"1": "qnv", "2": "qvl-pro", "3": "*tamə"},
                    "expansion": "Proto-Velan *tamə",
                },
                {
                    "name": "cog",
                    "args": {"1": "qsv", "2": "tamu"},
                    "expansion": "Southern Velan tamu",
                },
                {"name": "m", "args": {"1": "qsv", "2": "tamu"}, "expansion": "tamu"},
            ],
            "forms": [
                {"form": "tamoi", "tags": ["plural"]},
                {"form": "velan-noun", "tags": ["inflection-template"]},
            ],
            "sounds": [
                {"ipa": "/ˈta.mo/"},
                {
                    "audio": "Qnv-tamo.ogg",
                    "ogg_url": "https://upload.wikimedia.org/x.ogg",
                },
            ],
            "senses": [
                {
                    "id": "en-tamo-qnv-noun-Ab1Cd2",
                    "glosses": [noun_gloss],
                    "wikidata": ["Q4022"],
                    "examples": [
                        {
                            "text": "Tamoi sela.",
                            "english": "The rivers are wide.",
                            "ref": "Fictional Velan Reader (2001), p. 4",
                            "gloss": "river-PL wide-PRS",
                        }
                    ],
                }
            ],
        },
        {
            "word": "tamo",
            "lang": "Northern Velan",
            "lang_code": "qnv",
            "pos": "verb",
            "etymology_number": 2,
            "senses": [{"id": "en-tamo-qnv-verb-Ef3Gh4", "glosses": ["to flow"]}],
        },
        {
            "word": "banka",
            "lang": "Northern Velan",
            "lang_code": "qnv",
            "pos": "noun",
            "etymology_text": "Borrowed from Italian banca.",
            "etymology_templates": [
                {
                    "name": "bor",
                    "args": {"1": "qnv", "2": "it", "3": "banca"},
                    "expansion": "Italian banca",
                }
            ],
            "senses": [
                {
                    "id": "en-banka-qnv-noun-Ij5Kl6",
                    "glosses": ["bank (financial institution)"],
                }
            ],
        },
        {
            "word": "selo",
            "lang": "Northern Velan",
            "lang_code": "qnv",
            "pos": "adj",
            "senses": [{"glosses": ["wide"]}],
        },
    ]
    qsv = [
        {
            "word": "tamu",
            "lang": "Southern Velan",
            "lang_code": "qsv",
            "pos": "noun",
            "etymology_templates": [
                {
                    "name": "inh",
                    "args": {"1": "qsv", "2": "qvl-pro", "3": "*tamə"},
                    "expansion": "Proto-Velan *tamə",
                }
            ],
            "senses": [
                {
                    "id": "en-tamu-qsv-noun-Mn7Op8",
                    "glosses": ["a river"],
                    "wikidata": ["Q4022"],
                }
            ],
        },
    ]
    en = [
        {
            "word": "river",
            "lang": "English",
            "lang_code": "en",
            "pos": "noun",
            "senses": [
                {
                    "id": "en-river-en-noun-Qr9St0",
                    "glosses": ["A large natural stream of water."],
                }
            ],
            "translations": [
                {
                    "lang": "Northern Velan",
                    "code": "qnv",
                    "word": "tamo",
                    "sense": "A large natural stream of water.",
                },
                {
                    "lang": "Southern Velan",
                    "code": "qsv",
                    "word": "tamu",
                    "sense": "A large natural stream of water.",
                },
            ],
        },
    ]
    return {"qnv": qnv, "qsv": qsv, "en": en}


def jsonl(entries: list[dict]) -> str:
    return "".join(
        json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries
    )


KAIKKI_RELEASES = {1: ("2026-03-01", "2026-02-20"), 2: ("2026-06-01", "2026-05-20")}
KAIKKI_NAMES = {"qnv": "Northern%20Velan", "qsv": "Southern%20Velan", "en": "English"}

# ------------------------------------------------------------------ Glottolog / WALS / CLDR / SIL


def glottolog_tables(stage: int) -> dict[str, str]:
    dialect_parent = "nort3456" if stage == 1 else "sout7890"
    languages = (
        "ID,Name,Macroarea,Latitude,Longitude,Glottocode,ISO639P3code,Family_ID,Language_ID\n"
        "velt1234,Veltic,Eurasia,,,velt1234,,,\n"
        "nort3456,Northern Velan,Eurasia,61.5,24.25,nort3456,qnv,velt1234,\n"
        "sout7890,Southern Velan,Eurasia,59.75,23.5,sout7890,qsv,velt1234,\n"
        f"coas1122,Coastal Velan,Eurasia,60.1,22.9,coas1122,,velt1234,{dialect_parent}\n"
        "othr0001,Other Isolate,Africa,1.0,1.0,othr0001,,,\n"
    )
    values = (
        "ID,Language_ID,Parameter_ID,Value,Code_ID\n"
        "velt1234-level,velt1234,level,family,\n"
        "nort3456-level,nort3456,level,language,\n"
        "sout7890-level,sout7890,level,language,\n"
        "coas1122-level,coas1122,level,dialect,\n"
        "othr0001-level,othr0001,level,language,\n"
        "nort3456-classification,nort3456,classification,velt1234,\n"
        "sout7890-classification,sout7890,classification,velt1234,\n"
        f"coas1122-classification,coas1122,classification,velt1234/{dialect_parent},\n"
        "nort3456-aes,nort3456,aes,threatened,aes-threatened\n"
    )
    return {"languages": languages, "values": values}


GLOTTOLOG_RELEASES = {1: ("v5.0", "2026-01-10"), 2: ("v5.1", "2026-07-10")}
WALS_RELEASES = {1: ("v2020.3", "2020-12-01"), 2: ("v2026.1", "2026-07-01")}


def wals_tables(stage: int) -> dict[str, str]:
    sve_order = "2,81A-2" if stage == 1 else "7,81A-7"
    return {
        "languages": (
            "ID,Name,Macroarea,Latitude,Longitude,Glottocode,ISO639P3code\n"
            "nve,Northern Velan,Eurasia,61.5,24.25,nort3456,qnv\n"
            "sve,Southern Velan,Eurasia,59.75,23.5,sout7890,qsv\n"
            "els,Elsewhere,Africa,0,0,othr0001,\n"
        ),
        "parameters": (
            'ID,Name\n81A,"Order of Subject, Object and Verb"\n87A,Order of Adjective and Noun\n'
            "1A,Consonant Inventories\n"
        ),
        "codes": (
            "ID,Parameter_ID,Name,Number\n81A-1,81A,SOV,1\n81A-2,81A,SVO,2\n81A-7,81A,No dominant order,7\n"
            "87A-1,87A,Adjective-Noun,1\n87A-2,87A,Noun-Adjective,2\n1A-1,1A,Small,1\n"
        ),
        "values": (
            "ID,Language_ID,Parameter_ID,Value,Code_ID,Comment,Source\n"
            "81A-nve,nve,81A,1,81A-1,,Fictiva-2019[12-14]\n"
            f"81A-sve,sve,81A,{sve_order},,Fictiva-2019[15];Imaginus-2021\n"
            "87A-nve,nve,87A,2,87A-2,,Fictiva-2019\n"
            "1A-nve,nve,1A,1,1A-1,,Fictiva-2019\n"
        ),
    }


def cldr_documents() -> dict[str, dict]:
    version = {"_cldrVersion": "45"}
    return {
        "languages-de": {
            "main": {
                "de": {
                    "identity": {"version": version, "language": "de"},
                    "localeDisplayNames": {
                        "languages": {
                            "de": "Deutsch",
                            "en": "Englisch",
                            "fr": "Französisch",
                        }
                    },
                }
            }
        },
        "languages-en": {
            "main": {
                "en": {
                    "identity": {"version": version, "language": "en"},
                    "localeDisplayNames": {
                        "languages": {"de": "German", "en": "English", "fr": "French"}
                    },
                }
            }
        },
        "scripts-de": {
            "main": {
                "de": {
                    "identity": {"version": version, "language": "de"},
                    "localeDisplayNames": {
                        "scripts": {
                            "Latn": "Lateinisch",
                            "Cyrl": "Kyrillisch",
                            "Grek": "Griechisch",
                        }
                    },
                }
            }
        },
        "plurals": {
            "supplemental": {
                "version": version,
                "plurals-type-cardinal": {
                    "de": {
                        "pluralRule-count-one": "i = 1 and v = 0 @integer 1",
                        "pluralRule-count-other": " @integer 0, 2~16, 100, 1000, 10000, 100000, 1000000, …",
                    },
                    "en": {
                        "pluralRule-count-one": "i = 1 and v = 0 @integer 1",
                        "pluralRule-count-other": " @integer 0, 2~16, 100, 1000, 10000, 100000, 1000000, …",
                    },
                    "fr": {"pluralRule-count-one": "i = 0,1 @integer 0, 1"},
                },
            }
        },
    }


def iso_tables() -> dict[str, str]:
    return {
        "codes": (
            "Id\tPart2b\tPart2t\tPart1\tScope\tLanguage_Type\tRef_Name\tComment\n"
            "qnv\t\t\t\tI\tL\tNorthern Velan\t\n"
            "qsv\t\t\t\tI\tL\tSouthern Velan\t\n"
            "qmv\t\t\t\tM\tL\tVelan\t\n"
            "qaa\t\t\t\tS\tS\tReserved for local use\t\n"
        ),
        "macrolanguages": "M_Id\tI_Id\tI_Status\nqmv\tqnv\tA\nqmv\tqsv\tA\nqzz\tqzy\tA\n",
        "retirements": (
            "Id\tRef_Name\tRet_Reason\tChange_To\tRet_Remedy\tEffective\n"
            "qrv\tOld Velan\tS\t\tSplit into Northern Velan [qnv] and Southern Velan [qsv]\t2024-01-15\n"
            "qzx\tElsewhere\tD\tqzy\t\t2019-01-01\n"
        ),
    }


# ------------------------------------------------------------------ pages and sources


def production(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(
        next(s for s in manifest["sources"] if s["source_id"] == SOURCES[key])
    )


def _rehash(source: dict) -> dict:
    source.pop("source_hash", None)
    source["source_hash"] = _digest(source)
    return source


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


def staged(key: str, stage: int) -> tuple[dict, list[dict]]:
    """The declared source for a stage (stage-2 documents carry the later release) and its native pages."""
    source = production(key)
    declared = source["linguistics"]
    pages: list[dict] = []
    if key == "wikidata":
        docs = wikidata_documents(stage)
        for document in declared["documents"]:
            body = docs[document["entity"]]
            pages.append(
                {
                    "request": _path(document["url"]),
                    "status": 404 if body is None else 200,
                    "body": "" if body is None else body,
                }
            )
    elif key == "kaikki":
        extract, dump = KAIKKI_RELEASES[stage]
        entries = kaikki_entries(stage)
        for document in declared["documents"]:
            document.update(extract_date=extract, dump_date=dump)
            pages.append(
                {
                    "request": _path(document["url"]),
                    "body": jsonl(entries[document["language_code"]]),
                }
            )
    elif key in {"glottolog", "wals"}:
        release, released = (
            GLOTTOLOG_RELEASES if key == "glottolog" else WALS_RELEASES
        )[stage]
        tables = glottolog_tables(stage) if key == "glottolog" else wals_tables(stage)
        for document in declared["documents"]:
            if stage == 2:
                for field in [f for f in document if f.endswith("_url")]:
                    document[field] = document[field].replace(
                        document["release"], release
                    )
            document.update(release=release, release_date=released)
            for field in [f for f in document if f.endswith("_url")]:
                pages.append(
                    {
                        "request": _path(document[field]),
                        "body": tables[field.removesuffix("_url")],
                    }
                )
    elif key == "cldr":
        docs = cldr_documents()
        for document in declared["documents"]:
            pages.append(
                {"request": _path(document["url"]), "body": docs[document["label"]]}
            )
    else:
        tables = iso_tables()
        for document in declared["documents"]:
            pages.append(
                {"request": _path(document["url"]), "body": tables[document["table"]]}
            )
    return _rehash(source), pages


def run(
    conn, key: str, stage: int = 1, *, observed_at_ms: int | None = None
) -> list[dict]:
    """Fetch every page through the real adapter and project it as the runtime does."""
    source, pages = staged(key, stage)
    adapter = LinguisticsAdapter(source, transport=fixture_transport(pages))
    projector = LinguisticsProjector(conn)
    observed = observed_at_ms if observed_at_ms is not None else ms(STAGE_DAYS[stage])
    results, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records = [r for r in page.records if not r.get("rejection")]
        results.append(
            {
                "receipt": page.receipt,
                "records": list(page.records),
                "stored": projector.project_page(
                    run_id=f"{key}-{stage}-{observed}",
                    manifest={},
                    source=source,
                    records=records,
                    documents=[{"ingested_at": observed}],
                    page_receipt=page.receipt,
                    principal_id=PRINCIPAL,
                ),
            }
        )
        cursor = page.next_cursor
        if cursor is None:
            return results


def load_all(conn, stage: int = 1, *, keys=tuple(SOURCES)) -> None:
    for key in keys:
        run(conn, key, stage)


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(":memory:")


def main() -> None:
    """Rewrite the pinned stage-1 fixtures and their hashes in the source pack."""
    from src.ingestion.linguistics_sources import replay_native_fixture

    manifest = json.loads(PACK.read_text())
    for raw_source in manifest["sources"]:
        key = next(k for k, v in SOURCES.items() if v == raw_source["source_id"])
        source, pages = staged(key, 1)
        fixture = {
            "captured": False,
            "provider": source["linguistics"]["provider"],
            "note": "Authored documents in the documented format naming a fictional language family "
            "(Veltic); synthetic identifiers only. See tests/unit/linguistics_harness.py.",
            "native_pages": pages,
            "scenarios": [
                "bounded-selection",
                "out-of-scope-counted",
                "attributed-licence",
            ],
        }
        path = (
            ROOT
            / f"tests/fixtures/source_packs/linguistics-{raw_source['source_id']}.json"
        )
        text = json.dumps(fixture, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        path.write_text(text)
        raw_source["fixture"] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "expected_output_hash": _digest(replay_native_fixture(source, fixture)),
        }
    PACK.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
