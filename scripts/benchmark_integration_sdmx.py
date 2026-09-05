"""Replay captured native SDMX providers and compare Eurostat JSON-stat values."""

import argparse
import gzip
import hashlib
import json
import resource
import statistics
import time
from pathlib import Path

from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
from src.ingestion.connectors.dataset.eurostat import EurostatConnector
from src.ingestion.connectors.dataset.sdmx import SDMXConnector


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    fixtures = Path("tests/fixtures/integrations")
    captures = []
    if args.live:
        from urllib.parse import parse_qsl, urlsplit, urlunsplit

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        fixtures = args.out.with_suffix(".captures")
        fixtures.mkdir(parents=True, exist_ok=True)
        for provider, name, spec in [
            (
                "ECB",
                "ecb-native.xml",
                {
                    "flow": "EXR",
                    "key": "D.USD.EUR.SP00.A",
                    "startPeriod": "2024-01-02",
                    "endPeriod": "2024-01-03",
                },
            ),
            (
                "ESTAT",
                "estat-native.xml",
                {
                    "flow": "nama_10_gdp",
                    "key": "A.CP_MEUR.B1GQ.DE",
                    "startPeriod": "2023",
                    "endPeriod": "2023",
                },
            ),
            (
                "BBK",
                "bbk-native.xml",
                {"flow": "BBEX3", "key": "D.USD.EUR.BB.AC.000", "lastNObservations": 2},
            ),
        ]:
            client = SDMXConnector(provider)
            raw = client.fetch(next(client.discover(spec)))
            (fixtures / name).write_bytes(raw.content)
            captures.append(
                {
                    "fixture": name,
                    "source_url": raw.source_url,
                    "fetched_at_ms": raw.fetched_at,
                    "sha256": hashlib.sha256(raw.content).hexdigest(),
                }
            )
        raw = SDMXConnector("ESTAT").fetch_structure("datastructure", "NAMA_10_GDP")
        captures.append(
            {
                "fixture": "estat-dsd-native.xml.gz",
                "source_url": raw.source_url,
                "fetched_at_ms": raw.fetched_at,
                "sha256_uncompressed": hashlib.sha256(raw.content).hexdigest(),
            }
        )
        (fixtures / "estat-dsd-native.xml.gz").write_bytes(
            gzip.compress(raw.content, mtime=0)
        )

        def fetch_baseline(url):
            parts = urlsplit(url)
            response = HTTPSPageAdapter._request(
                url=urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                params=dict(parse_qsl(parts.query)),
                headers={
                    "User-Agent": "Noesis/1.0 (+https://github.com/Ikey168/Noesis)"
                },
                timeout=20,
                max_bytes=2000000,
            )
            if response.get("status") != 200:
                raise ValueError("Eurostat baseline request failed")
            (fixtures / "estat-jsonstat-native.json").write_bytes(response["content"])
            captures.append(
                {
                    "fixture": "estat-jsonstat-native.json",
                    "source_url": url,
                    "fetched_at_ms": int(time.time() * 1000),
                    "sha256": hashlib.sha256(response["content"]).hexdigest(),
                }
            )
            return response["content"]

        baseline_client = EurostatConnector(http_get=fetch_baseline)
        baseline_client.fetch(
            next(
                baseline_client.discover(
                    {
                        "dataset": "nama_10_gdp",
                        "geography": "DE",
                        "unit": "CP_MEUR",
                        "na_item": "B1GQ",
                        "freq": "A",
                        "time": "2023",
                    }
                )
            )
        )
    structure = SDMXConnector("ESTAT").parse_structure(
        RawSeries(
            SeriesRef("NAMA_10_GDP"),
            gzip.decompress((fixtures / "estat-dsd-native.xml.gz").read_bytes()),
        )
    )
    runs = []
    for provider, name, locator in [
        ("ECB", "ecb-native.xml", "EXR/D.USD.EUR.SP00.A"),
        ("ESTAT", "estat-native.xml", "nama_10_gdp/A.CP_MEUR.B1GQ.DE"),
        ("BBK", "bbk-native.xml", "BBEX3/D.USD.EUR.BB.AC.000"),
    ]:
        connector = SDMXConnector(provider)
        content = (fixtures / name).read_bytes()
        raw = RawSeries(SeriesRef(locator), content, fetched_at=100)
        timings = []
        for _ in range(20):
            started = time.perf_counter()
            series = connector.parse(
                raw, structure=structure if provider == "ESTAT" else None
            )[0]
            timings.append((time.perf_counter() - started) * 1000)
        runs.append(
            {
                "provider": provider,
                "source_fixture": name,
                "native_sha256": hashlib.sha256(content).hexdigest(),
                "series_id": series.series_id,
                "unit": series.unit,
                "frequency": series.frequency,
                "geography": series.geography,
                "observations": [
                    {"period": o.period, "value": o.value} for o in series.observations
                ],
                "metadata": series.metadata,
                "parse_p50_ms": statistics.median(timings),
                "parse_p95_ms": sorted(timings)[18],
            }
        )
    baseline = EurostatConnector()
    ref = next(baseline.discover({"dataset": "nama_10_gdp", "geography": "DE"}))
    baseline_values = [
        {"period": o.period, "value": o.value}
        for o in baseline.parse(
            RawSeries(ref, (fixtures / "estat-jsonstat-native.json").read_bytes())
        )[0].observations
    ]
    if baseline_values != runs[1]["observations"]:
        raise ValueError("Eurostat native protocols disagree on overlapping values")
    output = {
        "mode": "live-and-replay" if args.live else "captured-native-replay",
        "capture_provenance": captures,
        "replay_retrieval_timestamp": "100 is an explicit synthetic timestamp for deterministic parser replay; live fetch times are in capture_provenance",
        "runs": runs,
        "eurostat_baseline_values": baseline_values,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "decision": "Adopt optional SDMX connector for verified ECB, Eurostat and Bundesbank data paths with explicit structures. Keep legacy Eurostat default. Small native captures establish protocol fidelity, not complete endpoint coverage or historical vintages.",
        "limitations": [
            "DataStructure mapping retains identity/dimensions/code labels; other annotations require retained RawSeries.",
            "BBK01 documentation example keys returned no data; period syntax must match frequency.",
            "Publication/release time is not inferred from prepared/retrieval timestamps.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(
        json.dumps(
            [
                {k: r[k] for k in ["provider", "parse_p50_ms", "parse_p95_ms"]}
                for r in runs
            ]
        )
    )


if __name__ == "__main__":
    main()
