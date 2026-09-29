"""Parsers for German federal law sources (#2105): gesetze-im-internet.de, rechtsinformationen.bund.de, recht.bund.de.

Each parser reads one provider's documented format and returns
``noesis-native-regional-v1`` records the Legal store projects:

* :func:`parse_gii_toc` / :func:`parse_gii_statute` - the gesetze-im-internet.de
  table of contents (``gii-toc.xml``) and one statute's XML (``gii-norm.dtd``:
  ``dokumente``/``norm``/``metadaten``/``textdaten``) in its ``xml.zip``. The
  text is the current consolidation only; each fetch is an *observed* version.
  "Stand" notes are kept verbatim and never turned into validity dates.
* :func:`parse_ris_search` / :func:`parse_legaldocml_statute` - the federal legal
  information portal's JSON-LD search results (one ``workExample`` expression
  per member, ELI identifiers, ``temporalCoverage``) and one expression's
  LegalDocML.de (Akoma Ntoso) text; each is a *source-stated* version.
* :func:`parse_bgbl_feed` / :func:`parse_bgbl_act` - the digital Federal Law
  Gazette listing (RSS/Atom items naming the promulgation) and one
  promulgated act in LegalDocML.de: BGBl citation, promulgation date, title,
  the entry-into-force article verbatim, amending instructions parsed into
  target statute and provision where unambiguous, and implementation
  statements. Instructions are never applied to produce a consolidated text.

Provisions carry the shared locator path of :mod:`src.kb.legal_citations`
(``§5/abs2``), so observed and source-stated versions compare by the same key.
Every shape documented here is unverified against the live services; see
``docs/development/federal-statutes-evidence/source-audit.md``.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any
from urllib.parse import urlsplit

import defusedxml.ElementTree as ET

from src.kb.legal_citations import (
    bgbl_key,
    norm_path,
    parse_citations,
    statute_key,
)

REGIONAL_CONTRACT = "noesis-native-regional-v1"
GII_PROVIDER = "gesetze-im-internet"
RIS_PROVIDER = "rechtsinformationen-bund"
BGBL_PROVIDER = "recht-bund"
FEDERAL_PROVIDERS = (GII_PROVIDER, RIS_PROVIDER, BGBL_PROVIDER)
DIGITAL_BGBL_FROM_YEAR = 2023
MAX_XML_BYTES = 30_000_000
COVERAGE = {
    GII_PROVIDER: "Current consolidated text of the selected federal statutes as observed on the fetch date; "
    "no validity interval is stated by the source",
    RIS_PROVIDER: "Versions of the selected federal statutes with the validity interval the portal states",
    BGBL_PROVIDER: "Acts promulgated in the digital Federal Law Gazette (since 2023) that amend a selected statute",
}


class FormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _local(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _xml(raw: bytes):
    if not isinstance(raw, (bytes, bytearray)) or not 0 < len(raw) <= MAX_XML_BYTES:
        raise FormatError("input_limit", "XML input is missing or oversized")
    try:
        return ET.fromstring(bytes(raw), forbid_entities=True, forbid_external=True)
    except ET.ParseError as exc:
        raise FormatError("schema_drift", f"not well-formed XML: {exc}") from exc


def _iso(value: Any) -> str | None:
    """A source date as ISO ``YYYY-MM-DD``; anything else is absent (never the string "None")."""
    text = _text(value)
    for pattern, order in (
        (r"(\d{4})-(\d{2})-(\d{2})", (1, 2, 3)),
        (r"(\d{4})(\d{2})(\d{2})", (1, 2, 3)),
        (r"(\d{1,2})\.(\d{1,2})\.(\d{4})", (3, 2, 1)),
    ):
        match = re.fullmatch(
            pattern, text[:10] if pattern.startswith(r"(\d{4})-") else text
        )
        if match:
            try:
                return date(
                    int(match.group(order[0])),
                    int(match.group(order[1])),
                    int(match.group(order[2])),
                ).isoformat()
            except ValueError:
                return None
    return None


def _record(
    provider: str,
    identity: str,
    title: str,
    *,
    source_url: str,
    kind: str,
    fields: Mapping[str, Any],
    sections: Sequence[Mapping[str, Any]],
    native: Mapping[str, Any],
    published_at: str | None = None,
) -> dict[str, Any]:
    if not identity:
        raise FormatError("source_identity", "record has no stable source identity")
    fields = {k: v for k, v in dict(fields).items()}
    return {
        "contract": REGIONAL_CONTRACT,
        "provider": provider,
        "provider_id": identity,
        "kind": kind,
        "title": _text(title) or identity,
        "source_url": source_url,
        "language": "de",
        "published_at": published_at,
        "updated_at": None,
        "fields": fields,
        "missing_fields": sorted(k for k, v in fields.items() if v in (None, "", [])),
        "sections": list(sections),
        "relationships": [],
        "native": dict(native),
        "coverage_notice": COVERAGE[provider],
        "review_required": False,
        "is_current_law": None,
    }


# ----------------------------------------------------------- provisions

_ABSATZ = re.compile(r"^\((\d+[a-z]?)\)\s*")


def _provision_sections(
    norms: Sequence[Mapping[str, Any]], *, statute: str
) -> list[dict[str, Any]]:
    """One section per Absatz of each norm (or one per norm without Absätze), plus headings and footnotes.

    Every paragraph is scoped to the norm it appears in; a paragraph that only
    states a norm designation (``§ 6``) opens that norm, so headings written as
    plain paragraphs never merge two norms.
    """
    sections: list[dict[str, Any]] = []
    for norm in norms:
        enbez = _text(norm.get("enbez"))
        path = norm_path(enbez)
        base = {
            "statute": statute,
            "official_norm_id": enbez or None,
            "heading": norm.get("heading") or None,
            "gliederung": list(norm.get("gliederung") or []),
            "source_norm_id": norm.get("source_norm_id"),
        }
        if norm.get("kind") == "heading":
            sections.append(
                {
                    "text": _text(
                        " ".join(
                            filter(
                                None,
                                [
                                    norm.get("gliederungsbez"),
                                    norm.get("gliederungstitel"),
                                ],
                            )
                        )
                    ),
                    "locator": {
                        **base,
                        "kind": "statute-heading",
                        "path": "gliederung/"
                        + (
                            norm.get("gliederungskennzahl")
                            or str(norm.get("source_norm_id") or "")
                        ),
                    },
                }
            )
            continue
        if path is None:
            # Norms without a § / Art. designation (Eingangsformel, Inhaltsübersicht, Anlagen) keep their own path.
            label = (
                re.sub(r"[^0-9a-zäöüß]+", "-", enbez.casefold()).strip("-") or "norm"
            )
            path = f"norm:{label}:{norm.get('source_norm_id') or len(sections)}"
        current: dict[str, Any] | None = None
        for paragraph in norm.get("paragraphs") or []:
            text = _text(paragraph)
            if not text:
                continue
            absatz = _ABSATZ.match(text)
            if absatz:
                current = {
                    "text": text,
                    "locator": {
                        **base,
                        "kind": "statute-provision",
                        "path": f"{path}/abs{absatz.group(1)}",
                        "paragraph_number": absatz.group(1),
                    },
                }
                sections.append(current)
            elif current is not None:
                current["text"] += "\n" + text
            else:
                current = {
                    "text": text,
                    "locator": {**base, "kind": "statute-provision", "path": path},
                }
                sections.append(current)
        for index, footnote in enumerate(norm.get("footnotes") or []):
            if _text(footnote):
                sections.append(
                    {
                        "text": _text(footnote),
                        "locator": {
                            **base,
                            "kind": "statute-footnote",
                            "path": f"{path}/fn{index + 1}",
                        },
                    }
                )
    return sections


# ------------------------------------------------------------- gesetze-im-internet.de


def parse_gii_toc(raw: bytes) -> list[dict[str, str]]:
    """``[{title, link, path}]`` from ``gii-toc.xml``; ``path`` is the statute directory (``bgb``)."""
    root = _xml(raw)
    if _local(root.tag) != "items":
        raise FormatError("schema_drift", "expected the gii-toc.xml <items> root")
    items = []
    for item in root:
        if _local(item.tag) != "item":
            continue
        title = _text(item.findtext("title"))
        link = _text(item.findtext("link"))
        parts = urlsplit(link)
        match = re.fullmatch(r"/([A-Za-z0-9_.-]+)/xml\.zip", parts.path)
        if not link or not match:
            continue
        items.append(
            {
                "title": title,
                "link": link,
                "host": (parts.hostname or "").casefold(),
                "path": match.group(1).casefold(),
            }
        )
    return items


def unzip_single_xml(raw: bytes, *, limit: int = MAX_XML_BYTES) -> bytes:
    if not raw.startswith(b"PK"):
        return raw
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [
            m
            for m in archive.infolist()
            if not m.is_dir() and m.filename.casefold().endswith(".xml")
        ]
        if (
            len(members) != 1
            or ".." in members[0].filename
            or members[0].filename.startswith("/")
            or members[0].file_size > limit
            or members[0].flag_bits & 1
        ):
            raise FormatError(
                "unsupported_archive",
                "statute download must contain exactly one XML member",
            )
        with archive.open(members[0]) as stream:
            return stream.read(limit + 1)


def _content_paragraphs(node) -> list[str]:
    """Paragraph texts of a gii ``Content`` element (``P`` blocks; lists flattened into their paragraph)."""
    if node is None:
        return []
    output = []
    for child in node:
        if _local(child.tag) in {"P", "p"}:
            output.append(_text("".join(child.itertext())))
        elif _local(child.tag) in {"DL", "table", "Table", "pre"}:
            text = _text(" ".join(child.itertext()))
            if output and text:
                output[-1] += " " + text
            elif text:
                output.append(text)
    return [p for p in output if p]


def parse_gii_statute(raw: bytes, *, source_url: str, jurabk: str) -> dict[str, Any]:
    """One observed statute version from a gii ``dokumente`` XML (the selected ``jurabk`` must match)."""
    xml = unzip_single_xml(raw)
    root = _xml(xml)
    if _local(root.tag) != "dokumente":
        raise FormatError("schema_drift", "expected the gii <dokumente> root")
    norms = [n for n in root if _local(n.tag) == "norm"]
    if not norms:
        raise FormatError("empty_source", "statute has no norms")
    header = norms[0].find("metadaten")
    if header is None:
        raise FormatError("schema_drift", "statute header norm has no metadata")
    stated_jurabk = _text(header.findtext("jurabk"))
    if statute_key(stated_jurabk) != statute_key(jurabk):
        raise FormatError(
            "source_identity",
            f"statute download states jurabk {stated_jurabk!r}, not {jurabk!r}",
        )
    doknr = _text(root.get("doknr") or norms[0].get("doknr"))
    stand = []
    for item in header.findall("standangabe"):
        stand.append(
            {
                "type": _text(item.findtext("standtyp")) or None,
                "comment": _text(item.findtext("standkommentar")) or None,
                "checked": item.get("checked"),
            }
        )
    fundstellen = [
        {
            "periodical": _text(f.findtext("periodikum")) or None,
            "citation": _text(f.findtext("zitstelle")) or None,
            "type": f.get("typ"),
        }
        for f in header.findall("fundstelle")
    ]
    parsed_norms = []
    gliederung: list[str] = []
    for norm in norms:
        meta = norm.find("metadaten")
        if meta is None:
            continue
        unit = meta.find("gliederungseinheit")
        enbez = _text(meta.findtext("enbez"))
        textdaten = norm.find("textdaten")
        content = textdaten.find("text/Content") if textdaten is not None else None
        footnotes = (
            _content_paragraphs(textdaten.find("fussnoten/Content"))
            if textdaten is not None
            else []
        )
        if unit is not None and not enbez:
            label = _text(
                " ".join(
                    filter(
                        None,
                        [
                            unit.findtext("gliederungsbez"),
                            unit.findtext("gliederungstitel"),
                        ],
                    )
                )
            )
            gliederung = [label]
            parsed_norms.append(
                {
                    "kind": "heading",
                    "gliederungskennzahl": _text(unit.findtext("gliederungskennzahl")),
                    "gliederungsbez": _text(unit.findtext("gliederungsbez")),
                    "gliederungstitel": _text(unit.findtext("gliederungstitel")),
                    "source_norm_id": norm.get("doknr"),
                }
            )
            if footnotes:
                parsed_norms.append(
                    {
                        "kind": "norm",
                        "enbez": "",
                        "paragraphs": [],
                        "footnotes": footnotes,
                        "source_norm_id": norm.get("doknr"),
                        "gliederung": list(gliederung),
                    }
                )
            continue
        if norm is norms[0]:
            if footnotes:
                parsed_norms.append(
                    {
                        "kind": "norm",
                        "enbez": "Fußnoten zum Gesetz",
                        "paragraphs": [],
                        "footnotes": footnotes,
                        "source_norm_id": norm.get("doknr"),
                    }
                )
            continue
        paragraphs = _content_paragraphs(content)
        if not enbez and not paragraphs and not footnotes:
            continue
        parsed_norms.append(
            {
                "kind": "norm",
                "enbez": enbez,
                "heading": _text(meta.findtext("titel")) or None,
                "paragraphs": paragraphs,
                "footnotes": footnotes,
                "source_norm_id": norm.get("doknr"),
                "gliederung": list(gliederung),
            }
        )
    sections = _provision_sections(parsed_norms, statute=stated_jurabk)
    if not sections:
        raise FormatError("empty_source", "statute has no readable provisions")
    text_sha = hashlib.sha256(
        "\n".join(f"{s['locator']['path']}\t{s['text']}" for s in sections).encode()
    ).hexdigest()
    fields = {
        "statute_key": statute_key(stated_jurabk),
        "jurabk": stated_jurabk,
        "amtabk": _text(header.findtext("amtabk")) or None,
        "long_title": _text(header.findtext("langue")) or None,
        "short_title": _text(header.findtext("kurzue")) or None,
        "ausfertigung_date": _iso(header.findtext("ausfertigung-datum")),
        "fundstellen": fundstellen,
        "stand": stand,
        "doknr": doknr or None,
        "builddate": _text(root.get("builddate")) or None,
        "validity_basis": "observed",
        "text_sha256": text_sha,
    }
    return _record(
        GII_PROVIDER,
        f"gii:{statute_key(stated_jurabk)}",
        fields["long_title"] or stated_jurabk,
        source_url=source_url,
        kind="statute-version",
        fields=fields,
        sections=sections,
        native={
            "xml_sha256": hashlib.sha256(xml).hexdigest(),
            "original_sha256": hashlib.sha256(raw).hexdigest(),
        },
    )


# ------------------------------------------------------------- LegalDocML.de (RIS and BGBl)


def _akn_blocks(body) -> list[dict[str, Any]]:
    """Flatten an Akoma Ntoso body into norms: ``{enbez, heading, paragraphs}`` scoped to their article/section.

    Structured ``article``/``section`` elements open norms; a plain paragraph
    whose whole text is a norm designation (``§ 6``) or an article heading
    (``Artikel 2``) also opens one, so flattened documents are scoped the same.
    """
    norms: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def open_norm(
        enbez: str, heading: str | None, element_id: str | None
    ) -> dict[str, Any]:
        norm = {
            "kind": "norm",
            "enbez": enbez,
            "heading": heading,
            "paragraphs": [],
            "footnotes": [],
            "source_norm_id": element_id,
            "points": [],
        }
        norms.append(norm)
        return norm

    def walk(node, points: tuple[str, ...]) -> None:
        nonlocal current
        name = _local(node.tag)
        if name in {"article", "section"} and node.find("./{*}num") is not None:
            number = _text("".join(node.find("./{*}num").itertext()))
            heading_node = node.find("./{*}heading")
            heading = (
                _text("".join(heading_node.itertext()))
                if heading_node is not None
                else None
            )
            if norm_path(number) or re.fullmatch(r"Artikel\s+\d+[a-z]?", number):
                current = open_norm(number, heading, node.get("eId"))
                for child in node:
                    if _local(child.tag) not in {"num", "heading"}:
                        walk(child, ())
                return
        if name == "point":
            num = node.find("./{*}num")
            label = _text("".join(num.itertext())) if num is not None else ""
            for child in node:
                if _local(child.tag) != "num":
                    walk(child, (*points, label))
            return
        if name == "paragraph":
            num = node.find("./{*}num")
            label = _text("".join(num.itertext())) if num is not None else ""
            first = True
            for child in node:
                if _local(child.tag) == "num":
                    continue
                before = len(current["paragraphs"]) if current else 0
                walk(child, points)
                if current and first and label and len(current["paragraphs"]) > before:
                    current["paragraphs"][before] = (
                        f"{label} {current['paragraphs'][before]}"
                    )
                    first = False
            return
        if name == "p":
            text = _text("".join(node.itertext()))
            if not text:
                return
            if norm_path(text) or re.fullmatch(r"Artikel\s+\d+[a-z]?", text):
                current = open_norm(text, None, node.get("eId"))
                return
            if current is None:
                current = open_norm("", None, None)
            if (
                current["heading"] is None
                and not current["paragraphs"]
                and current["enbez"]
                and re.fullmatch(r"Artikel\s+\d+[a-z]?", current["enbez"])
                and not points
                and not re.search(r"\b(wird|werden|ist|sind|tritt|treten)\b", text)
            ):
                current["heading"] = (
                    text  # a heading written as the article's first plain paragraph
                )
                return
            current["paragraphs"].append(text)
            current["points"].append(points)
            return
        for child in node:
            walk(child, points)

    walk(body, ())
    return norms


def _frbr(root, level: str, element: str) -> list[tuple[str | None, str | None]]:
    output = []
    for node in root.iter():
        if _local(node.tag) == level:
            for child in node:
                if _local(child.tag) == element:
                    output.append(
                        (child.get("name"), child.get("value") or child.get("date"))
                    )
    return output


def _find(root, name: str):
    for node in root.iter():
        if _local(node.tag) == name:
            return node
    return None


def parse_ris_search(
    raw: bytes, *, jurabk: str, eli_work: str | None = None
) -> list[dict[str, Any]]:
    """Expressions of one statute from the portal's JSON-LD search results (exact abbreviation match only)."""
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise FormatError("schema_drift", "search response is not JSON") from exc
    members = payload.get("member") if isinstance(payload, Mapping) else None
    if not isinstance(members, list):
        raise FormatError("schema_drift", "search response has no member list")
    output = []
    for member in members:
        item = dict(member.get("item") or member) if isinstance(member, Mapping) else {}
        if statute_key(item.get("abbreviation")) != statute_key(jurabk):
            continue
        work = _text(item.get("legislationIdentifier"))
        if eli_work and work != eli_work:
            continue
        examples = item.get("workExample")
        for example in (
            examples if isinstance(examples, list) else [examples] if examples else []
        ):
            example = dict(example)
            eli = _text(example.get("legislationIdentifier"))
            coverage = _text(example.get("temporalCoverage"))
            encodings = example.get("encoding") or []
            xml_url = next(
                (
                    _text(e.get("contentUrl"))
                    for e in encodings
                    if isinstance(e, Mapping)
                    and "xml" in str(e.get("encodingFormat") or "")
                ),
                None,
            )
            if not eli or not xml_url:
                continue
            start, _, end = coverage.partition("/")
            output.append(
                {
                    "eli_work": work or None,
                    "eli_expression": eli,
                    "temporal_coverage": coverage or None,
                    "validity_from": _iso(start),
                    "validity_to": None if end in {"", ".."} else _iso(end),
                    "xml_url": xml_url,
                    "name": _text(item.get("name")) or None,
                    "abbreviation": _text(item.get("abbreviation")),
                    "date_modified": _iso(
                        example.get("dateModified") or item.get("dateModified")
                    ),
                    "legal_force_statement": _text(example.get("legislationLegalForce"))
                    or None,
                }
            )
    return output


