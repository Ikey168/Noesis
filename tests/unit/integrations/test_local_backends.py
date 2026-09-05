import pytest


@pytest.fixture(autouse=True)
def optional_backend(request):
    packages = {
        "lingua": "lingua",
        "transform": "pyproj",
        "topology": "shapely",
        "resolver": "rapidfuzz",
        "cpsat": "ortools",
        "pint": "pint",
        "pandera": "pandera",
        "adwin": "river",
        "minhash": "datasketch",
        "rocrate": "rocrate",
        "pandoc": "pypandoc",
    }
    for key, package in packages.items():
        if key in request.node.name:
            pytest.importorskip(
                package,
                reason="install noesis-evidence[workflow-integrations] for real backend tests",
            )


def test_lingua_unicode_offsets_and_abstention():
    from src.integrations.text import detect_language

    text = "Über die Förderung entscheidet die Berliner Verwaltung nach Prüfung des Antrags."
    result = detect_language(text)
    assert result["language"] == "de"
    assert all(text[s["start"] : s["end"]] for s in result["segments"])
    assert detect_language("")["status"] == "uncertain"
    with pytest.raises(ValueError):
        detect_language("text", minimum_margin=float("nan"))


def test_transform_known_utm_central_meridian_and_roundtrip():
    from src.integrations.spatial import transform_geometry

    # UTM zone 33N central meridian: easting 500 km, northing zero is 15E, 0N.
    point = {"type": "Point", "coordinates": [500000, 0]}
    a = transform_geometry(point, "EPSG:25833")
    assert a["result"]["geometry"]["coordinates"] == pytest.approx([15, 0], abs=1e-8)
    b = transform_geometry(a["result"]["geometry"], "EPSG:4326", "EPSG:25833")
    assert b["result"]["geometry"]["coordinates"] == pytest.approx(
        [500000, 0], abs=0.001
    )
    assert transform_geometry(point, "EPSG:25833")["sha256"] == a["sha256"]


def test_topology_holes_and_boundary():
    from src.integrations.spatial import topology

    polygon = {
        "type": "Polygon",
        "coordinates": [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [1, 2], [2, 2], [2, 1], [1, 1]],
        ],
    }
    assert not topology(
        "contains", polygon, {"type": "Point", "coordinates": [1.5, 1.5]}
    )["result"]["contains"]
    assert not topology("contains", polygon, {"type": "Point", "coordinates": [0, 2]})[
        "result"
    ]["contains"]
    assert topology("covers", polygon, {"type": "Point", "coordinates": [0, 2]})[
        "result"
    ]["covers"]


def test_resolver_requires_explicit_rapidfuzz_threshold():
    from src.knowledge_graph.foundation.resolution import EntityResolver

    with pytest.raises(ValueError):
        EntityResolver(fuzzy_backend="rapidfuzz")
    resolver = EntityResolver(fuzzy_backend="rapidfuzz", fuzzy_threshold=0.9)
    assert resolver._fuzzy_ratio("Berliner", "Berliner") == 1


def test_cpsat_finds_feasible_combination_and_infeasibility():
    from src.integrations.planning import select_sources

    def c(identity, cost, parts, group):
        return {
            "capability": {"source_id": identity, "dependency_group": group},
            "projected_cost": cost,
            "covered_parts": parts,
        }

    rows = [
        c("expensive", 4, [0], "a"),
        c("both", 3, [0, 1], "a"),
        c("independent", 1, [1], "b"),
    ]
    constraints = {
        "budget": 4,
        "max_sources": 2,
        "min_independence": 2,
        "required_sources": [],
    }
    result = select_sources(rows, constraints, 2)
    assert result["status"] == "OPTIMAL"
    assert set(result["selected_ids"]) == {"both", "independent"}
    assert (
        select_sources(rows, {**constraints, "budget": 0}, 2)["status"] == "INFEASIBLE"
    )


def test_pint_compound_offsets_and_dimensions():
    from src.integrations.units import convert_physical

    assert (
        convert_physical("36", "kilometer/hour", "meter/second")["result"]["value"]
        == "10.000000"
    )
    assert convert_physical("0", "degC", "kelvin")["result"]["value"] == "273.150000"
    with pytest.raises(ValueError):
        convert_physical("1", "kilogram", "meter")
    with pytest.raises(ValueError):
        convert_physical("1", "EUR", "USD")


