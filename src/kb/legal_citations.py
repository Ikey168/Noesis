"""German statutory citations, Federal Law Gazette (BGBl) references and EU act references (#2105, FL06).

One shared parser turns German citation syntax into provision locators for
acquisition (amending instructions, "Stand" notes), court-decision linking and
user queries:

* ``§ 823 Abs. 1 BGB``, ``§ 5 Abs. 2 Satz 1 Nr. 3``, ``Art. 20 Abs. 3 GG``,
  ``§§ 33 ff. WpHG``, ``§§ 5 bis 7``, ``§ 5 f.``, ``§ 5 Absatz 2 Satz 1
  Nummer 3`` (the long forms statutes use), ``Abs 1`` without a period (the
  RII ``norm`` field), ``i.V.m.`` chains, lists joined by ``und``/``,`` and
  ``a.F.``/``n.F.`` markers (also ``aF``/``nF`` and "in der bis zum ...
  geltenden Fassung").
* Statute abbreviations resolve only through the bounded statute set (FL01,
  :data:`FEDERAL_STATUTE_SET`) plus any explicitly supplied statutes; an
  unknown abbreviation is returned ``unresolved`` with the raw text, never
  guessed.
* Every citation keeps its character offsets in the parsed text.

Provision paths are normalised the same way on both sides of every match:
``§33/abs1/satz1/nr3``, ``art20/abs3``, ``§5/abs2/buchst-a``. Statute keys are
the case-folded abbreviation (``wphg``). Identifier boundaries are shared by
the statute, BGBl, ELI and EU-act parsers: a match never starts or ends inside
a word or an identifier path (``/``, ``-``, ``_`` continue an identifier), so
an ELI such as ``eli/bund/bgbl-1/2030/45`` is one token.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

# Shared identifier boundaries: nothing alphanumeric or path-like may touch a match.
IDENT_START = r"(?<![0-9A-Za-zÄÖÜäöüß_/\-])"
IDENT_END = r"(?![0-9A-Za-zÄÖÜäöüß_\-]|/[0-9A-Za-z])"

# FL01 bounded statute set (docs/development/federal-statutes-evidence/source-audit.md).
# Keys are the official abbreviations (jurabk); ``gii_path`` is the per-statute
# directory on gesetze-im-internet.de (verify live); ``names`` are the forms the
# statute's name takes in amending articles (nominative and genitive).
FEDERAL_STATUTE_SET: dict[str, dict[str, Any]] = {
    "BGB": {
        "title": "Bürgerliches Gesetzbuch",
        "gii_path": "bgb",
        "names": [
            "Bürgerliche Gesetzbuch",
            "Bürgerlichen Gesetzbuchs",
            "Bürgerliches Gesetzbuch",
        ],
        "reason": "the civil-law statute most cited by federal court decisions",
    },
    "HGB": {
        "title": "Handelsgesetzbuch",
        "gii_path": "hgb",
        "names": ["Handelsgesetzbuch", "Handelsgesetzbuchs"],
        "reason": "commercial law cited by BGH decisions and by accounting provisions",
    },
    "GmbHG": {
        "title": "Gesetz betreffend die Gesellschaften mit beschränkter Haftung",
        "gii_path": "gmbhg",
        "names": [
            "Gesetz betreffend die Gesellschaften mit beschränkter Haftung",
            "GmbH-Gesetz",
            "GmbH-Gesetzes",
        ],
        "reason": "company law cited by BGH decisions and ownership records",
    },
    "AktG": {
        "title": "Aktiengesetz",
        "gii_path": "aktg",
        "names": ["Aktiengesetz", "Aktiengesetzes"],
        "reason": "stock-corporation law cited by BGH decisions and ownership records",
    },
    "WpHG": {
        "title": "Wertpapierhandelsgesetz",
        "gii_path": "wphg",
        "names": ["Wertpapierhandelsgesetz", "Wertpapierhandelsgesetzes"],
        "reason": "securities trading law BaFin notices rely on (#2106), incl. MAR implementation provisions",
    },
    "KWG": {
        "title": "Gesetz über das Kreditwesen",
        "gii_path": "kredwg",
        "names": [
            "Kreditwesengesetz",
            "Kreditwesengesetzes",
            "Gesetz über das Kreditwesen",
            "Gesetzes über das Kreditwesen",
        ],
        "reason": "banking supervision law BaFin notices rely on (#2106)",
    },
    "VwVfG": {
        "title": "Verwaltungsverfahrensgesetz",
        "gii_path": "vwvfg",
        "names": ["Verwaltungsverfahrensgesetz", "Verwaltungsverfahrensgesetzes"],
        "reason": "administrative procedure law cited by BVerwG decisions and supervisory notices",
    },
    "GG": {
        "title": "Grundgesetz für die Bundesrepublik Deutschland",
        "gii_path": "gg",
        "names": [
            "Grundgesetz",
            "Grundgesetzes",
            "Grundgesetz für die Bundesrepublik Deutschland",
        ],
        "reason": "constitutional provisions (Art.) cited across federal court decisions",
    },
    "ZPO": {
        "title": "Zivilprozessordnung",
        "gii_path": "zpo",
        "names": ["Zivilprozessordnung"],
        "reason": "cited by the BGH decision already in the Legal pack fixtures (§ 13, § 251 ZPO)",
    },
    "InsO": {
        "title": "Insolvenzordnung",
        "gii_path": "inso",
        "names": ["Insolvenzordnung"],
        "reason": "cited by the BGH decision already in the Legal pack fixtures (§ 4 InsO)",
    },
    "VwGO": {
        "title": "Verwaltungsgerichtsordnung",
        "gii_path": "vwgo",
        "names": ["Verwaltungsgerichtsordnung"],
        "reason": "procedural law of the BVerwG decisions already in the Legal pack fixtures",
    },
}

_NUM = r"\d+[a-z]?"
_ROMAN = r"[IVX]+"
_PART_WORDS = [
    ("uabs", r"Unterabsatz|Unterabs\.|UAbs\.?"),
    ("abs", r"Absatz|Absätze|Abs\.?"),
    ("satz", r"Satz|Sätze|S\.(?=\s*\d)"),
    ("hs", r"Halbsatz|Halbs\.|Hs\.|HS"),
    ("nr", r"Nummer|Nrn\.|Nr\.?|Ziffer|Ziff\."),
    ("buchst", r"Buchstabe|Buchst\.|lit\."),
    ("alt", r"Alternative|Variante|Alt\.|Var\."),
]
_PART = "|".join(f"(?P<{name}>{pattern})" for name, pattern in _PART_WORDS)
_PART_RE = re.compile(
    rf"\s*(?:{_PART})\s*(?P<value>{_NUM}|{_ROMAN}|[a-z]{{1,2}}\)?)(?![0-9A-Za-zÄÖÜäöüß_\-])"
)
_HEAD_RE = re.compile(
    IDENT_START
    + r"(?P<head>§§|§|Art\.|Artt\.|Artikel|Art(?=\s+\d))\s*(?P<num>"
    + _NUM
    + r")"
    + IDENT_END
)
_RANGE_RE = re.compile(
    r"\s*(?P<sep>bis|-|–)\s*(?:(?:§|Art\.?|Artikel)\s*)?(?P<num>"
    + _NUM
    + r")"
    + IDENT_END
)
_FF_RE = re.compile(r"\s*(?P<ff>ff\.|f\.)(?![A-Za-z])")
_HINT_RE = re.compile(
    r"\s*(?:(?P<af>a\.\s?F\.|aF)|(?P<nf>n\.\s?F\.|nF))(?![A-Za-z])"
    r"|\s*in\s+der\s+(?P<dir>bis|ab)\s+(?:zum|dem|zu)?\s*(?P<date>\d{1,2}\.\s*(?:\d{1,2}\.|[A-Za-zä]+)\s*\d{4})\s+"
    r"geltenden\s+Fassung"
)
_ABBR_RE = re.compile(
    r"\s*(?:des\s+|der\s+)?(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]{0,15}(?:-[A-Za-zÄÖÜäöüß]+)?(?:\s+(?:[IVX]{1,4}|\d{1,2})(?=\W|$))?)"
    + IDENT_END
)
_JOIN_RE = re.compile(
    r"\s*(?P<join>i\.\s?V\.\s?m\.|iVm|in\s+Verbindung\s+mit|und|sowie|oder|,)\s*(?=§|Art)"
)
_LIST_RE = re.compile(r"\s*(?:,|und|sowie|oder)\s*(?P<num>" + _NUM + r")" + IDENT_END)
_ROMAN_ABSATZ = re.compile(
    r"\s+([IVX]{1,4})(?=\s+(?:[A-ZÄÖÜ§]|S\.|Satz|Nr\.|a\.\s?F\.|n\.\s?F\.))"
)
_ROMAN_VALUES = {
    "I": 1,
    "II": 2,
    "III": 3,
    "IV": 4,
    "V": 5,
    "VI": 6,
    "VII": 7,
    "VIII": 8,
    "IX": 9,
    "X": 10,
}
_MONTHS = {
    "januar": 1,
    "februar": 2,
    "märz": 3,
    "maerz": 3,
    "april": 4,
    "mai": 5,
    "juni": 6,
    "juli": 7,
    "august": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "dezember": 12,
}


def statute_key(abbreviation: Any) -> str:
    """The normalised key of a statute abbreviation (both sides of every match use this)."""
    return re.sub(r"\s+", " ", str(abbreviation or "")).strip().casefold()


def statute_registry(
    extra: Iterable[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """The FL01 statute set plus explicitly supplied statutes, keyed by :func:`statute_key`."""
    registry = {
        statute_key(k): {"jurabk": k, **v} for k, v in FEDERAL_STATUTE_SET.items()
    }
    items = (
        extra.items()
        if isinstance(extra, Mapping)
        else ((None, e) for e in (extra or []))
    )
    for key, entry in items:
        entry = dict(entry)
        jurabk = str(entry.get("jurabk") or key or "")
        if jurabk:
            registry[statute_key(jurabk)] = {
                **registry.get(statute_key(jurabk), {}),
                **entry,
                "jurabk": jurabk,
            }
    return registry


def _norm_number(value: str) -> str:
    return value.strip().casefold()


def provision_path(
    kind: str, number: str, parts: Iterable[tuple[str, str]] = ()
) -> str:
    """``§5a/abs2/satz1`` - the shared provision locator path."""
    head = ("§" if kind == "§" else "art") + _norm_number(number)
    tail = []
    for name, value in parts:
        value = value.strip().rstrip(")").casefold()
        tail.append(f"{name}-{value}" if name == "buchst" else f"{name}{value}")
    return "/".join([head, *tail])


def norm_path(enbez: Any) -> str | None:
    """The provision path of a norm designation as a source states it (``§ 5a`` -> ``§5a``, ``Art 20`` -> ``art20``)."""
    text = " ".join(str(enbez or "").split())
    match = re.fullmatch(r"(§|Art\.?|Artikel)\s*(" + _NUM + r")", text)
    if not match:
        return None
    return provision_path("§" if match.group(1) == "§" else "art", match.group(2))


def path_contains(outer: str, inner: str) -> bool:
    """Whether ``inner`` is ``outer`` or one of its sub-units (``§5`` contains ``§5/abs2``, not ``§50``)."""
    return inner == outer or inner.startswith(outer + "/")


def _stated_date(text: str) -> str | None:
    text = " ".join(text.split())
    numeric = re.fullmatch(r"(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})", text)
    worded = re.fullmatch(r"(\d{1,2})\.\s*([A-Za-zä]+)\s*(\d{4})", text)
    try:
        if numeric:
            return date(
                int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1))
            ).isoformat()
        if worded and worded.group(2).casefold() in _MONTHS:
            return date(
                int(worded.group(3)),
                _MONTHS[worded.group(2).casefold()],
                int(worded.group(1)),
            ).isoformat()
    except ValueError:
        return None
    return None


def _uppercase_count(token: str) -> int:
    return sum(1 for c in token if c.isupper())


def _parse_one(text: str, pos: int) -> tuple[dict[str, Any], int] | None:
    head = _HEAD_RE.match(text, pos)
    if not head:
        return None
    kind = "§" if head.group("head").startswith("§") else "art"
    plural = head.group("head") in {"§§", "Artt."}
    numbers = [head.group("num")]
    end = head.end()
    parts: list[tuple[str, str]] = []
    shape = "single"
    to_number = None
    roman = _ROMAN_ABSATZ.match(text, end)
    if roman and roman.group(1) in _ROMAN_VALUES:
        # "§ 823 II BGB": a Roman numeral right after the norm number is its Absatz (older citation style).
        parts.append(("abs", str(_ROMAN_VALUES[roman.group(1)])))
        end = roman.end()
    while True:
        part = _PART_RE.match(text, end)
        if part:
            name = next(n for n, _ in _PART_WORDS if part.group(n))
            parts.append((name, part.group("value")))
            end = part.end()
            continue
        break
    rng = _RANGE_RE.match(text, end)
    ff = _FF_RE.match(text, end)
    if rng and not parts:
        shape, to_number, end = "range", rng.group("num"), rng.end()
    elif ff:
        shape, end = (
            ("following" if ff.group("ff") == "ff." else "following_one"),
            ff.end(),
        )
    elif plural and not parts:
        while True:
            more = _LIST_RE.match(text, end)
            if not more:
                break
            numbers.append(more.group("num"))
            end = more.end()
    hint, hint_text, hint_date = None, None, None

    def read_hint(at: int) -> int:
        nonlocal hint, hint_text, hint_date
        match = _HINT_RE.match(text, at)
        if not match:
            return at
        hint_text = match.group(0).strip()
        if match.group("af") or match.group("dir") == "bis":
            hint = "a.F."
        else:
            hint = "n.F."
        if match.group("date"):
            hint_date = _stated_date(match.group("date"))
        return match.end()

    end = read_hint(end)
    abbr = _ABBR_RE.match(text, end)
    statute_raw = None
    if abbr and _uppercase_count(abbr.group("abbr").split()[0]) >= 2:
        statute_raw = abbr.group("abbr")
        end = abbr.end()
        end = read_hint(end)
    provisions = []
    for number in numbers:
        provisions.append(
            {
                "path": provision_path(kind, number, parts),
                "shape": shape if len(numbers) == 1 else "single",
            }
        )
    if shape == "range":
        provisions = [
            {
                "path": provision_path(kind, numbers[0]),
                "shape": "range",
                "to_path": provision_path(kind, to_number),
            }
        ]
    citation = {
        "raw": text[pos:end].strip(),
        "start": pos,
        "end": len(text[:end].rstrip()),
        "kind": kind,
        "statute_raw": statute_raw,
        "provisions": provisions,
        "version_hint": hint,
        "version_hint_text": hint_text,
        "version_hint_date": hint_date,
    }
    return citation, end


def parse_citations(
    text: str,
    *,
    statutes: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    context_statute: str | None = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """Every statutory citation in ``text`` with offsets, provision paths, statute resolution and version hints.

    ``context_statute`` resolves citations that name no statute (e.g. inside a
    statute's own text); without it they stay ``no_statute``. A citation in an
    ``i.V.m.``/``und`` chain without its own abbreviation shares the next stated
    statute of the chain (``statute_basis = shared_in_chain``).
    """
    registry = statute_registry(statutes)
    text = str(text or "")
    found: list[dict[str, Any]] = []
    chain: list[dict[str, Any]] = []
    pos = 0
    chain_no = 0
    while pos < len(text) and len(found) < limit:
        start = text.find("§", pos)
        art = re.compile(IDENT_START + r"Art(?:\.|ikel|t\.)?\s*\d").search(text, pos)
        candidates = [
            p
            for p in (start if start >= 0 else None, art.start() if art else None)
            if p is not None
        ]
        if not candidates:
            break
        at = min(candidates)
        parsed = _parse_one(text, at)
        if parsed is None:
            pos = at + 1
            continue
        citation, end = parsed
        if chain and chain[-1].get("_joined_at") != at:
            _close_chain(chain)
            chain = []
        if not chain:
            chain_no += 1
        citation["chain"] = chain_no
        citation["relation"] = "primary" if not chain else chain[-1]["_next_relation"]
        chain.append(citation)
        found.append(citation)
        join = _JOIN_RE.match(text, end)
        if join:
            citation["_joined_at"] = join.end()
            chain[-1]["_joined_at"] = join.end()
            word = join.group("join")
            citation["_next_relation"] = (
                "in_conjunction_with"
                if word.replace(" ", "").casefold()
                in {"i.v.m.", "ivm", "inverbindungmit"}
                else "listed_with"
            )
            pos = join.end()
        else:
            pos = end
    _close_chain(chain)
    for citation in found:
        for key in ("_joined_at", "_next_relation"):
            citation.pop(key, None)
        raw = citation["statute_raw"]
        basis = citation.pop("_statute_basis", "stated" if raw else None)
        if raw is None and context_statute:
            raw, basis = context_statute, "context"
        key = statute_key(raw) if raw else None
        entry = registry.get(key) if key else None
        citation.update(
            {
                "statute": entry["jurabk"] if entry else None,
                "statute_key": key if entry else None,
                "statute_basis": basis,
                "status": "resolved"
                if entry
                else "no_statute"
                if raw is None
                else "unresolved",
            }
        )
        if raw and not entry:
            citation["unresolved_reason"] = (
                "abbreviation is not in the bounded statute set"
            )
        citation["statute_raw"] = citation["statute_raw"] or (
            raw if basis == "shared_in_chain" else None
        )
    return found


def _close_chain(chain: list[dict[str, Any]]) -> None:
    """Chain members without their own abbreviation share the next stated one."""
    following = None
    for citation in reversed(chain):
        if citation["statute_raw"]:
            following = citation["statute_raw"]
        elif following:
            citation["statute_raw"] = following
            citation["_statute_basis"] = "shared_in_chain"


def resolve_citation(
    text: str,
    *,
    statutes: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse a user-supplied citation string: exactly the citations found, resolved or unresolved."""
    citations = parse_citations(text, statutes=statutes)
    status = (
        "resolved"
        if citations and all(c["status"] == "resolved" for c in citations)
        else "partially_resolved"
        if any(c["status"] == "resolved" for c in citations)
        else "unresolved"
        if citations
        else "no_citation"
    )
    return {
        "input": text,
        "status": status,
        "citations": citations,
        "semantics": "syntax only: a resolved statute is one in the bounded statute set; nothing here says the "
        "provision exists in a version or is in force",
    }


# --------------------------------------------------------------------- BGBl

_PART_ROMAN = {"I": 1, "II": 2}
_BGBL_PATTERNS = [
    # Digital BGBl (since 2023): "BGBl. 2023 I Nr. 411"
    re.compile(
        IDENT_START
        + r"BGBl\.?\s*(?P<year>\d{4})\s+(?P<part>II|I)\s+Nr\.?\s*(?P<number>\d+)"
        + IDENT_END
    ),
    # DIP style: "BGBl I 2023 Nr. 411"
    re.compile(
        IDENT_START
        + r"BGBl\.?\s*(?P<part>II|I)\s+(?P<year>\d{4})\s+Nr\.?\s*(?P<number>\d+)"
        + IDENT_END
    ),
    # Legacy with year: "BGBl. I 2002, 42" / "BGBl. I 2002 S. 42"
    re.compile(
        IDENT_START
        + r"BGBl\.?\s*(?P<part>II|I)\s+(?P<year>\d{4}),?\s+(?:S\.\s*)?(?P<page>\d+)"
        + IDENT_END
    ),
    # Legacy page only: "BGBl. I S. 2708" (year from a preceding "vom <date>" when stated)
    re.compile(
        IDENT_START + r"BGBl\.?\s*(?P<part>II|I)\s+S\.\s*(?P<page>\d+)" + IDENT_END
    ),
    # "Stand" notes: "G v. 14.3.2030 I Nr. 45" / "Bek. v. 2.1.2002 I 42"
    re.compile(
        IDENT_START
        + r"v\.\s*(?P<day>\d{1,2})\.(?P<month>\d{1,2})\.(?P<year>\d{4})\s+(?P<part>II|I)\s+"
        r"(?:Nr\.\s*(?P<number>\d+)|(?P<page>\d+))" + IDENT_END
    ),
]
_VOM = re.compile(r"vom\s+(\d{1,2}\.\s*(?:\d{1,2}\.|[A-Za-zä]+)\s*\d{4})\s*\(?\s*$")


def bgbl_key(part: Any, year: Any, number: Any = None, page: Any = None) -> str | None:
    """``bgbl-1/2030/nr-45`` (digital) or ``bgbl-1/2002/s-42`` (legacy page); ``None`` without a year."""
    try:
        part_no = (
            _PART_ROMAN[str(part).strip().upper()]
            if str(part).strip().upper() in _PART_ROMAN
            else int(str(part).strip())
        )
        year_no = int(str(year))
    except (TypeError, ValueError, KeyError):
        return None
    if part_no not in (1, 2):
        return None
    if number not in (None, ""):
        return f"bgbl-{part_no}/{year_no}/nr-{int(str(number))}"
    if page not in (None, ""):
        return f"bgbl-{part_no}/{year_no}/s-{int(str(page))}"
    return None


def parse_bgbl_references(text: str) -> list[dict[str, Any]]:
    """Every BGBl reference with offsets and a normalised key (``None`` when the year is not stated)."""
    text = str(text or "")
    spans: list[tuple[int, int]] = []
    output = []
    for pattern in _BGBL_PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < e and s < match.end() for s, e in spans):
                continue
            groups = match.groupdict()
            year = groups.get("year")
            year_basis = "stated" if year else None
            if not year:
                prior = _VOM.search(text[max(0, match.start() - 60) : match.start()])
                stated = _stated_date(prior.group(1)) if prior else None
                if stated:
                    year, year_basis = stated[:4], "promulgation_date_stated_before"
            spans.append((match.start(), match.end()))
            output.append(
                {
                    "raw": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                    "part": "II" if groups["part"] == "II" else "I",
                    "year": int(year) if year else None,
                    "number": int(groups["number"]) if groups.get("number") else None,
                    "page": int(groups["page"]) if groups.get("page") else None,
                    "year_basis": year_basis,
                    "key": bgbl_key(
                        groups["part"], year, groups.get("number"), groups.get("page")
                    ),
                }
            )
    return sorted(output, key=lambda r: r["start"])


