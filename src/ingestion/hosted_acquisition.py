"""Native opt-in hosted acquisition with durable budgets and original-source lineage.

Credentials belong only to execution arguments. Request reservations are not
invoices; transformed provider text never substitutes for captured originals.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date
from urllib.parse import urlsplit

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import SourceRef
from src.ingestion.discovery_acquisition import acquire_candidates
from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import (
    DurableHTTP,
    ProviderError,
    _safe_url,
    canonical,
    digest,
)

HOSTS = {
    "exa": {"api.exa.ai"},
    "tavily": {"api.tavily.com"},
    "firecrawl": {"api.firecrawl.dev"},
    "zyte": {"api.zyte.com"},
    "jina": {"r.jina.ai"},
    "openalex-content": {"content.openalex.org"},
}


def _authorized(namespace, principal_id, scopes):
    if (
        not principal_id
        or "operator" not in scopes
        and not {"knowledge:ingestion:execute", f"namespace:{namespace}:write"}
        <= set(scopes)
    ):
        raise ProviderError(
            "unauthorized",
            "current ingestion and namespace-write authorization required",
        )


def _date(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("dates must use YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


class HostedClient:
    def __init__(
        self,
        http: DurableHTTP,
        *,
        principal_id,
        enabled=False,
        credential=None,
        max_cost_per_request_micros=0,
    ):
        if (
            enabled is not True
            or http.provider not in HOSTS
            or not http.hosts <= HOSTS[http.provider]
        ):
            raise ValueError(
                "explicit opt-in and exact hosted-provider configuration required"
            )
        if (
            type(max_cost_per_request_micros) is not int
            or not 0 <= max_cost_per_request_micros <= http.limits["usd_micros"]
        ):
            raise ValueError("per-request ceiling must fit the explicit project budget")
        if http.provider != "jina" and (
            not credential or max_cost_per_request_micros <= 0
        ):
            raise ValueError(
                "credential and nonzero conservative per-request reservation required"
            )
        self.http, self.principal_id, self.credential, self.price = (
            http,
            principal_id,
            credential,
            max_cost_per_request_micros,
        )

    def search(
        self,
        query,
        observation,
        *,
        public_query_approved=False,
        domains=(),
        from_date=None,
        to_date=None,
        limit=10,
    ):
        provider = self.http.provider
        if provider not in {"exa", "tavily"}:
            raise ValueError("this provider does not implement discovery")
        if (
            public_query_approved is not True
            or not isinstance(query, str)
            or not 1 <= len(query) <= 4000
        ):
            raise ValueError(
                "explicitly approved bounded non-private search query required"
            )
        if (
            type(limit) is not int
            or not 1 <= limit <= 20
            or len(domains) > 20
            or any(not re.fullmatch(r"[A-Za-z0-9.-]+", value) for value in domains)
        ):
            raise ValueError("bounded discovery domains/result limit required")
        start, end = _date(from_date), _date(to_date)
        if start and end and start > end:
            raise ValueError("inverted discovery date window")
        if provider == "exa":
            body = {
                "query": query,
                "numResults": limit,
                "type": "auto",
                "contents": {"text": False, "highlights": False},
            }
            if domains:
                body["includeDomains"] = list(domains)
            if start:
                body["startPublishedDate"] = start + "T00:00:00.000Z"
            if end:
                body["endPublishedDate"] = end + "T23:59:59.999Z"
            secret_headers = {"x-api-key": self.credential}
        else:
            body = {
                "query": query,
                "max_results": limit,
                "search_depth": "basic",
                "auto_parameters": False,
                "include_answer": False,
                "include_raw_content": False,
                "include_usage": True,
            }
            if domains:
                body["include_domains"] = list(domains)
            if start:
                body["start_date"] = start
            if end:
                body["end_date"] = end
            secret_headers = {"Authorization": "Bearer " + self.credential}
        captured = self.http.request(
            observation + ":search",
            "https://api."
            + provider
            + (".ai/search" if provider == "exa" else ".com/search"),
            principal_id=self.principal_id,
            method="POST",
            body=body,
            secret_headers=secret_headers,
            max_cost_micros=self.price,
            max_bytes=4_000_000,
            timeout_s=30,
        )
        payload = captured.json()
        records = payload.get("results")
        if not isinstance(records, list) or len(records) > limit:
            raise ProviderError(
                "schema_drift", "hosted search returned an invalid result envelope"
            )
        refs, excluded = [], []
        for record in records:
            if not isinstance(record, dict):
                raise ProviderError(
                    "schema_drift", "hosted search result must be an object"
                )
            url = record.get("url")
            host = urlsplit(str(url)).hostname
            try:
                _safe_url(url, {host})
                if domains and not any(
                    host == value or host.endswith("." + value) for value in domains
                ):
                    raise ValueError("outside requested domain scope")
            except (ValueError, ProviderError):
                excluded.append({"reason": "invalid_or_out_of_scope_url"})
                continue
            refs.append(
                SourceRef(
                    url,
                    title=record.get("title"),
                    metadata={
                        "discovery_provider": provider,
                        "provider_source_id": record.get("id") or url,
                        "provider_summary": record.get("summary")
                        or record.get("content")
                        or record.get("text"),
                        "provider_score": record.get("score"),
                        "published_at": record.get("publishedDate")
                        or record.get("published_date"),
                        "provider_summary_is_evidence": False,
                        "requires_source_acquisition": True,
                        "capture_sha256": captured.receipt["digest"],
                        "observed_at_ms": captured.receipt["observed_at_ms"],
                    },
                )
            )
        return {
            "references": refs,
            "excluded": excluded,
            "receipt": captured.receipt,
            "reported_usage": payload.get("usage") or payload.get("costDollars"),
            "reported_usage_is_invoice": False,
        }

    def acquire_selected(
        self,
        conn,
        result,
        selected_urls,
        *,
        namespace,
        scopes,
        allowed_hosts,
        language,
        observation,
        transport=None,
        dns_resolver=None,
    ):
        _authorized(namespace, self.principal_id, scopes)
        if (
            not isinstance(selected_urls, list)
            or len(selected_urls) > 20
            or len(set(selected_urls)) != len(selected_urls)
        ):
            raise ValueError("bounded explicitly selected unique URLs required")
        by_url = {ref.locator: ref for ref in result["references"]}
        if not set(selected_urls) <= set(by_url):
            raise ValueError("selection contains a URL absent from discovery")
        return acquire_candidates(
            conn,
            [by_url[url] for url in selected_urls],
            acquisition_id=digest([namespace, self.principal_id, observation]),
            allowed_hosts=allowed_hosts,
            language=language,
            max_candidates=20,
            transport=transport,
            dns_resolver=dns_resolver,
        )

    def scrape(
        self, url, observation, *, allowed_source_hosts, public_url_approved=False
    ):
        provider = self.http.provider
        if provider not in {"firecrawl", "zyte"} or public_url_approved is not True:
            raise ValueError("explicit public URL processing approval required")
        _safe_url(url, set(allowed_source_hosts))
        if provider == "firecrawl":
            endpoint = "https://api.firecrawl.dev/v2/scrape"
            body = {
                "url": url,
                "formats": ["rawHtml", "markdown"],
                "onlyMainContent": False,
                "timeout": 20000,
                "proxy": "basic",
                "parsers": [],
                "storeInCache": False,
            }
            headers = {"Authorization": "Bearer " + self.credential}
        else:
            endpoint = "https://api.zyte.com/v1/extract"
            body = {"url": url, "httpResponseBody": True, "httpResponseHeaders": True}
            headers = {
                "Authorization": "Basic "
                + base64.b64encode((self.credential + ":").encode()).decode()
            }
            if (
                self.http.transport is not zyte_scrapy_transport
                and self.http.configuration["transport"] == "network"
            ):
                raise ValueError(
                    "Zyte evaluation must use the explicit Scrapy transport boundary"
                )
        captured = self.http.request(
            observation + ":scrape",
            endpoint,
            method="POST",
            body=body,
            secret_headers=headers,
            principal_id=self.principal_id,
            max_cost_micros=self.price,
            max_bytes=8_000_000,
            timeout_s=45,
        )
        payload = captured.json()
        if provider == "firecrawl":
            if payload.get("success") is not True or not isinstance(
                payload.get("data"), dict
            ):
                raise ProviderError(
                    "provider_failed",
                    "Firecrawl did not return a successful native scrape",
                )
            native = payload["data"]
            source_status = native.get("metadata", {}).get("statusCode")
            if type(source_status) is not int or not 200 <= source_status < 300:
                raise ProviderError(
                    "source_failed",
                    "Firecrawl returned an unsuccessful or missing source status",
                )
            final_url = (
                native.get("metadata", {}).get("sourceURL")
                or native.get("metadata", {}).get("url")
                or url
            )
            raw_html = native.get("rawHtml")
            if not isinstance(raw_html, str) or not raw_html:
                raise ProviderError(
                    "unavailable_html", "Firecrawl omitted requested HTML"
                )
            representation = "provider-transformed-html-not-original-response"
        else:
            native = payload
            if (
                type(native.get("statusCode")) is not int
                or not 200 <= native["statusCode"] < 300
            ):
                raise ProviderError(
                    "source_failed", "Zyte returned an unsuccessful source status"
                )
            final_url = native.get("url")
            data = base64.b64decode(native.get("httpResponseBody", ""), validate=True)
            if not data:
                raise ProviderError(
                    "unavailable_html", "Zyte omitted requested response bytes"
                )
            raw_html = data.decode("utf-8", errors="replace")
            representation = "zyte-reported-http-response-body"
        _safe_url(final_url, set(allowed_source_hosts))
        if len(raw_html.encode()) > 4_000_000:
            raise ProviderError(
                "response_limit", "provider HTML exceeds evidence budget"
            )
        return {
            "html": raw_html,
            "markdown": native.get("markdown"),
            "source_url": url,
            "final_url": final_url,
            "representation": representation,
            "provider": provider,
            "receipt": captured.receipt,
            "provider_metadata": native.get("metadata"),
            "precise_locators": False,
            "actual_billed_usd_micros": None,
        }

    def reader(
        self,
        url,
        observation,
        *,
        original_snapshot,
        allowed_source_hosts,
        public_url_approved=False,
    ):
        if self.http.provider != "jina" or public_url_approved is not True:
            raise ValueError("explicit Jina public URL processing approval required")
        _safe_url(url, set(allowed_source_hosts))
        if (
            not isinstance(original_snapshot, dict)
            or original_snapshot.get("final_url", original_snapshot.get("url")) != url
        ):
            raise ValueError("a matching original-source snapshot is required")
        source_observation = self.http.conn.execute(
            "SELECT 1 FROM source_binary_observations WHERE url=? AND final_url=? AND fetched_at=? AND digest=?",
            [
                original_snapshot.get("url"),
                url,
                original_snapshot.get("fetched_at"),
                original_snapshot.get("digest"),
            ],
        ).fetchone()
        if source_observation is None:
            raise ValueError(
                "original snapshot is not bound to this source observation"
            )
        stored = self.http.conn.execute(
            "SELECT payload FROM source_binary_blobs WHERE digest=?",
            [original_snapshot.get("digest")],
        ).fetchone()
        if not stored or hashlib.sha256(
            bytes(stored[0])
        ).hexdigest() != original_snapshot.get("digest"):
            raise ValueError("original source bytes are missing or changed")
        captured = self.http.request(
            observation + ":reader",
            "https://r.jina.ai/" + url,
            principal_id=self.principal_id,
            headers={
                "Accept": "application/json",
                "X-Respond-With": "markdown",
                "X-Token-Budget": "16000",
            },
            secret_headers={"Authorization": "Bearer " + self.credential}
            if self.credential
            else {},
            max_cost_micros=self.price,
            max_bytes=4_000_000,
            timeout_s=30,
        )
        payload = captured.json()
        native = payload.get("data")
        if (
            payload.get("code") != 200
            or not isinstance(native, dict)
            or not isinstance(native.get("content"), str)
        ):
            raise ProviderError(
                "schema_drift", "unexpected Jina Reader native response"
            )
        actual_url = native.get("url", url)
        if actual_url != url:
            raise ProviderError(
                "source_identity", "Jina transformation refers to another source"
            )
        return {
            "text": native["content"],
            "source_url": url,
            "original_snapshot": original_snapshot,
            "transformation_snapshot": captured.receipt["snapshot"],
            "representation": "jina-markdown",
            "precise_locators": False,
            "receipt": captured.receipt,
            "reported_usage": native.get("usage"),
        }


class OpenAlexFullText:
    """Native per-format fulltext with restart-safe cost reservations and source history."""

    def __init__(
        self,
        http,
        *,
        principal_id,
        credential,
        enabled=False,
        max_cost_per_download_micros,
    ):
        self.hosted = HostedClient(
            http,
            principal_id=principal_id,
            credential=credential,
            enabled=enabled,
            max_cost_per_request_micros=max_cost_per_download_micros,
        )
        if http.provider != "openalex-content":
            raise ValueError("OpenAlex content transport required")
        self.http, self.principal_id = http, principal_id
        http.conn.execute(
            "CREATE TABLE IF NOT EXISTS openalex_content_outcomes(namespace TEXT,owner TEXT,observation TEXT,work_id TEXT,representation TEXT,request_hash TEXT,result_json TEXT,PRIMARY KEY(namespace,owner,observation,work_id,representation))"
        )

    def acquire(
        self, work, observation, *, representation="grobid_xml", namespace, scopes
    ):
        _authorized(namespace, self.principal_id, scopes)
        if (
            representation not in {"pdf", "grobid_xml"}
            or not isinstance(work, dict)
            or not re.fullmatch(r"https://openalex.org/W[0-9]+", str(work.get("id")))
        ):
            raise ValueError(
                "native OpenAlex work ID and supported representation required"
            )
        if not isinstance(observation, str) or not 1 <= len(observation) <= 256:
            raise ValueError("bounded explicit OpenAlex observation required")
        key = [namespace, self.principal_id, observation, work["id"], representation]
        request_hash = digest({"work": work, "budget_id": self.http.budget_id})
        previous = self.http.conn.execute(
            "SELECT request_hash,result_json FROM openalex_content_outcomes WHERE namespace=? AND owner=? AND observation=? AND work_id=? AND representation=?",
            key,
        ).fetchone()
        if previous:
            if previous[0] != request_hash:
                raise ProviderError(
                    "observation_conflict",
                    "OpenAlex observation is bound to different work metadata or budget",
                )
            result = {**json.loads(previous[1]), "replayed": True}
            if "receipt" in result:
                result["receipt"] = {**result["receipt"], "replayed": True}
            return result

        def finish(result):
            self.http.conn.execute(
                "INSERT INTO openalex_content_outcomes VALUES (?,?,?,?,?,?,?)",
                [*key, request_hash, canonical(result)],
            )
            return {**result, "replayed": False}

        available, urls = work.get("has_content"), work.get("content_urls")
        if (
            not isinstance(available, dict)
            or available.get(representation) is not True
            or not isinstance(urls, dict)
            or not urls.get(representation)
        ):
            return finish(
                {
                    "status": "unavailable",
                    "work_id": work["id"],
                    "representation": representation,
                    "content_coverage": "metadata-only",
                    "failure_code": "representation_unavailable",
                }
            )
        identity = work["id"].rsplit("/", 1)[-1]
        expected = (
            "https://content.openalex.org/works/"
            + identity
            + (".pdf" if representation == "pdf" else ".grobid-xml")
        )
        if urls[representation] != expected:
            raise ProviderError(
                "source_identity", "OpenAlex content URL does not match the work/format"
            )
        captured = self.http.request(
            observation + ":" + identity + ":" + representation,
            expected,
            principal_id=self.principal_id,
            secret_params={"api_key": self.hosted.credential},
            max_cost_micros=self.hosted.price,
            max_bytes=20_000_000,
            timeout_s=45,
            headers={
                "Accept": "application/pdf"
                if representation == "pdf"
                else "application/xml"
            },
        )
        raw = captured.content
        sections, references, coverage = [], [], "full-text"
        if representation == "grobid_xml":
            from defusedxml import ElementTree as ET

            from src.ingestion.roadmap_integrations import parse_tei

            root = ET.fromstring(raw)
            body = root.find(".//{*}body")
            if (
                root.tag.rsplit("}", 1)[-1] != "TEI"
                or body is None
                or not "".join(body.itertext()).strip()
            ):
                raise ProviderError(
                    "incomplete_tei",
                    "OpenAlex TEI has no usable body; references alone are not full text",
                )
            parsed = parse_tei(raw, max_bytes=20_000_000)
            text, sections, references = (
                parsed["text"],
                parsed["sections"],
                parsed["references"],
            )
        else:
            if not raw.startswith(b"%PDF"):
                raise ProviderError(
                    "unsupported_format", "OpenAlex PDF response is not PDF data"
                )
            import pymupdf

            from src.ingestion.connectors.paper.pdf_parser import parse_pdf

            with pymupdf.open(stream=raw, filetype="pdf") as document:
                if len(document) > 250 or document.needs_pass:
                    raise ProviderError(
                        "input_limit", "bounded unencrypted PDF required"
                    )
                for index, page in enumerate(document, 1):
                    for block in page.get_text("blocks"):
                        if block[4].strip():
                            sections.append(
                                {
                                    "text": block[4],
                                    "page": index,
                                    "bbox": list(block[:4]),
                                }
                            )
            parsed = parse_pdf(raw)
            text = parsed.text
            if not text.strip():
                return finish(
                    {
                        "status": "unavailable",
                        "work_id": work["id"],
                        "receipt": captured.receipt,
                        "content_coverage": "captured-pdf-without-extracted-text",
                        "failure_code": "ocr_required",
                    }
                )
        licence = (work.get("best_oa_location") or {}).get("license")
        doc_id = (
            "openalex-content:" + digest([namespace, work["id"], representation])[:32]
        )
        doc = Document(
            document_id=doc_id,
            source_type="paper",
            source_id=work["id"],
            language=work.get("language") or "und",
            ingested_at=captured.receipt["observed_at_ms"],
            url=work["id"],
            title=work.get("title") or work["id"],
            content=text,
            metadata={
                "namespace": namespace,
                "work_id": work["id"],
                "representation": representation,
                "original_sha256": captured.receipt["digest"],
                "content_coverage": coverage,
                "license": licence,
                "license_basis": "best_oa_location; original copyright retained",
                "sections_json": canonical(sections),
                "references_json": canonical(references),
                "completeness_independently_verified": False,
                "acquisition_provenance_json": canonical(captured.receipt),
            },
        )
        store = DocumentStore(self.http.conn)
        result = {
            "status": "acquired",
            "document_id": doc_id,
            "work_id": work["id"],
            "representation": representation,
            "content_coverage": coverage,
            "receipt": captured.receipt,
            "license": licence,
            "sections": sections,
            "references": references,
        }
        self.http.conn.execute("BEGIN")
        try:
            outcome = store.upsert([doc])
            if outcome.invalid:
                raise ProviderError(
                    "document_invalid",
                    "OpenAlex content failed normal document validation",
                )
            result["source_ref"] = {
                "document_id": doc_id,
                "revision_id": outcome.changes[0]["revision_id"],
            }
            result = finish(result)
            self.http.conn.execute("COMMIT")
        except Exception:
            self.http.conn.execute("ROLLBACK")
            raise
        return result


def zyte_scrapy_transport(*, method, url, params, body, headers, timeout_s, max_bytes):
    """DurableHTTP transport: perform the one reserved request via Scrapy in a worker."""
    if (
        method != "POST"
        or url != "https://api.zyte.com/v1/extract"
        or params
        or set(body or {}) != {"url", "httpResponseBody", "httpResponseHeaders"}
    ):
        raise ValueError("unsupported Zyte Scrapy transport request")
    encoded = headers.get("Authorization", "")
    if not encoded.startswith("Basic "):
        raise ValueError("explicit Zyte credential required")
    credential = base64.b64decode(encoded[6:], validate=True).decode().removesuffix(":")
    from src.evaluation.runtime_jobs import execute_job

    result = execute_job(
        "zyte-fetch",
        {
            "url": body["url"],
            "credential": credential,
            "timeout_s": max(0.1, timeout_s - 2),
            "max_bytes": max_bytes,
            "reservation_authorized": True,
        },
        timeout_s=timeout_s - 0.1,
        max_rss_bytes=1024**3,
        max_output_bytes=max_bytes + 100000,
    )
    if result["status"] != "completed":
        raise ProviderError(
            result.get("failure_code", "zyte_failed"),
            "reserved Zyte worker did not complete",
        )
    return {
        "status": 200,
        "content": canonical(result["result"]).encode(),
        "headers": {"Content-Type": "application/json"},
    }