def parse_legaldocml_statute(
    raw: bytes, *, expression: Mapping[str, Any], jurabk: str, source_url: str
) -> dict[str, Any]:
    """One source-stated version: LegalDocML.de text mapped to the same provision paths as observed versions."""
    root = _xml(raw)
    if _local(root.tag) != "akomaNtoso":
        raise FormatError(
            "schema_drift", "expected an Akoma Ntoso (LegalDocML.de) document"
        )
    body = _find(root, "body")
    if body is None:
        raise FormatError("empty_source", "LegalDocML document has no body")
    expressions = [v for _, v in _frbr(root, "FRBRExpression", "FRBRthis") if v]
    stated = str(expression["eli_expression"])
    if expressions and not any(
        v == stated or v.startswith(stated + "/") for v in expressions
    ):
        raise FormatError(
            "source_identity",
            "LegalDocML expression ELI differs from the selected expression",
        )
    sections = _provision_sections(_akn_blocks(body), statute=jurabk)
    if not sections:
        raise FormatError(
            "empty_source", "LegalDocML document has no readable provisions"
        )
    amended_by = sorted(
        {
            key
            for key in (
                bgbl_key_from_eli(_text(node.get("href")))
                for node in root.iter()
                if _local(node.tag) == "passiveRef"
            )
            if key
        }
    )
    text_sha = hashlib.sha256(
        "\n".join(f"{s['locator']['path']}\t{s['text']}" for s in sections).encode()
    ).hexdigest()
    fields = {
        "statute_key": statute_key(jurabk),
        "jurabk": jurabk,
        "long_title": expression.get("name"),
        "eli_work": expression.get("eli_work"),
        "eli_expression": stated,
        "validity_basis": "source_stated",
        "validity_from": expression.get("validity_from"),
        "validity_to": expression.get("validity_to"),
        "temporal_coverage": expression.get("temporal_coverage"),
        "source_modified": expression.get("date_modified"),
        "amended_by": amended_by,
        "text_sha256": text_sha,
    }
    return _record(
        RIS_PROVIDER,
        stated,
        expression.get("name") or jurabk,
        source_url=source_url,
        kind="statute-version",
        fields=fields,
        sections=sections,
        native={
            "xml_sha256": hashlib.sha256(raw).hexdigest(),
            "legal_force_statement": expression.get("legal_force_statement"),
        },
    )


