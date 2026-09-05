"""Optional SDMX-native statistical connector with bounded raw capture."""

import io
import math
from urllib.parse import urlsplit, parse_qsl, urlunsplit
from .base import DatasetConnector, SeriesRef, RawSeries
from services.ingest.common.series_model import SeriesRecord, Observation
from src.integrations.common import IntegrationError, digest, version


class SDMXConnector(DatasetConnector):
    def __init__(
        self,
        provider="ECB",
        *,
        transport=None,
        max_bytes=8_000_000,
        max_observations=10000,
    ):
        if provider not in {"ECB", "ESTAT", "BBK"}:
            raise ValueError("Supported SDMX providers: ECB, ESTAT, BBK")
        if not 1 <= max_bytes <= 20_000_000 or not 1 <= max_observations <= 100000:
            raise ValueError("invalid SDMX bounds")
        self.provider = provider.lower()
        self.source = provider
        self.transport = transport
        self.max_bytes = max_bytes
        self.max_observations = max_observations

    def discover(self, query=None):
        if query is None:
            return
        if (
            not isinstance(query, dict)
            or set(query) - {"flow", "key", "startPeriod", "endPeriod"}
            or not query.get("flow")
        ):
            raise ValueError(
                "SDMX requires flow and optional key/startPeriod/endPeriod"
            )
        yield SeriesRef(
            locator=str(query["flow"]) + "/" + str(query.get("key", "")),
            metadata=dict(query),
        )

    def fetch(self, ref):
        import sdmx
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        client = sdmx.Client(self.source)
        request = client.get(
            "data",
            ref.metadata["flow"],
            key=ref.metadata.get("key", ""),
            params={
                k: ref.metadata[k]
                for k in ("startPeriod", "endPeriod")
                if k in ref.metadata
            },
            dry_run=True,
        )
        parts = urlsplit(request.url)
        # The SDK defines provider URLs; user input never supplies a host.
        if parts.scheme != "https" or parts.hostname not in {
            "data-api.ecb.europa.eu",
            "ec.europa.eu",
            "api.statistiken.bundesbank.de",
            "api.statistiken.bundesbank.de",
        }:
            raise IntegrationError(
                "provider_endpoint_changed",
                "Review the SDMX provider endpoint before use",
            )
        base = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        response = (self.transport or HTTPSPageAdapter._request)(
            url=base,
            params=dict(parse_qsl(parts.query)),
            headers={"Accept": "application/vnd.sdmx.genericdata+xml;version=2.1"},
            timeout=20,
            max_bytes=self.max_bytes,
        )
        if int(response.get("status", 200)) != 200:
            raise IntegrationError("source_unavailable", "SDMX provider request failed")
        content = response["content"]
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        return RawSeries(
            ref, content, content_type="application/xml", source_url=request.url
        )

    def parse(self, raw):
        import sdmx

        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        # Reject external/internal entity declarations before the SDK XML reader.
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise IntegrationError("invalid_xml", "XML entities are forbidden")
        message = sdmx.read_sdmx(io.BytesIO(content))
        groups = {}
        count = 0
        for dataset in message.data:
            for observation in dataset.obs:
                count += 1
                if count > self.max_observations:
                    raise IntegrationError(
                        "observation_limit", "No truncated SDMX series published"
                    )
                dimensions = {
                    k: str(v.value) for k, v in observation.key.values.items()
                }
                period = dimensions.pop("TIME_PERIOD", dimensions.pop("TIME", None))
                if not period:
                    raise IntegrationError(
                        "missing_period", "Observation lacks a time dimension"
                    )
                attributes = {k: str(v.value) for k, v in observation.attrib.items()}
                key = digest(dimensions)
                entry = groups.setdefault(
                    key,
                    {"dimensions": dimensions, "observations": [], "attributes": {}},
                )
                value = (
                    float(observation.value) if observation.value is not None else None
                )
                if value is not None and not math.isfinite(value):
                    value = None
                if period in entry["attributes"]:
                    raise IntegrationError(
                        "duplicate_period", "Repeated period within SDMX series"
                    )
                entry["observations"].append(Observation(period, value))
                entry["attributes"][period] = attributes
        records = []
        for key, entry in sorted(groups.items()):
            dimensions = entry["dimensions"]
            frequency = {
                "A": "annual",
                "Q": "quarterly",
                "M": "monthly",
                "W": "weekly",
                "D": "daily",
            }.get(dimensions.get("FREQ"), "irregular")
            records.append(
                SeriesRecord(
                    series_id=self.provider + ":" + raw.ref.locator + ":" + key[:24],
                    provider=self.provider,
                    title=raw.ref.title or raw.ref.locator,
                    frequency=frequency,
                    as_of=raw.fetched_at,
                    observations=sorted(entry["observations"], key=lambda o: o.period),
                    unit=dimensions.get("UNIT"),
                    geography=dimensions.get("REF_AREA") or dimensions.get("GEO"),
                    source_url=raw.source_url,
                    metadata={
                        "dimensions": dimensions,
                        "observation_attributes": entry["attributes"],
                        "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
                        "sdmx_version": version("sdmx1"),
                        "vintage_semantics": "retrieval time; historical provider vintage not inferred",
                    },
                )
            )
        return records