# ------------------------------------------------------------------ EU acts

_EU_ACT = re.compile(
    IDENT_START
    + r"(?P<type>(?:Delegierte[nr]?\s+|Durchführungs)?(?:Richtlinie|Verordnung|Beschluss|Beschlusses|Richtlinien))"
    r"\s+(?:(?:\((?P<org1>EU|EG|EWG|Euratom)\)\s*(?:Nr\.\s*)?(?P<a>\d{1,4})/(?P<b>\d{1,4}))"
    r"|(?:(?P<c>\d{2,4})/(?P<d>\d{1,4})/(?P<org2>EU|EG|EWG)))" + IDENT_END
)
_ELI_EU = re.compile(
    IDENT_START
    + r"(?:https?://data\.europa\.eu/)?eli/(?P<type>dir|reg|dec|reg_impl|reg_del|dir_impl"
    r"|dir_del)/(?P<year>\d{4})/(?P<number>\d+)(?:/[A-Za-z0-9_.-]+)*" + IDENT_END
)
IMPLEMENTATION_MARKERS = re.compile(
    r"(?:dient|dienen)\s+der\s+(?:Umsetzung|Durchführung)|zur\s+Umsetzung\s+der|"
    r"Umsetzung\s+der\s+Richtlinie|Durchführung\s+der\s+Verordnung",
    re.I,
)


