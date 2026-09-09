"""Geometry reference cases and actual ALKIS Berlin boundary simplification."""

import argparse
import gzip
import hashlib
import json
import statistics
import time
from pathlib import Path

import duckdb

from src.integrations.common import version
from src.integrations.spatial import simplify_geometry, topology, transform_geometry
from src.kb.geospatial import CALCULATE_SCOPE, WRITE_SCOPE, GeospatialStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    fixtures = Path("tests/fixtures/integrations")
    native = json.loads(
        gzip.decompress((fixtures / "berlin-mitte-native.json.gz").read_bytes())
    )["features"][0]
    source = json.loads((fixtures / "berlin-mitte-provenance.json").read_text())
    geographic = transform_geometry(native["geometry"], "EPSG:25833")["result"][
        "geometry"
    ]
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        result = simplify_geometry(geographic, 10, projected_crs="EPSG:25833")
        timings.append((time.perf_counter() - started) * 1000)
    import shapely
    from shapely.geometry import shape

    reduced = shape(result["result"]["geometry"])
    store = GeospatialStore(duckdb.connect(), now=lambda: 100)
    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[13, 52], [13.1, 52], [13.1, 52.1], [13, 52.1], [13, 52]],
            [
                [13.02, 52.02],
                [13.04, 52.02],
                [13.04, 52.04],
                [13.02, 52.04],
                [13.02, 52.02],
            ],
        ],
    }
    saved = store.store_geometry(
        "berlin",
        polygon,
        place_id=None,
        crs="EPSG:4326",
        precision_m=0,
        simplified_from=None,
        disputed=False,
        admin_hierarchy=[],
        source={"type": "authored-reference"},
        evidence=[],
        scopes={WRITE_SCOPE},
        principal_id="fixture",
    )
    reference_cases = []
    for point, expected in [
        ([13.01, 52.01], True),
        ([13.03, 52.03], False),
        ([13, 52.05], False),
        ([13.02, 52.03], False),
    ]:
        records = {}
        for backend in ("stdlib", "shapely"):
            started = time.perf_counter()
            run = store.relation(
                "berlin",
                "contains",
                saved["geometry_id"],
                point,
                backend=backend,
                scopes={CALCULATE_SCOPE},
                principal_id="fixture",
            )
            records[backend] = {
                "elapsed_ms": (time.perf_counter() - started) * 1000,
                "result": run["result"],
            }
        assert records["shapely"]["result"]["contains"] == expected
        reference_cases.append(
            {"point": point, "strict_contains_expected": expected, "backends": records}
        )
    boundary_cover = topology(
        "covers", polygon, {"type": "Point", "coordinates": [13, 52.05]}
    )
    # Both store backends receive the same first Polygon from the native multipart boundary.
    shared_polygon = {"type": "Polygon", "coordinates": geographic["coordinates"][0]}
    shared = store.store_geometry(
        "berlin",
        shared_polygon,
        place_id=None,
        crs="EPSG:4326",
        precision_m=1,
        simplified_from=None,
        disputed=False,
        admin_hierarchy=["DE-BE"],
        source={"native_sha256": source["sha256"], "selected_polygon": 0},
        evidence=[],
        scopes={WRITE_SCOPE},
        principal_id="fixture",
    )
    comparisons = {}
    projected_original = shape(
        transform_geometry(shared_polygon, "EPSG:4326", "EPSG:25833")["result"][
            "geometry"
        ]
    )
    for backend in ("stdlib", "shapely"):
        elapsed = []
        for _ in range(10):
            started = time.perf_counter()
            simplified = store.simplify(
                "berlin",
                shared["geometry_id"],
                10,
                backend=backend,
                projected_crs="EPSG:25833" if backend == "shapely" else None,
                scopes={WRITE_SCOPE},
                principal_id="fixture",
            )
            elapsed.append((time.perf_counter() - started) * 1000)
        projected_result = shape(
            transform_geometry(simplified["geometry"], "EPSG:4326", "EPSG:25833")[
                "result"
            ]["geometry"]
        )
        comparisons[backend] = {
            "median_ms": statistics.median(elapsed),
            "repetitions": 10,
            "geometry_id": simplified["geometry_id"],
            "source_geometry_id": shared["geometry_id"],
            "valid": bool(projected_result.is_valid),
            "coordinates": int(shapely.get_num_coordinates(projected_result)),
            "discrete_hausdorff_m": float(
                shapely.hausdorff_distance(
                    projected_original, projected_result, densify=0.25
                )
            ),
        }
    report = {
        "shapely_version": version("shapely"),
        "geos_version": shapely.geos_version_string,
        "reference_cases": reference_cases,
        "reference_provenance": "Authored axis-aligned rectangles with analytically specified topology; not statistical ground truth",
        "boundary_covers": boundary_cover,
        "store_simplification_comparison": comparisons,
        "native_berlin_case": {
            "source": source,
            "feature_id": native["id"],
            "repetitions": 20,
            "median_ms": statistics.median(timings),
            "p95_ms": sorted(timings)[18],
            "original_coordinates": int(shapely.get_num_coordinates(shape(geographic))),
            "simplified_coordinates": int(shapely.get_num_coordinates(reduced)),
            "valid": bool(reduced.is_valid),
            "receipt": result,
        },
        "decision": "adopt explicit optional topology and projected simplification; preserve originals, reject repairs and unsupported GeometryCollection/dateline operations",
    }
    output = Path(args.out)
    archived_receipt = json.dumps(
        report["native_berlin_case"].pop("receipt"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    receipt_path = output.with_suffix(".receipt.json.gz")
    receipt_path.write_bytes(gzip.compress(archived_receipt, mtime=0))
    report["native_berlin_case"]["receipt_artifact"] = {
        "path": receipt_path.name,
        "uncompressed_sha256": hashlib.sha256(archived_receipt).hexdigest(),
        "receipt_sha256": result["sha256"],
        "sampled_displacement_m": result["result"]["discrete_hausdorff_m"],
        "effective_tolerance_m": result["result"]["effective_tolerance_m"],
    }
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in report["native_berlin_case"].items()
                if k not in {"source", "receipt"}
            }
        )
    )
    store.conn.close()


if __name__ == "__main__":
    main()
