"""Trial-registry identifiers carried by scholarly records (Clinical Evidence, H07).

The Science providers already harvest PubMed, Europe PMC and medRxiv/bioRxiv
records as ``paper`` documents. This module reads the registry identifiers
those sources *declare* for an article, so the Clinical Evidence pack can link
trials to publications from source evidence instead of guessing:

* PubMed efetch MEDLINE XML: ``DataBankList`` / ``AccessionNumberList``
  (secondary-source identifiers such as ClinicalTrials.gov and EudraCT
  numbers) and ``PublicationTypeList``; esummary alone carries neither.
* Europe PMC Annotations API: text-mined ``Accession Numbers`` annotations
  (tags ``nct``, ``eudract``, ``isrctn``).
* medRxiv/bioRxiv details: the ``published`` DOI of the journal version, as a
  provider relation usable by :class:`~src.domains.research.paper_families.PaperFamilyStore`.

Identifiers are attached to a document's metadata as
``registry_accessions_json`` with their source; a registry number that only
appears in free text is reported separately as a mention (a review candidate,
never an accepted link).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

_PATTERNS = {
    "nct": re.compile(r"^NCT\d{8}$"),
    "eu-ct": re.compile(r"^\d{4}-\d{6}-\d{2}-\d{2}$"),
    "eudract": re.compile(r"^\d{4}-\d{6}-\d{2}$"),
    "isrctn": re.compile(r"^ISRCTN\d{8}$"),
    "prospero": re.compile(r"^CRD\d{11}$"),
}
_MENTION = {
    "nct": re.compile(r"\bNCT\d{8}\b"),
    "eu-ct": re.compile(r"\b\d{4}-\d{6}-\d{2}-\d{2}\b"),
    "eudract": re.compile(r"\b\d{4}-\d{6}-\d{2}\b(?!-\d)"),
    "isrctn": re.compile(r"\bISRCTN\d{8}\b"),
    "prospero": re.compile(r"\bCRD\d{11}\b"),
}
_DATABANKS = {"clinicaltrials.gov": "nct", "eudract": "eudract", "isrctn": "isrctn", "ctis": "eu-ct"}
TRIAL_PUBLICATION_TYPES = frozenset({
    "randomized controlled trial", "clinical trial", "clinical trial, phase i", "clinical trial, phase ii",
    "clinical trial, phase iii", "clinical trial, phase iv", "controlled clinical trial", "pragmatic clinical trial",
    "equivalence trial",
})


def classify(value: str) -> str | None:
    value = str(value or "").strip()
    return next((kind for kind, pattern in _PATTERNS.items() if pattern.fullmatch(value)), None)


def parse_pubmed_databanks(xml: bytes | str) -> dict[str, dict[str, Any]]:
    """PMID -> declared registry accessions, publication types, DOI and abstract sections."""
    from defusedxml import ElementTree as ET

    root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    result: dict[str, dict[str, Any]] = {}
    for article in root.iter("PubmedArticle"):
        pmid = (article.findtext("MedlineCitation/PMID") or "").strip()
        if not pmid:
            continue
        accessions = []
        for bank in article.iter("DataBank"):
            name = (bank.findtext("DataBankName") or "").strip()
            for number in bank.iter("AccessionNumber"):
                value = (number.text or "").strip()
                kind = classify(value) or _DATABANKS.get(name.casefold())
                if value and kind and (kind not in _PATTERNS or _PATTERNS[kind].fullmatch(value)):
                    accessions.append({"kind": kind, "value": value, "declared_by": f"pubmed-databank:{name}",
                                       "locator": {"xpath": f"PubmedArticle[PMID={pmid}]/DataBankList"}})
        sections = []
        for index, text in enumerate(article.iter("AbstractText")):
            content = "".join(text.itertext()).strip()
            if content:
                sections.append({"label": text.get("Label"), "text": content, "passage": index})
        doi = next(((i.text or "").strip().lower() for i in article.iter("ArticleId") if i.get("IdType") == "doi"),
                   None)
        result[pmid] = {"accessions": accessions, "doi": doi, "abstract": sections,
                        "publication_types": [(p.text or "").strip() for p in article.iter("PublicationType")
                                              if (p.text or "").strip()]}
    return result


def parse_europepmc_accessions(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Europe PMC Annotations API response -> article id -> text-mined accession numbers."""
    if not isinstance(payload, list):
        raise ValueError("Europe PMC annotations response must be a list of articles")
    result: dict[str, list[dict[str, Any]]] = {}
    for article in payload:
        key = f"{article.get('source')}:{article.get('extId')}"
        found = []
        for annotation in article.get("annotations") or []:
            if annotation.get("type") != "Accession Numbers":
                continue
            value = str(annotation.get("exact") or "").strip()
            tags = {str(t.get("name") or "").casefold() for t in annotation.get("tags") or []}
            kind = classify(value) or next((_DATABANKS.get(t) or (t if t in _PATTERNS else None) for t in tags), None)
            if kind and value:
                found.append({"kind": kind, "value": value, "declared_by": "europepmc-annotations",
                              "locator": {"section": annotation.get("section"), "quote": value}})
        result[key] = found
    return result