def test_pandera_reports_row_errors_without_coercion():
    from src.integrations.validation import validate_rows

    schema = [{"name": "count", "type": "integer", "minimum": 0}]
    result = validate_rows([{"count": 2}, {"count": -1}], schema)
    assert not result["result"]["valid"]
    assert result["result"]["failures"][0]["index"] == 1
    assert validate_rows([{"count": 3}], schema)["result"]["valid"]


def test_adwin_replay_deduplication_and_late_events():
    from src.integrations.drift import detect_drift

    points = [
        {"id": str(i), "timestamp_ms": i, "value": 0 if i < 300 else 1}
        for i in range(600)
    ]
    first = detect_drift(points)
    assert first["result"]["events"]
    assert detect_drift(points + [points[-1]])["sha256"] == first["sha256"]
    with pytest.raises(ValueError):
        detect_drift(points + [{"id": "late", "timestamp_ms": 2, "value": 1}])


def test_minhash_preserves_provenance_and_rebuild_removes_deleted_ids():
    from src.integrations.reuse import candidate_pairs

    signals = {
        "a": {"word_fingerprints": ["same", "words"], "publisher_owner": "owner"},
        "b": {"word_fingerprints": ["same", "words"]},
        "c": {"word_fingerprints": ["different"], "publisher_owner": "owner"},
    }
    pairs, metadata = candidate_pairs(signals)
    assert ("a", "b") in pairs and ("a", "c") in pairs
    assert not any(
        "b" in p
        for p in candidate_pairs({k: v for k, v in signals.items() if k != "b"})[0]
    )


def test_annotation_offsets_revision_and_identity():
    from src.integrations.annotation import export_tasks, import_annotations
    import copy

    tasks = export_tasks(
        [{"task_id": "a", "revision_id": "r1", "text": "Über Berlin"}], labels=["place"]
    )
    returned = copy.deepcopy(tasks)
    returned[0]["annotations"] = [
        {
            "id": 1,
            "completed_by": 9,
            "result": [
                {
                    "type": "labels",
                    "value": {
                        "start": 5,
                        "end": 11,
                        "text": "Berlin",
                        "labels": ["place"],
                    },
                }
            ],
        }
    ]
    result = import_annotations(
        tasks, returned, reviewer_mapping={"9": "alice"}, current_revisions={"a": "r1"}
    )
    assert result[0]["status"] == "pending-review" and not result[0]["human_verified"]
    with pytest.raises(ValueError):
        import_annotations(
            tasks,
            returned,
            reviewer_mapping={"9": "alice"},
            current_revisions={"a": "r2"},
        )


def test_rocrate_roundtrip_native_manifest(tmp_path):
    import base64, json, zipfile, io
    from src.integrations.export import export_rocrate

    package = {
        "package_id": "p",
        "status": "complete",
        "content_hash": "abc",
        "members": [],
    }
    result = export_rocrate(package)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["bytes_b64"]))) as z:
        assert json.loads(z.read("native-package.json")) == package
        assert "ro-crate-metadata.json" in z.namelist()


def test_pandoc_docx_preserves_text_and_citation():
    import base64, io, zipfile
    from src.integrations.export import render_report

    content = {
        "title": "Förderung",
        "sections": [
            {
                "title": "Berlin",
                "assertions": [
                    {"text": "Der Bericht enthält Daten.", "citations": ["ref1"]}
                ],
            }
        ],
        "bibliography": [{"id": "ref1", "text": "Authored reference"}],
        "limitations": ["Unverified support"],
    }
    exported = {"report": {"content": content}, "sha256": "native"}
    rendered = render_report(
        exported,
        references=[
            {
                "id": "ref1",
                "type": "report",
                "title": "Statistik Berlin",
                "author": [{"literal": "Amt für Statistik"}],
                "issued": {"date-parts": [[2025]]},
            }
        ],
    )
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(rendered["bytes_b64"]))) as z:
        xml = z.read("word/document.xml").decode()
        assert (
            "Förderung" in xml
            and "Statistik Berlin" in xml
            and "Unverified support" in xml
        )