def _celex(kind: str, year: int, number: int) -> str:
    return f"3{year:04d}{kind}{number:04d}"


def parse_eu_act_references(text: str) -> list[dict[str, Any]]:
    """EU directives/regulations/decisions named in German text, with the CELEX number each form implies."""
    text = str(text or "")
    output = []
    for match in _EU_ACT.finditer(text):
        word = match.group("type")
        kind = "L" if "Richtlinie" in word else "R" if "Verordnung" in word else "D"
        if match.group("a"):
            first, second = int(match.group("a")), int(match.group("b"))
            numbered = "Nr." in match.group(0)
            # "(EU) 2019/1234" is year/number since 2015; "(EU) Nr. 596/2014" and "(EG) Nr. 1060/2009" are
            # number/year.
            year, number = (
                (second, first) if numbered or first < 1950 else (first, second)
            )
        else:
            year, number = int(match.group("c")), int(match.group("d"))
            if year < 100:
                year += 1900 if year > 50 else 2000
        output.append(
            {
                "raw": match.group(0),
                "start": match.start(),
                "end": match.end(),
                "act_type": kind,
                "year": year,
                "number": number,
                "celex": _celex(kind, year, number),
            }
        )
    for match in _ELI_EU.finditer(text):
        kind = {"dir": "L", "dir_impl": "L", "dir_del": "L", "dec": "D"}.get(
            match.group("type"), "R"
        )
        year, number = int(match.group("year")), int(match.group("number"))
        output.append(
            {
                "raw": match.group(0),
                "start": match.start(),
                "end": match.end(),
                "act_type": kind,
                "year": year,
                "number": number,
                "celex": _celex(kind, year, number),
                "eli": match.group(0).split("data.europa.eu/")[-1],
            }
        )
    return sorted(output, key=lambda r: r["start"])


