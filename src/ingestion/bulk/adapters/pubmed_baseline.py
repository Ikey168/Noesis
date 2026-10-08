"""PubMed annual baseline and daily update files (FA14).

``https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/`` (2026: 1,334 files
``pubmed26nNNNN.xml.gz``, ~20–40 MB each) and ``/pubmed/updatefiles/`` (daily
increments). Each file has a ``.md5`` companion (``MD5(file)= <hex>``), which
the runner fetches and verifies before parsing. No key, no E-utilities quota.

Articles become paper ``Document``s with the same ``document_id`` as the
``pubmed`` connector (DOI-based when a DOI is present), with title, structured
abstract, authors, journal, publication date and MeSH headings. Update files
also carry ``DeleteCitation`` PMIDs, emitted to a ``deletions`` table so a
downstream store can retire them. Apply files in order (baseline, then updates).

Parameters (at least one): ``pmids``, ``mesh`` (descriptor names), ``journals``
(ISSN or journal title), ``keywords`` (case-insensitive, in title or abstract);
optional ``year_from`` / ``year_to``; ``include_baseline`` / ``include_updates``
(both default true). Use the runner's ``max_files`` budget to process the 1,600+
files over several runs. NLM terms: https://www.nlm.nih.gov/databases/download/terms_and_conditions.html
"""
from __future__ import annotations

import gzip
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional

from defusedxml.ElementTree import iterparse

from services.ingest.common.document_model import Document
from src.ingestion.bulk.adapters._common import text_set
from src.ingestion.bulk.base import BulkAdapter, FileSource, Release, ReleaseFile
from src.ingestion.connectors.scholarly.base import _document_id, _to_millis

BASE = "https://ftp.ncbi.nlm.nih.gov/pubmed/"
_ENTRY = re.compile(r'href="(pubmed(\d{2})n(\d{4})\.xml\.gz)">[^<]*</a>\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})')
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _text(element) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _pub_date(article) -> Optional[str]:
    node = article.find("./MedlineCitation/Article/Journal/JournalIssue/PubDate")
    if node is None:
        return None
    year = node.findtext("Year") or (node.findtext("MedlineDate") or "")[:4]
    if not year or not year.isdigit():
        return None
    month = node.findtext("Month") or "1"
    month = MONTHS.get(month[:3].lower(), int(month) if month.isdigit() else 1)
    day = node.findtext("Day") or "1"
    return f"{year}-{int(month):02d}-{int(day) if day.isdigit() else 1:02d}"


