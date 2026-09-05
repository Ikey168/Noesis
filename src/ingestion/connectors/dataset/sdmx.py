"""Optional SDMX-native statistical connector with bounded raw capture."""

import io
import math
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from services.ingest.common.series_model import Observation, SeriesRecord
from src.integrations.common import IntegrationError, digest, version

from .base import DatasetConnector, RawSeries, SeriesRef


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
            or set(query)
            - {"flow", "key", "startPeriod", "endPeriod", "lastNObservations"}
            or not query.get("flow")
        ):
            raise ValueError(
                "SDMX requires flow and optional key/startPeriod/endPeriod"
            )
        if "lastNObservations" in query and (
            type(query["lastNObservations"]) is not int
            or not 1 <= query["lastNObservations"] <= self.max_observations
        ):
            raise ValueError("lastNObservations exceeds the observation budget")
        yield SeriesRef(
            locator=str(query["flow"]) + "/" + str(query.get("key", "")),
            metadata=dict(query),
        )

    def fetch(self, ref):
        import sdmx

        client = sdmx.Client(self.source)
        request = client.get(
            "data",
            ref.metadata["flow"],
            key=ref.metadata.get("key", ""),
            params={
                k: ref.metadata[k]
                for k in ("startPeriod", "endPeriod", "lastNObservations")
                if k in ref.metadata
            },
            dry_run=True,
        )
        return self._fetch_request(
            ref, request, "application/vnd.sdmx.genericdata+xml;version=2.1"
        )

    def fetch_structure(self, resource, resource_id):
        """Fetch one bounded native dataflow, data structure or code list."""
        import sdmx

        if resource not in {"dataflow", "datastructure", "codelist"}:
            raise ValueError("Unsupported SDMX structure resource")
        request = sdmx.Client(self.source).get(resource, resource_id, dry_run=True)
        ref = SeriesRef(
            resource + "/" + resource_id,
            metadata={"resource": resource, "resource_id": resource_id},
        )
        return self._fetch_request(
            ref, request, "application/vnd.sdmx.structure+xml;version=2.1"
        )

    def parse_structure(self, raw):
        """Map structure identities, dimensions and multilingual code labels.

        The caller retains RawSeries for unmodeled SDMX annotations; the mapping
        references its exact bytes and does not claim to be a full SDMX serializer.
        """
        import sdmx

        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if (
            len(content) > self.max_bytes
            or b"<!DOCTYPE" in content.upper()
            or b"<!ENTITY" in content.upper()
        ):
            raise IntegrationError(
                "invalid_structure",
                "Structure exceeds byte limit or declares XML entities",
            )
        message = sdmx.read_sdmx(io.BytesIO(content))
        result = {
            "source_url": raw.source_url,
            "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
            "retrieved_at_ms": raw.fetched_at,
            "provider": self.provider,
            "sdmx_version": version("sdmx1"),
            "dataflows": {},
            "structures": {},
            "codelists": {},
            "mapping_coverage": "identities, dimensions, code labels; other structural annotations remain in RawSeries",
        }
        for key, flow in message.dataflow.items():
            result["dataflows"][key] = {
                "id": flow.id,
                "version": flow.version,
                "names": dict(flow.name.localizations),
                "structure_id": flow.structure.id if flow.structure else None,
            }
        for key, structure in message.structure.items():
            result["structures"][key] = {
                "id": structure.id,
                "version": structure.version,
                "names": dict(structure.name.localizations),
                "dimensions": {
                    dimension.id: getattr(
                        getattr(dimension.local_representation, "enumerated", None),
                        "id",
                        None,
                    )
                    for dimension in structure.dimensions.components
                },
            }
        count = 0
        for key, codelist in message.codelist.items():
            count += len(codelist.items)
            if count > 100000:
                raise IntegrationError(
                    "structure_limit", "SDMX structure exceeds code budget"
                )
            result["codelists"][key] = {
                "id": codelist.id,
                "version": codelist.version,
                "names": dict(codelist.name.localizations),
                "codes": {
                    code.id: dict(code.name.localizations)
                    for code in codelist.items.values()
                },
            }
        return result

    def ingest(self, query, store, *, structure=None):
        """Archive native bytes and publish observations in one transaction."""
        from src.ingestion.snapshots import SnapshotStore

        snapshots = SnapshotStore(store._conn)
        results = []
        for ref in self.discover(query):
            raw = self.fetch(ref)
            records = self.parse(raw, structure=structure)
            store._conn.execute("BEGIN")
            try:
                captured = snapshots.snapshot_bytes(
                    raw.source_url,
                    raw.content,
                    raw.fetched_at,
                    content_type=raw.content_type,
                    final_url=raw.source_url,
                )
                for record in records:
                    record.metadata["native_snapshot"] = captured
                written = store.upsert_many(records)
                store._conn.execute("COMMIT")
            except BaseException:
                store._conn.execute("ROLLBACK")
                raise
            results.append(
                {
                    "snapshot": captured,
                    "series_ids": [record.series_id for record in records],
                    "observations_written": written,
                }
            )
        return results

    def _fetch_request(self, ref, request, accept):
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        parts = urlsplit(request.url)
        # The SDK defines provider URLs; user input never supplies a host.
        if parts.scheme != "https" or parts.hostname not in {
            "data-api.ecb.europa.eu",
            "ec.europa.eu",
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
            headers={
                "Accept": accept,
                "User-Agent": "Noesis/1.0 (+https://github.com/Ikey168/Noesis)",
            },
            timeout=20,
            max_bytes=self.max_bytes,
        )
        if int(response.get("status", 200)) != 200:
            status = int(response.get("status", 200))
            raise IntegrationError(
                "source_http_" + str(status),
                f"SDMX {self.source} returned HTTP {status}; check key, frequency and period bounds",
            )
        content = response["content"]
        content = content.encode() if isinstance(content, str) else content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        return RawSeries(
            ref, content, content_type="application/xml", source_url=request.url
        )

    @staticmethod
    def _structure_projection(structure, dimensions):
        if structure is None:
            return {"status": "not_requested"}
        projections = []
        for definition in structure["structures"].values():
            labels = {}
            for dimension, code in dimensions.items():
                codelist_id = definition["dimensions"].get(dimension)
                codelist = structure["codelists"].get(codelist_id, {})
                labels[dimension] = {
                    "code": code,
                    "codelist_id": codelist_id,
                    "codelist_version": codelist.get("version"),
                    "labels": codelist.get("codes", {}).get(code, {}),
                }
            projections.append(
                {
                    "id": definition["id"],
                    "version": definition["version"],
                    "dimensions": labels,
                }
            )
        return {
            "source_url": structure["source_url"],
            "raw_sha256": structure["raw_sha256"],
            "definitions": projections,
            "status": "mapped" if projections else "no_data_structure",
        }

    def parse(self, raw, *, structure=None):
        import sdmx

        content = raw.content.encode() if isinstance(raw.content, str) else raw.content
        if len(content) > self.max_bytes:
            raise IntegrationError("response_limit", "SDMX response exceeds budget")
        # Reject external/internal entity declarations before the SDK XML reader.
        if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
            raise IntegrationError("invalid_xml", "XML entities are forbidden")
        message = sdmx.read_sdmx(io.BytesIO(content))
        if structure is not None and any(
            dataset.structured_by.id not in structure["structures"]
            for dataset in message.data
        ):
            raise IntegrationError(
                "structure_mismatch",
                "SDMX data structure does not match the supplied definitions",
            )
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
                    {
                        "dimensions": dimensions,
                        "observations": [],
                        "attributes": {},
                        "raw_values": {},
                        "dataset_metadata": [],
                    },
                )
                dataset_metadata = {
                    "action": str(dataset.action.value) if dataset.action else None,
                    "valid_from": str(dataset.valid_from)
                    if dataset.valid_from
                    else None,
                    "structure_id": dataset.structured_by.id
                    if dataset.structured_by
                    else None,
                }
                if dataset_metadata not in entry["dataset_metadata"]:
                    entry["dataset_metadata"].append(dataset_metadata)
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
                entry["raw_values"][period] = (
                    str(observation.value) if observation.value is not None else None
                )
        records = []
        for key, entry in sorted(groups.items()):
            dimensions = entry["dimensions"]
            normalized_dimensions = {k.upper(): v for k, v in dimensions.items()}
            common_attributes = {}
            if entry["attributes"]:
                first = next(iter(entry["attributes"].values()))
                common_attributes = {
                    k: v
                    for k, v in first.items()
                    if all(attrs.get(k) == v for attrs in entry["attributes"].values())
                }
            frequency = {
                "A": "annual",
                "Q": "quarterly",
                "M": "monthly",
                "W": "weekly",
                "D": "daily",
            }.get(
                normalized_dimensions.get("FREQ")
                or normalized_dimensions.get("BBK_STD_FREQ")
                or common_attributes.get("FREQ"),
                "irregular",
            )
            records.append(
                SeriesRecord(
                    series_id=self.provider + ":" + raw.ref.locator + ":" + key[:24],
                    provider=self.provider,
                    title=raw.ref.title or raw.ref.locator,
                    frequency=frequency,
                    as_of=raw.fetched_at,
                    observations=sorted(entry["observations"], key=lambda o: o.period),
                    unit=normalized_dimensions.get("UNIT")
                    or common_attributes.get("UNIT")
                    or common_attributes.get("BBK_UNIT"),
                    geography=normalized_dimensions.get("REF_AREA")
                    or normalized_dimensions.get("GEO"),
                    source_url=raw.source_url,
                    metadata={
                        "dimensions": dimensions,
                        "common_attributes": common_attributes,
                        "original_values": entry["raw_values"],
                        "provider_prepared_at": str(message.header.prepared)
                        if message.header.prepared
                        else None,
                        "provider_release_at": None,
                        "dataset_metadata": entry["dataset_metadata"],
                        "provider_extracted_at": str(message.header.extracted)
                        if message.header.extracted
                        else None,
                        "dataflow_id": raw.ref.metadata.get("flow")
                        or raw.ref.locator.split("/", 1)[0],
                        "structure": self._structure_projection(structure, dimensions),
                        "observation_attributes": entry["attributes"],
                        "raw_sha256": __import__("hashlib").sha256(content).hexdigest(),
                        "sdmx_version": version("sdmx1"),
                        "vintage_semantics": "retrieval time; historical provider vintage not inferred",
                    },
                )
            )
        return records