def bgbl_key_from_eli(eli: str) -> str | None:
    """``eli/bund/bgbl-1/2030/45`` -> ``bgbl-1/2030/nr-45``; legacy ``.../2002/s42`` -> ``bgbl-1/2002/s-42``."""
    match = re.search(
        r"(?:^|/)eli/bund/bgbl-([12])/(\d{4})/(s?)(\d+)(?=/|$)", str(eli or "")
    )
    if not match:
        return None
    if match.group(3):
        return bgbl_key(match.group(1), match.group(2), page=match.group(4))
    return bgbl_key(match.group(1), match.group(2), number=match.group(4))


# ------------------------------------------------------------- recht.bund.de (digital BGBl)


def parse_bgbl_feed(raw: bytes) -> list[dict[str, Any]]:
    """Promulgations listed by an RSS 2.0 or Atom feed: ``[{part, year, number, key, title, link}]``."""
    root = _xml(raw)
    output = []
    for item in root.iter():
        if _local(item.tag) not in {"item", "entry"}:
            continue
        texts = {}
        for child in item:
            name = _local(child.tag)
            texts.setdefault(
                name,
                _text(
                    child.get("href")
                    if name == "link" and child.get("href")
                    else "".join(child.itertext())
                ),
            )
        candidate = " ".join(texts.get(k, "") for k in ("link", "guid", "id"))
        match = re.search(
            r"eli/bund/bgbl-([12])/(\d{4})/(\d+)", candidate
        ) or re.search(r"/bgbl/([12])/(\d{4})/(\d+)(?:/|$)", candidate)
        if not match:
            continue
        key = bgbl_key(match.group(1), match.group(2), number=match.group(3))
        output.append(
            {
                "part": int(match.group(1)),
                "year": int(match.group(2)),
                "number": int(match.group(3)),
                "key": key,
                "title": texts.get("title") or None,
                "link": texts.get("link") or None,
                "listed_at": texts.get("pubDate") or texts.get("updated") or None,
            }
        )
    unique = {}
    for entry in output:
        unique.setdefault(entry["key"], entry)
    return list(unique.values())