class PubMedBaseline(BulkAdapter):
    name = "pubmed-baseline"
    publisher = "U.S. National Library of Medicine"
    title = "PubMed baseline and update files"
    description = "PubMed citations filtered from the annual baseline and daily update XML files."
    allowed_hosts = ("ftp.ncbi.nlm.nih.gov",)
    output = "documents"
    incremental = "file"

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        out = {"pmids": text_set(params.get("pmids")), "mesh": [m.casefold() for m in text_set(params.get("mesh"))],
               "journals": [j.casefold() for j in text_set(params.get("journals"))],
               "keywords": [k.casefold() for k in text_set(params.get("keywords"))],
               "year_from": int(params["year_from"]) if params.get("year_from") else None,
               "year_to": int(params["year_to"]) if params.get("year_to") else None,
               "include_baseline": params.get("include_baseline", True) is not False,
               "include_updates": params.get("include_updates", True) is not False}
        if not any(out[k] for k in ("pmids", "mesh", "journals", "keywords")):
            raise ValueError("give pmids, mesh, journals or keywords")
        return out

    def list_release(self, http, params) -> Release:
        files: List[ReleaseFile] = []
        year = ""
        last_update = ""
        for folder, wanted in (("baseline/", params["include_baseline"]), ("updatefiles/", params["include_updates"])):
            if not wanted:
                continue
            for name, yy, number, stamp in sorted(set(_ENTRY.findall(http.get_text(BASE + folder)))):
                year = year or yy
                if folder == "updatefiles/":
                    last_update = number
                files.append(ReleaseFile(name=folder + name, url=BASE + folder + name, mode="download",
                                         last_modified=stamp,
                                         metadata={"checksum_url": BASE + folder + name + ".md5",
                                                   "kind": folder.rstrip("/"), "sequence": number}))
        if not files:
            raise ValueError("no PubMed files found in the listings")
        release_id = f"pubmed{year}-baseline" + (f"-updates{last_update}" if last_update else "")
        return Release("nlm", release_id, files, licence={
            "id": "nlm-pubmed-terms", "attribution": "U.S. National Library of Medicine",
            "terms_url": "https://www.nlm.nih.gov/databases/download/terms_and_conditions.html",
            "note": "abstracts may be subject to publisher copyright"})

    def _match(self, pmid: str, title: str, abstract: str, mesh: List[str], journal: List[str],
               year: Optional[int], params) -> bool:
        if params["year_from"] and (year is None or year < params["year_from"]):
            return False
        if params["year_to"] and (year is None or year > params["year_to"]):
            return False
        if params["pmids"] and pmid in params["pmids"]:
            return True
        hits = []
        if params["mesh"]:
            hits.append(any(m.casefold() in params["mesh"] for m in mesh))
        if params["journals"]:
            hits.append(any(j.casefold() in params["journals"] for j in journal if j))
        if params["keywords"]:
            hay = f"{title} {abstract}".casefold()
            hits.append(any(k in hay for k in params["keywords"]))
        return bool(hits) and all(hits)

    def process(self, source: FileSource, params) -> Iterator[Any]:
        with gzip.open(source.path, "rb") as handle:
            for _, element in iterparse(handle, events=("end",)):
                if element.tag == "DeleteCitation":
                    for pmid in element.findall("PMID"):
                        yield "deletions", {"pmid": (pmid.text or "").strip(), "file": source.file.name}
                    element.clear()
                    continue
                if element.tag != "PubmedArticle":
                    continue
                pmid = (element.findtext("./MedlineCitation/PMID") or "").strip()
                article = element.find("./MedlineCitation/Article")
                title = _text(article.find("ArticleTitle")) if article is not None else ""
                parts = []
                for node in element.findall("./MedlineCitation/Article/Abstract/AbstractText"):
                    label = node.get("Label")
                    text = _text(node)
                    parts.append(f"{label}: {text}" if label and text else text)
                abstract = "\n".join(p for p in parts if p)
                mesh = [_text(d) for d in element.findall("./MedlineCitation/MeshHeadingList/MeshHeading/DescriptorName")]
                journal_title = element.findtext("./MedlineCitation/Article/Journal/Title") or ""
                issns = [i.text or "" for i in element.findall("./MedlineCitation/Article/Journal/ISSN")]
                published = _pub_date(element)
                year = int(published[:4]) if published else None
                if self._match(pmid, title, abstract, mesh, [journal_title, *issns], year, params):
                    doi = next((i.text for i in element.findall("./PubmedData/ArticleIdList/ArticleId")
                                if i.get("IdType") == "doi" and i.text), None)
                    authors = []
                    for a in element.findall("./MedlineCitation/Article/AuthorList/Author"):
                        name = " ".join(x for x in (a.findtext("ForeName"), a.findtext("LastName")) if x)
                        authors.append(name or a.findtext("CollectiveName") or "")
                    yield Document(
                        document_id=_document_id("pubmed", pmid, doi), source_type="paper",
                        language=(element.findtext("./MedlineCitation/Article/Language") or "en")[:3],
                        ingested_at=0, source_id="pubmed", url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                        title=title.rstrip("."), content=abstract or None, authors=[a for a in authors if a],
                        created_at=_to_millis(published) if published else None,
                        metadata={k: v for k, v in {
                            "source_api": "pubmed-baseline", "external_id": pmid, "doi": doi,
                            "work_identifier": f"doi:{doi.lower()}" if doi else f"pubmed:{pmid}",
                            "venue": journal_title or None, "mesh": mesh or None,
                            "content_coverage": "abstract-only" if abstract else "metadata-only",
                            "bulk_file": source.file.name}.items() if v})
                element.clear()