def rxiv_related_resources(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    """medRxiv/bioRxiv ``published`` DOI as an ``IsPreprintOf`` provider relation."""
    doi, published = str(record.get("doi") or "").lower(), str(record.get("published") or "").lower()
    if not doi or not published or published in {"na", "none"}:
        return []
    return [{"source_identifier": doi, "source_identifier_type": "DOI", "predicate": "IsPreprintOf",
             "target_identifier": published, "target_identifier_type": "DOI",
             "provider": str(record.get("server") or "medrxiv")}]


def with_registry_identifiers(document: Mapping[str, Any], accessions: Iterable[Mapping[str, Any]], *,
                              publication_types: Iterable[str] = (), related_resources=None) -> dict[str, Any]:
    """A copy of a document payload carrying declared registry identifiers in its metadata."""
    payload = dict(document.to_dict() if hasattr(document, "to_dict") else document)
    metadata = dict(payload.get("metadata") or {})
    values = sorted({json.dumps(dict(a), sort_keys=True) for a in accessions})
    metadata["registry_accessions_json"] = json.dumps([json.loads(v) for v in values], sort_keys=True)
    types = sorted({str(t) for t in publication_types if str(t).strip()})
    if types:
        metadata["publication_types_json"] = json.dumps(types)
    if related_resources:
        metadata["related_resources_json"] = json.dumps(list(related_resources), sort_keys=True)
    payload["metadata"] = metadata
    return payload


def _metadata(document: Mapping[str, Any]) -> dict[str, Any]:
    metadata = document.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = {}
    return dict(metadata)


def _native(document: Mapping[str, Any]) -> dict[str, Any]:
    raw = _metadata(document).get("source_pack_native_json")
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        return {}


def registry_identifiers(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Declared (secondary-source) registry identifiers of a stored document."""
    raw = _metadata(document).get("registry_accessions_json")
    try:
        values = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except ValueError:
        values = []
    return [dict(v, evidence_kind="secondary-source-identifier") for v in values
            if isinstance(v, Mapping) and v.get("kind") and v.get("value")]


def mentions(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Registry numbers found only in title or abstract text (review candidates)."""
    text = " ".join(str(document.get(k) or "") for k in ("title", "content"))
    found = []
    for kind, pattern in _MENTION.items():
        for match in pattern.finditer(text):
            item = {"kind": kind, "value": match.group(0), "evidence_kind": "abstract-mention",
                    "locator": {"quote": text[max(0, match.start() - 60): match.end() + 60]}}
            if item["value"] not in {f["value"] for f in found}:
                found.append(item)
    return found


def publication_types(document: Mapping[str, Any]) -> list[str]:
    metadata = _metadata(document)
    raw = metadata.get("publication_types_json")
    try:
        types = json.loads(raw) if isinstance(raw, str) else list(raw or [])
    except ValueError:
        types = []
    native = _native(document).get("pubTypeList") or {}
    types += list(native.get("pubType") or []) if isinstance(native, Mapping) else []
    return sorted({str(t) for t in types})


def is_trial_report(document: Mapping[str, Any]) -> bool:
    return any(t.casefold() in TRIAL_PUBLICATION_TYPES for t in publication_types(document))


def identifiers(document: Mapping[str, Any]) -> dict[str, str | None]:
    """PMID and DOI of a stored paper document, from its metadata."""
    metadata, native = _metadata(document), _native(document)
    pmid = None
    if metadata.get("source_api") == "pubmed" or document.get("source_id") == "pubmed":
        pmid = metadata.get("external_id")
    pmid = pmid or native.get("pmid") or metadata.get("pmid")
    doi = metadata.get("doi") or native.get("doi")
    return {"pmid": str(pmid) if pmid else None, "doi": str(doi).lower() if doi else None}