_ACTIONS = (
    ("aufgehoben", "repeal"),
    ("eingefügt", "insert"),
    ("angefügt", "append"),
    ("neu gefasst", "replace"),
    ("gefasst", "replace"),
    ("ersetzt", "replace_words"),
    ("gestrichen", "delete_words"),
    ("geändert", "amend"),
)
_REL_PARTS = re.compile(
    r"^(?:In\s+|Dem\s+|Der\s+|Die\s+|Das\s+)?((?:(?:Absatz|Satz|Nummer|Buchstabe)\s+\d*[a-z]?\s*)+)"
)


def _statute_in(
    text: str, statutes: Sequence[Mapping[str, Any]]
) -> dict[str, Any] | None:
    for statute in statutes:
        names = [
            statute["jurabk"],
            *(statute.get("names") or []),
            statute.get("title") or "",
        ]
        for name in sorted({n for n in names if n}, key=len, reverse=True):
            pattern = (
                r"\(" + re.escape(name) + r"\)"
                if name == statute["jurabk"]
                else r"(?<![\wäöüß])" + re.escape(name) + r"(?:es|s)?(?![\wäöüß])"
            )
            if re.search(pattern, text):
                return dict(statute)
    return None


def _action(text: str) -> str:
    return next((action for word, action in _ACTIONS if word in text), "unspecified")


