"""Bounded scholarly acquisition and section-preserving JATS XML parsing."""

import hashlib
import json
import re
import time
from urllib.parse import quote, urlsplit

from src.ingestion.source_pack_runtime import HTTPSPageAdapter


def resolve_unpaywall(doi, *, contact, transport=None):
    if not contact or "@" not in contact:
        raise ValueError("Unpaywall contact email is required")
    doi = str(doi).removeprefix("https://doi.org/").lower()
    if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        raise ValueError("invalid DOI")
    request = transport or HTTPSPageAdapter._request
    response = request(
        url="https://api.unpaywall.org/v2/" + quote(doi, safe=""),
        params={"email": contact},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    if response.get("status", 200) != 200:
        raise ValueError("Unpaywall resolution failed")
    data = response["content"]
    if len(data) > 5_000_000:
        raise ValueError("Unpaywall response too large")
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(
        payload.get("oa_locations"), list
    ):
        raise ValueError("invalid Unpaywall response")  # noqa: TRY004 - external schema failures use ValueError
    result = []
    for location in payload["oa_locations"]:
        if not isinstance(location, dict):
            raise ValueError("invalid OA location")  # noqa: TRY004 - external schema failures use ValueError
        url = (
            location.get("url_for_pdf")
            or location.get("url_for_landing_page")
            or location.get("url")
        )
        if url and urlsplit(url).scheme == "https":
            result.append(
                {
                    "url": url,
                    "version": location.get("version"),
                    "host_type": location.get("host_type"),
                    "license": location.get("license"),
                    "doi": doi,
                    "provider": "unpaywall",
                }
            )
    return result


def parse_jats(data):
    from defusedxml import ElementTree as ET

    if len(data) > 10_000_000:
        raise ValueError("XML exceeds byte budget")
    root = ET.fromstring(data)
    if root.tag != "article":
        raise ValueError("full text is not a JATS article")
    sections = []
    references = []

    def visit(node, path, section=None):
        if node.tag == "sec":
            section = node.findtext("title") or section
        if node.tag == "p":
            text = "".join(node.itertext()).strip()
            sections.append(
                {
                    "text": text,
                    "section": section,
                    "locator": path,
                    "id": node.get("id"),
                    "citations": [
                        x.get("rid")
                        for x in node.findall(".//xref")
                        if x.get("ref-type") == "bibr"
                    ],
                }
            )
        if node.tag == "ref":
            references.append(
                {
                    "id": node.get("id"),
                    "text": "".join(node.itertext()).strip(),
                    "locator": path,
                }
            )
        counts = {}
        for child in node:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            visit(child, f"{path}/{child.tag}[{counts[child.tag]}]", section)

    visit(root, "/article[1]")
    return {
        "sections": sections,
        "references": references,
        "text": "\n\n".join(s["text"] for s in sections),
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


class FullTextAcquirer:
    def __init__(
        self, snapshots, *, allowed_hosts, transport=None, max_bytes=10_000_000
    ):
        self.snapshots = snapshots
        self.allowed_hosts = set(allowed_hosts)
        self.transport = transport
        self.max_bytes = min(20_000_000, max(1, int(max_bytes)))

    def acquire(self, url):
        if (
            urlsplit(url).scheme != "https"
            or urlsplit(url).hostname not in self.allowed_hosts
        ):
            return {"outcome": "failed", "failure_code": "source_scope_forbidden"}
        try:
            response = (self.transport or HTTPSPageAdapter._request)(
                url=url,
                params={},
                headers={"Accept": "application/pdf,application/xml"},
                timeout=15,
                **({} if self.transport else {"max_bytes": self.max_bytes}),
            )
            raw = response.get("content", b"")
            raw = raw.encode() if isinstance(raw, str) else bytes(raw)
            final = response.get("final_url", url)
            if (
                urlsplit(final).hostname not in self.allowed_hosts
                or urlsplit(final).scheme != "https"
            ):
                raise ValueError("redirect outside scope")
            if (
                response.get("status", 200) != 200
                or not raw
                or len(raw) > self.max_bytes
            ):
                raise ValueError("full-text acquisition failed or oversized")
            content_type = next(
                (
                    str(v).split(";")[0]
                    for k, v in response.get("headers", {}).items()
                    if k.lower() == "content-type"
                ),
                "application/octet-stream",
            )
            receipt = self.snapshots.snapshot_bytes(
                url,
                raw,
                int(time.time() * 1000),
                content_type=content_type,
                final_url=final,
            )
            if raw.lstrip().startswith(b"<"):
                parsed = parse_jats(raw)
            elif raw.startswith(b"%PDF"):
                import fitz

                with fitz.open(stream=raw, filetype="pdf") as pdf:
                    if len(pdf) > 200:
                        raise ValueError("PDF exceeds page budget")
                    parsed = {
                        "text": "\n\n".join(page.get_text() for page in pdf),
                        "sections": [],
                        "references": [],
                    }
            else:
                raise ValueError("unsupported full-text representation")
            if not parsed["text"].strip():
                raise ValueError("full-text extraction empty")
            return {"outcome": "full-text", "snapshot": receipt, **parsed}
        except Exception as exc:  # noqa: BLE001 - preserve bounded acquisition failure outcome
            return {
                "outcome": "failed",
                "failure_code": getattr(exc, "code", "full_text_failed"),
            }

    def europe_pmc(self, pmcid):
        if not re.fullmatch(r"PMC\d+", pmcid):
            raise ValueError("invalid PMCID")
        return self.acquire(
            "https://www.ebi.ac.uk/europepmc/webservices/rest/" + pmcid + "/fullTextXML"
        )
