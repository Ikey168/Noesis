import gzip
import json
from pathlib import Path

import pytest

pytest.importorskip("sdmx")

from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
from src.ingestion.connectors.dataset.eurostat import EurostatConnector
from src.ingestion.connectors.dataset.sdmx import SDMXConnector

FIXTURES = Path("tests/fixtures/integrations")


def test_native_eurostat_dimensions_codelists_and_baseline_agree():
    connector = SDMXConnector("ESTAT")
    structure = connector.parse_structure(
        RawSeries(
            SeriesRef("NAMA_10_GDP"),
            gzip.decompress((FIXTURES / "estat-dsd-native.xml.gz").read_bytes()),
        )
    )
    ref = SeriesRef("nama_10_gdp/A.CP_MEUR.B1GQ.DE", metadata={"flow": "nama_10_gdp"})
    raw = RawSeries(ref, (FIXTURES / "estat-native.xml").read_bytes(), fetched_at=100)
    series = connector.parse(raw, structure=structure)[0]
    baseline = EurostatConnector()
    baseline_ref = next(
        baseline.discover({"dataset": "nama_10_gdp", "geography": "DE"})
    )
    expected = baseline.parse(
        RawSeries(baseline_ref, (FIXTURES / "estat-jsonstat-native.json").read_bytes())
    )[0]
    assert (
        [(o.period, o.value) for o in series.observations]
        == [(o.period, o.value) for o in expected.observations]
        == [("2023", 4254930.0)]
    )
    assert (
        series.frequency == "annual"
        and series.unit == "CP_MEUR"
        and series.geography == "DE"
    )
    assert series.metadata["observation_attributes"]["2023"]["OBS_FLAG"] == "p"
    definition = series.metadata["structure"]["definitions"][0]
    assert definition["dimensions"]["geo"]["labels"]["en"] == "Germany"
    assert (
        series.metadata["provider_prepared_at"]
        and series.metadata["provider_release_at"] is None
    )
    assert connector.parse(raw, structure=structure)[0].series_id == series.series_id


def test_native_ecb_and_bundesbank_attribute_units_and_frequency():
    for provider, name, locator in [
        ("ECB", "ecb-native.xml", "EXR/D.USD.EUR.SP00.A"),
        ("BBK", "bbk-native.xml", "BBEX3/D.USD.EUR.BB.AC.000"),
    ]:
        series = SDMXConnector(provider).parse(
            RawSeries(SeriesRef(locator), (FIXTURES / name).read_bytes())
        )[0]
        assert series.unit == "USD" and series.frequency == "daily"
        assert len(series.observations) == 2
        assert len(series.metadata["original_values"]) == 2
        assert series.metadata["common_attributes"]


def test_provider_diagnostics_limits_and_missing_status():
    connector = SDMXConnector(
        "BBK", transport=lambda **_: {"status": 400, "content": b""}
    )
    ref = next(
        connector.discover(
            {"flow": "BBEX3", "key": "D.USD.EUR.BB.AC.000", "lastNObservations": 2}
        )
    )
    with pytest.raises(ValueError, match="BBK returned HTTP 400") as caught:
        connector.fetch(ref)
    assert caught.value.code == "source_http_400"
    with pytest.raises(ValueError, match="budget"):
        list(connector.discover({"flow": "x", "lastNObservations": 1000000}))
    native = (
        (FIXTURES / "estat-native.xml")
        .read_bytes()
        .replace(b'value="4254930.0"', b'value="NaN"')
    )
    series = SDMXConnector("ESTAT").parse(RawSeries(SeriesRef("fixture"), native))[0]
    assert series.observations[0].value is None
    assert series.metadata["original_values"]["2023"] == "NaN"
    assert series.metadata["observation_attributes"]["2023"]["OBS_FLAG"] == "p"


def test_native_ingestion_archives_originals_and_replays():
    import duckdb
    from jsonschema import Draft7Validator

    from src.ingestion.connectors.dataset.store import ObservationStore

    conn = duckdb.connect()
    store = ObservationStore(conn)
    content = (FIXTURES / "estat-native.xml").read_bytes()
    connector = SDMXConnector(
        "ESTAT", transport=lambda **_: {"status": 200, "content": content}
    )
    query = {
        "flow": "nama_10_gdp",
        "key": "A.CP_MEUR.B1GQ.DE",
        "startPeriod": "2023",
        "endPeriod": "2023",
    }
    ref = next(connector.discover(query))
    raw = RawSeries(
        ref, content, source_url="https://ec.europa.eu/fixture", fetched_at=100
    )
    connector.fetch = lambda _: raw
    schema = json.loads(
        Path("contracts/schemas/jsonschema/dataset-series-v1.json").read_text()
    )
    Draft7Validator(schema).validate(connector.parse(raw)[0].to_dict())
    first = connector.ingest(query, store)
    assert connector.ingest(query, store) == first
    assert conn.execute("SELECT count(*) FROM dataset_observations").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone()[0] == 1
    revised = content.replace(b'value="4254930.0"', b'value="4254931.0"')
    raw = RawSeries(
        ref, revised, source_url="https://ec.europa.eu/fixture", fetched_at=200
    )
    connector.ingest(query, store)
    assert conn.execute(
        "SELECT value FROM dataset_observations ORDER BY as_of"
    ).fetchall() == [(4254930.0,), (4254931.0,)]
    assert conn.execute("SELECT count(*) FROM source_binary_blobs").fetchone()[0] == 2
    conn.close()