def _relative(parent: str, text: str) -> str | None:
    match = _REL_PARTS.match(text)
    if not match:
        return None
    path = parent
    for word, value in re.findall(
        r"(Absatz|Satz|Nummer|Buchstabe)\s+(\d*[a-z]?)", match.group(1)
    ):
        if not value:
            return None
        name = {"Absatz": "abs", "Satz": "satz", "Nummer": "nr", "Buchstabe": "buchst"}[
            word
        ]
        path += f"/{name}-{value}" if name == "buchst" else f"/{name}{value}"
    return path


def _instruction(
    clause: str, statute: Mapping[str, Any] | None, parent: str | None
) -> dict[str, Any]:
    """Target provision of one amending instruction (the clause before its new wording)."""
    # New wording is quoted („…“); only the instruction around it names the target and the action.
    bare = re.sub(r"„[^“]*“", "„…“", clause)
    head = bare.split(":", 1)[0]
    action = _action(head)
    if statute is None:
        return {"status": "statute_not_in_set", "provision": None, "action": action}
    if re.search(r"Inhaltsübersicht|Inhaltsverzeichnis|\bAnlage\b", head):
        return {
            "status": "ambiguous",
            "provision": None,
            "action": action,
            "reason": "the instruction changes the table of contents or an annex, not a provision",
        }
    citations = parse_citations(
        head,
        context_statute=statute["jurabk"],
        statutes=[{"jurabk": statute["jurabk"]}],
    )
    citations = [c for c in citations if c["status"] == "resolved"]
    if citations:
        chosen = (
            citations[-1]
            if action in {"insert", "append"} and len(citations) > 1
            else citations[0]
        )
        provision = chosen["provisions"][0]
        if provision["shape"] != "single" or len(chosen["provisions"]) > 1:
            return {
                "status": "ambiguous",
                "provision": None,
                "action": action,
                "reason": "the instruction names a range or list of provisions",
            }
        return {"status": "resolved", "provision": provision["path"], "action": action}
    if parent:
        relative = _relative(parent, head.strip())
        if relative:
            return {"status": "resolved", "provision": relative, "action": action}
    return {
        "status": "ambiguous",
        "provision": None,
        "action": action,
        "reason": "no single provision is named in the instruction",
    }


