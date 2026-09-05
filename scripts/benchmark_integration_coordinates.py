"""Offline pyproj validation against published Berlin Umweltatlas coordinates."""

import argparse
import json
import math
import resource
import statistics
import time
from pathlib import Path

from src.integrations.spatial import transform_geometry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    fixture = json.loads(
        Path("tests/fixtures/integrations/berlin-coordinate-reference.json").read_text()
    )
    cases = []
    for point in fixture["points"]:
        coordinates = [
            v[0] + v[1] / 60 + v[2] / 3600
            for v in (point["longitude_dms"], point["latitude_dms"])
        ]
        geometry = {"type": "Point", "coordinates": coordinates}
        latency = []
        for _ in range(30):
            started = time.perf_counter()
            result = transform_geometry(geometry, "EPSG:4326", "EPSG:25833")
            latency.append((time.perf_counter() - started) * 1000)
        error = math.dist(
            result["result"]["geometry"]["coordinates"], point["epsg25833"]
        )
        if error > fixture["tolerance_m"]:
            raise ValueError("Published coordinate reference tolerance exceeded")
        cases.append(
            {
                "name": point["name"],
                "error_m": error,
                "receipt": result,
                "latency_p50_ms": statistics.median(latency),
                "latency_p95_ms": sorted(latency)[28],
            }
        )
    output = {
        "reference": fixture,
        "cases": cases,
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "decision": "Adopt as an explicit offline EPSG:25833/4326 import option at the declared source precision. Published rounded coordinates validate regional axis order and metre-scale correctness; no survey-grade or other-CRS accuracy claim. Missing best grids fail explicitly; network disabled. No planar metric operations added.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps([{k: v for k, v in c.items() if k != "receipt"} for c in cases]))


if __name__ == "__main__":
    main()