def implementation_statements(text: str) -> list[dict[str, Any]]:
    """EU acts a sentence states the text implements ("dient der Umsetzung der Richtlinie (EU) 2019/…")."""
    text = str(text or "")
    output = []
    for marker in IMPLEMENTATION_MARKERS.finditer(text):
        window_end = _sentence_end(text, marker.end())
        for ref in parse_eu_act_references(text[marker.start() : window_end]):
            output.append(
                {
                    **ref,
                    "start": ref["start"] + marker.start(),
                    "end": ref["end"] + marker.start(),
                    "statement": " ".join(
                        text[_sentence_start(text, marker.start()) : window_end].split()
                    ),
                }
            )
    unique = {}
    for item in output:
        unique.setdefault((item["celex"], item["start"]), item)
    return sorted(unique.values(), key=lambda r: r["start"])


_ABBREVIATION_DOTS = re.compile(
    r"(?:Nr|Abs|Art|Buchst|bzw|vgl|ABl|S|L|z\.B|u\.a|i\.V\.m|Bek|G|v)\.$"
)


def _sentence_end(text: str, pos: int) -> int:
    for match in re.finditer(r"\.(?=\s+[A-ZÄÖÜ]|\s*$)", text[pos:]):
        before = text[: pos + match.start() + 1]
        token = before.rsplit(None, 1)[-1] if before.split() else ""
        # "Nr.", "Abs." ... and day numbers of dates ("1. März") never end a sentence.
        if _ABBREVIATION_DOTS.search(token) or re.fullmatch(r"\(?\d{1,2}\.", token):
            continue
        return pos + match.end()
    return len(text)


def _sentence_start(text: str, pos: int) -> int:
    cut = max(text.rfind(". ", 0, pos), text.rfind("\n", 0, pos))
    return 0 if cut < 0 else cut + 1