_POINT = re.compile(r"^(?:(\d+[a-z]?)\.|([a-z]{1,2})\))\s+")


def parse_bgbl_act(
    raw: bytes, *, key: str, statutes: Sequence[Mapping[str, Any]], source_url: str
) -> dict[str, Any]:
    """One promulgated act: citation, dates, entry-into-force text, amending instructions and statements."""
    root = _xml(raw)
    if _local(root.tag) != "akomaNtoso":
        raise FormatError(
            "schema_drift", "expected an Akoma Ntoso (LegalDocML.de) promulgation"
        )
    body = _find(root, "body")
    if body is None:
        raise FormatError("empty_source", "promulgation has no body")
    works = [v for _, v in _frbr(root, "FRBRWork", "FRBRthis") if v]
    stated_key = next(
        (bgbl_key_from_eli(w) for w in works if bgbl_key_from_eli(w)), None
    )
    if stated_key and stated_key != key:
        raise FormatError(
            "source_identity", f"promulgation states {stated_key}, not {key}"
        )
    dates = _frbr(root, "FRBRWork", "FRBRdate") + _frbr(
        root, "FRBRExpression", "FRBRdate"
    )
    promulgated = next(
        (_iso(v) for n, v in dates if n and "verkuendung" in n.casefold()), None
    )
    title_node = _find(root, "docTitle")
    title = _text("".join(title_node.itertext())) if title_node is not None else None
    preface = _find(root, "preface")
    preface_text = _text(" ".join(preface.itertext())) if preface is not None else ""
    signed = re.search(r"Vom\s+(\d{1,2}\.\s*[A-Za-zä]+\s*\d{4})", preface_text)
    from src.kb.legal_citations import _stated_date

    norms = _akn_blocks(body)
    sections: list[dict[str, Any]] = []
    amendments: list[dict[str, Any]] = []
    entry_into_force = []
    touched: set[str] = set()
    for article_index, norm in enumerate(norms):
        label = norm["enbez"] or f"Block {article_index + 1}"
        article_path = (
            re.sub(r"\s+", "", label.casefold())
            if label
            else f"block{article_index + 1}"
        )
        heading = norm.get("heading") or ""
        paragraphs = list(norm["paragraphs"])
        points = list(norm.get("points") or [()] * len(paragraphs))
        for index, text in enumerate(paragraphs):
            sections.append(
                {
                    "text": text,
                    "locator": {
                        "kind": "act-paragraph",
                        "official_norm_id": label,
                        "path": f"{article_path}/p{index + 1}",
                        "heading": heading or None,
                        "points": list(points[index]) if index < len(points) else [],
                    },
                }
            )
        if re.search(r"Inkrafttreten", heading):
            entry_into_force.append(
                {
                    "article": label,
                    "heading": heading,
                    "text": "\n".join(paragraphs),
                    "locator": {"path": article_path},
                }
            )
            continue
        intro = paragraphs[0] if paragraphs else ""
        if not re.search(
            r"wird\s+wie\s+folgt\s+geändert|werden\s+wie\s+folgt\s+geändert", intro
        ):
            continue
        statute = _statute_in(" ".join([heading, intro]), statutes)
        if statute:
            touched.add(statute_key(statute["jurabk"]))
        parents: dict[int, str | None] = {}
        for index, text in enumerate(paragraphs[1:], start=1):
            structural = points[index] if index < len(points) else ()
            match = _POINT.match(text)
            level = (
                len(structural)
                if structural
                else (1 if match and match.group(1) else 2 if match else 0)
            )
            item = (
                " ".join(structural)
                if structural
                else (match.group(0).strip() if match else "")
            )
            clause = text[match.end() :] if match and not structural else text
            if level == 0:
                continue
            # Drop the context of deeper items first, then read the enclosing item's target: an item never
            # inherits a sibling's or an earlier item's provision.
            for deeper in [k for k in parents if k >= level]:
                parents.pop(deeper)
            parent = parents.get(level - 1) if level > 1 else None
            if level > 1 and parent is None and statute is not None:
                # The enclosing item names no single provision (table of contents, annex, a range ...): its
                # sub-items stay unresolved with their text, whatever they mention.
                result = {
                    "status": "unresolved",
                    "provision": None,
                    "action": _action(
                        re.sub(r"„[^“]*“", "„…“", clause).split(":", 1)[0]
                    ),
                    "reason": "the enclosing item names no single provision",
                }
            else:
                result = _instruction(clause, statute, parent)
            parents[level] = result["provision"]
            amendments.append(
                {
                    "article": label,
                    "item": item or None,
                    "statute": statute["jurabk"] if statute else None,
                    "statute_key": statute_key(statute["jurabk"]) if statute else None,
                    **result,
                    "instruction": clause,
                    "locator": {
                        "path": f"{article_path}/p{index + 1}",
                        "article": label,
                        "item": item or None,
                    },
                }
            )
    notes = [n for n in root.iter() if _local(n.tag) == "authorialNote"]
    for index, note in enumerate(notes):
        text = _text(" ".join(note.itertext()))
        if text:
            sections.append(
                {
                    "text": text,
                    "locator": {
                        "kind": "act-note",
                        "official_norm_id": None,
                        "path": f"note/{note.get('marker') or index + 1}",
                    },
                }
            )
    fields = {
        "bgbl_key": key,
        "bgbl_citation": bgbl_citation(key),
        "eli": works[0] if works else None,
        "promulgation_date": promulgated,
        "ausfertigung_date": _stated_date(signed.group(1)) if signed else None,
        "entry_into_force": entry_into_force,
        "amendments": amendments,
        "touched_statutes": sorted(touched),
    }
    return _record(
        BGBL_PROVIDER,
        key,
        title or bgbl_citation(key),
        source_url=source_url,
        kind="amendment-act",
        fields=fields,
        sections=sections,
        native={"xml_sha256": hashlib.sha256(raw).hexdigest()},
        published_at=promulgated,
    )


def bgbl_citation(key: str) -> str:
    match = re.fullmatch(r"bgbl-([12])/(\d{4})/(?:nr-(\d+)|s-(\d+))", key)
    if not match:
        return key
    part = "I" if match.group(1) == "1" else "II"
    if match.group(3):
        return f"BGBl. {match.group(2)} {part} Nr. {match.group(3)}"
    return f"BGBl. {part} {match.group(2)} S. {match.group(4)}"
