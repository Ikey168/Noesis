"""Authored, fictional provider responses for the Materials pack (MT04-MT07, MT13).

Every response follows the provider's documented shape (see
``docs/development/materials-evidence/source-audit.md``); every ID is in a
fictional 99xxxxx range and every number is invented. Nothing here is a
capture. ``python -m tests.unit.materials.fixture_builder`` rewrites the
pinned fixtures under ``tests/fixtures/source_packs/`` and prints the hashes
``config/source_packs/materials.json`` pins.

The world: rutile and anatase TiO2 (a polymorph pair that must never merge),
corundum Al2O3, three computed sources with different functionals, two COD
experimental structures, and a WebBook species with measured and evaluated
values at two temperatures.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.ingestion.materials_sources import fixture_request

ROOT = Path(__file__).resolve().parents[3]
DATE = "Mon, 01 Feb 2099 12:00:00 GMT"
MP_DOCUMENTS = [
    {
        "kind": "summary",
        "path": "/materials/summary/",
        "params": {
            "chemsys": "O-Ti,Al-O",
            "_limit": "50",
            "_fields": "material_id,formula_pretty,symmetry,structure,nsites,volume,density,band_gap,"
            "database_IDs,deprecated,last_updated",
        },
    },
    {
        "kind": "thermo",
        "path": "/materials/thermo/",
        "params": {
            "chemsys": "O-Ti,Al-O",
            "_limit": "50",
            "_fields": "material_id,thermo_type,formation_energy_per_atom,energy_above_hull",
        },
    },
    {
        "kind": "elasticity",
        "path": "/materials/elasticity/",
        "params": {
            "chemsys": "O-Ti,Al-O",
            "_limit": "50",
            "_fields": "material_id,bulk_modulus,shear_modulus",
        },
    },
]
JARVIS_DOCUMENTS = [
    {"kind": "snapshot", "path": "/ndownloader/files/99990001", "params": {}}
]
OQMD_DOCUMENTS = [
    {
        "kind": "formationenergy",
        "path": "/oqmdapi/formationenergy",
        "params": {
            "composition": "TiO2",
            "limit": "50",
            "fields": "name,entry_id,icsd_id,spacegroup,natoms,volume,delta_e,stability,band_gap,fit",
        },
    },
]
WEBBOOK_DOCUMENTS = [
    {
        "kind": "species",
        "path": "/cgi/cbook.cgi",
        "params": {"ID": "C9990001", "Units": "SI", "Mask": "6"},
    }
]
COD_DOCUMENTS = [
    {
        "kind": "search",
        "path": "/cod/result",
        "params": {"formula": "O2 Ti", "format": "json"},
    }
]


def page(document, body, *, status=200, date=DATE):
    return {
        "request": fixture_request(document),
        "status": status,
        "headers": {"date": date},
        "body": body if isinstance(body, str) else json.dumps(body, ensure_ascii=False),
    }


def _mp_summary(
    mid,
    formula,
    number,
    symbol,
    system,
    nsites,
    volume,
    gap,
    density,
    lattice,
    *,
    icsd=None,
    deprecated=False,
    updated="2099-01-10 00:00:00",
):
    return (
        f'{{"material_id": "{mid}", "formula_pretty": "{formula}", "symmetry": {{"crystal_system": "{system}", '
        f'"symbol": "{symbol}", "number": {number}}}, "structure": {{"lattice": {{"a": {lattice[0]}, '
        f'"b": {lattice[1]}, "c": {lattice[2]}, "alpha": 90.0, "beta": 90.0, "gamma": {lattice[3]}}}}}, '
        f'"nsites": {nsites}, "volume": {volume}, "density": {density}, "band_gap": {gap}, '
        f'"database_IDs": {{"icsd": {json.dumps(icsd or [])}}}, "deprecated": {json.dumps(deprecated)}, '
        f'"last_updated": "{updated}"}}'
    )


def mp_pages(release=1):
    """Materials Project summary/thermo/elasticity responses; release 2 corrects, adds and deprecates."""

    db_version = "2099.1.0" if release == 1 else "2099.2.0"
    gap = "1.780" if release == 1 else "1.800"
    summaries = [
        _mp_summary(
            "mp-990001",
            "TiO2",
            136,
            "P4_2/mnm",
            "Tetragonal",
            6,
            "64.10",
            gap,
            "4.130",
            ("4.640", "4.640", "2.978", "90.0"),
            icsd=["icsd-99001"],
        ),
        _mp_summary(
            "mp-990002",
            "TiO2",
            141,
            "I4_1/amd",
            "Tetragonal",
            12,
            "139.00",
            "2.050",
            "3.820",
            ("3.806", "3.806", "9.600", "90.0"),
            deprecated=release == 2,
        ),
        _mp_summary(
            "mp-990003",
            "Al2O3",
            167,
            "R-3c",
            "Trigonal",
            10,
            "86.00",
            "5.850",
            "3.940",
            ("4.810", "4.810", "13.120", "120.0"),
        ),
    ]
    thermo = [
        '{"material_id": "mp-990001", "thermo_type": "GGA_GGA+U", "formation_energy_per_atom": -3.510, '
        '"energy_above_hull": 0.0}',
        '{"material_id": "mp-990001", "thermo_type": "R2SCAN", "formation_energy_per_atom": -3.620, '
        '"energy_above_hull": 0.0}',
        '{"material_id": "mp-990002", "thermo_type": "GGA_GGA+U", "formation_energy_per_atom": -3.500, '
        '"energy_above_hull": 0.010}',
        '{"material_id": "mp-990003", "thermo_type": "GGA_GGA+U", "formation_energy_per_atom": -3.430, '
        '"energy_above_hull": 0.0}',
    ]
    if release == 2:
        summaries.append(
            _mp_summary(
                "mp-990004",
                "Ti2O3",
                167,
                "R-3c",
                "Trigonal",
                10,
                "104.00",
                "0.100",
                "4.570",
                ("5.160", "5.160", "13.610", "120.0"),
            )
        )
        thermo.append(
            '{"material_id": "mp-990004", "thermo_type": "GGA_GGA+U", "formation_energy_per_atom": -3.300, '
            '"energy_above_hull": 0.0}'
        )
    elastic = (
        '{"material_id": "mp-990001", "bulk_modulus": {"voigt": 212.0, "reuss": 208.0, "vrh": 210.0}, '
        '"shear_modulus": {"voigt": 115.0, "reuss": 109.0, "vrh": 112.0}}'
    )
    meta = f'"meta": {{"api_version": "0.99.0", "db_version": "{db_version}", "total_doc": 3}}'
    date = DATE if release == 1 else "Tue, 01 Jun 2099 12:00:00 GMT"
    return [
        page(
            MP_DOCUMENTS[0],
            '{"data": [' + ", ".join(summaries) + "], " + meta + "}",
            date=date,
        ),
        page(
            MP_DOCUMENTS[1],
            '{"data": [' + ", ".join(thermo) + "], " + meta + "}",
            date=date,
        ),
        page(MP_DOCUMENTS[2], '{"data": [' + elastic + "], " + meta + "}", date=date),
    ]


def jarvis_pages():
    body = """[
 {"jid": "JVASP-990101", "formula": "TiO2", "spg_number": 136, "spg_symbol": "P4_2/mnm", "formation_energy_peratom": -3.380,
  "optb88vdw_bandgap": 1.770, "mbj_bandgap": 2.950, "bulk_modulus_kv": 225.0, "shear_modulus_gv": "na", "density": 4.200,
  "icsd": "99001", "atoms": {"lattice_mat": [[4.640, 0.0, 0.0], [0.0, 4.640, 0.0], [0.0, 0.0, 2.970]],
  "elements": ["Ti", "Ti", "O", "O", "O", "O"]}},
 {"jid": "JVASP-990102", "formula": "TiO2", "spg_number": 141, "spg_symbol": "I4_1/amd", "formation_energy_peratom": -3.370,
  "optb88vdw_bandgap": 2.050, "mbj_bandgap": 3.300, "bulk_modulus_kv": "na", "shear_modulus_gv": "na", "density": 3.850,
  "icsd": "na", "atoms": {"lattice_mat": [[3.790, 0.0, 0.0], [0.0, 3.790, 0.0], [0.0, 0.0, 9.640]],
  "elements": ["Ti", "Ti", "Ti", "Ti", "O", "O", "O", "O", "O", "O", "O", "O"]}},
 {"jid": "JVASP-990199", "formula": "SiO2", "spg_number": 154, "spg_symbol": "P3_221", "formation_energy_peratom": -3.000,
  "optb88vdw_bandgap": 5.600, "mbj_bandgap": 8.100, "bulk_modulus_kv": "na", "shear_modulus_gv": "na", "density": 2.600,
  "icsd": "na", "atoms": {"lattice_mat": [[4.9, 0.0, 0.0], [-2.45, 4.2435, 0.0], [0.0, 0.0, 5.4]],
  "elements": ["Si", "Si", "Si", "O", "O", "O", "O", "O", "O"]}}
]"""
    return [page(JARVIS_DOCUMENTS[0], body)]


def oqmd_pages():
    body = """{"links": {"next": null}, "resource": {}, "data": [
 {"name": "TiO2", "entry_id": 99000301, "icsd_id": 99001, "spacegroup": "P4_2/mnm", "natoms": 6, "volume": 63.90,
  "delta_e": -3.300, "stability": 0.0, "band_gap": 1.840, "fit": "standard"},
 {"name": "TiO2", "entry_id": 99000302, "icsd_id": null, "spacegroup": "I4_1/amd", "natoms": 12, "volume": 138.40,
  "delta_e": -3.290, "stability": 0.012, "band_gap": 2.100, "fit": "standard"}],
 "meta": {"query": {"composition": "TiO2"}, "api_version": "1.0", "db_version": "v9.9", "data_returned": 2,
  "data_available": 2, "more_data_available": false}, "response_message": "OK"}"""
    return [page(OQMD_DOCUMENTS[0], body)]


WEBBOOK_HTML = """<!DOCTYPE html>
<html><head><title>Titanium oxide (fictional fixture)</title></head><body>
<h1 id="Top">Titanium oxide (fictional fixture)</h1>
<ul>
<li><strong>Formula:</strong> O<sub>2</sub>Ti</li>
<li><strong>IUPAC Standard InChI:</strong> <span class="inchi-text">InChI=1S/2O.Ti</span></li>
<li><strong>CAS Registry Number:</strong> 99999-99-9</li>
</ul>
<h2 id="Thermo-Condensed">Condensed phase thermochemistry data</h2>
<table class="data" aria-label="One dimensional data">
<tr><th>Quantity</th><th>Value</th><th>Units</th><th>Method</th><th>Reference</th><th>Comment</th></tr>
<tr><td>&Delta;<sub>f</sub>H&deg;<sub>solid</sub></td><td class="right-nowrap">-944.0 &plusmn; 0.8</td><td>kJ/mol</td><td>Review</td><td><a href="#ref-1">Fictional, 2098</a></td><td>Fixture review; rutile</td></tr>
<tr><td>&Delta;<sub>f</sub>H&deg;<sub>solid</sub></td><td class="right-nowrap">-939.7 &plusmn; 1.2</td><td>kJ/mol</td><td>Cm</td><td><a href="#ref-2">Invented and Madeup, 2097</a></td><td>Fixture measurement</td></tr>
<tr><td>S&deg;<sub>solid</sub></td><td class="right-nowrap">50.29 &plusmn; 0.21</td><td>J/mol*K</td><td>Review</td><td><a href="#ref-1">Fictional, 2098</a></td><td>Fixture review</td></tr>
<tr><td>&Delta;<sub>c</sub>H&deg;<sub>solid</sub></td><td class="right-nowrap">-12.3</td><td>kJ/mol</td><td>Ccb</td><td><a href="#ref-2">Invented and Madeup, 2097</a></td><td>Outside the property set</td></tr>
</table>
<table class="data" aria-label="Constant pressure heat capacity of solid">
<tr><th>C<sub>p,solid</sub> (J/mol*K)</th><th>Temperature (K)</th><th>Reference</th><th>Comment</th></tr>
<tr><td class="right-nowrap">55.1</td><td>298.15</td><td><a href="#ref-3">Placeholder, 2096</a></td><td>Adiabatic calorimetry per ASTM E1269 (fixture)</td></tr>
<tr><td class="right-nowrap">63.4</td><td>500.</td><td><a href="#ref-3">Placeholder, 2096</a></td><td>Adiabatic calorimetry per ASTM E1269 (fixture)</td></tr>
</table>
<h2 id="Thermo-Phase">Phase change data</h2>
<table class="data" aria-label="One dimensional data">
<tr><th>Quantity</th><th>Value</th><th>Units</th><th>Method</th><th>Reference</th><th>Comment</th></tr>
<tr><td>T<sub>fus</sub></td><td class="right-nowrap">2116 &plusmn; 20</td><td>K</td><td>N/A</td><td><a href="#ref-4">Nobody, 2095</a></td><td>Fixture value without a DOI</td></tr>
</table>
<h2 id="Refs">References</h2>
<p class="ref"><a id="ref-1"></a><strong>Fictional, 2098</strong><br />Fictional, A., Evaluated fixture tables, J. Fict. Chem. Ref. Data 99, 1 (2098). <a href="https://doi.org/10.99999/fict.webbook.001">doi</a></p>
<p class="ref"><a id="ref-2"></a><strong>Invented and Madeup, 2097</strong><br />Invented, B.; Madeup, C., Combustion fixture study, J. Fict. Thermodyn. 98, 2 (2097). <a href="https://doi.org/10.99999/fict.webbook.002">doi</a></p>
<p class="ref"><a id="ref-3"></a><strong>Placeholder, 2096</strong><br />Placeholder, D., Heat capacity of a fixture oxide, Fict. Calorim. 97, 3 (2096).</p>
<p class="ref"><a id="ref-4"></a><strong>Nobody, 2095</strong><br />Nobody, E., Melting of fixtures, Unindexed Proceedings (2095).</p>
</body></html>
"""


def webbook_pages():
    return [page(WEBBOOK_DOCUMENTS[0], WEBBOOK_HTML)]


def cod_pages(revision=990100, a="4.5937(3)"):
    body = f"""[
 {{"file": 9990001, "a": "{a}", "b": "{a}", "c": "2.9587(2)", "alpha": "90", "beta": "90", "gamma": "90",
  "vol": "62.43(1)", "celltemp": "295(2)", "diffrtemp": null, "cellpressure": "101.325", "sg": "P 42/m n m",
  "sgHall": "-P 4n 2n", "sgNumber": 136, "formula": "- O2 Ti -", "Z": "2",
  "authors": "Fixture, F.; Invent, I.", "title": "Rutile fixture structure", "journal": "J. Fict. Crystallogr.",
  "year": "2098", "volume": "12", "firstpage": "34", "doi": "10.99999/fict.cod.001", "svnrevision": "{revision}",
  "date": "2098-05-01", "status": null}},
 {{"file": 9990002, "a": "3.7845(2)", "b": "3.7845(2)", "c": "9.5143(5)", "alpha": "90", "beta": "90", "gamma": "90",
  "vol": "136.27(2)", "celltemp": "293", "diffrtemp": null, "cellpressure": null, "sg": "I 41/a m d :2",
  "sgHall": "-I 4bd 2", "sgNumber": 141, "formula": "- O2 Ti -", "Z": "4",
  "authors": "Fixture, F.", "title": "Anatase fixture structure", "journal": "J. Fict. Crystallogr.",
  "year": "2097", "volume": "11", "firstpage": "7", "doi": null, "svnrevision": "990050",
  "date": "2097-03-01", "status": null}}
]"""
    return [page(COD_DOCUMENTS[0], body)]


FIXTURES = {
    "materials-mp.json": (
        "Materials Project",
        mp_pages,
        [
            "summary+thermo+elasticity merged per material_id",
            "two thermo types kept separate",
            "polymorph pair",
        ],
    ),
    "materials-jarvis.json": (
        "JARVIS-DFT",
        jarvis_pages,
        [
            "OptB88vdW and TBmBJ band gaps kept separate",
            "'na' is missing",
            "outside the chemical systems",
        ],
    ),
    "materials-oqmd.json": (
        "OQMD",
        oqmd_pages,
        ["PBE with fit", "icsd cross-reference", "polymorph pair"],
    ),
    "materials-webbook.json": (
        "NIST Chemistry WebBook",
        webbook_pages,
        [
            "evaluated and measured rows",
            "heat capacity at two temperatures",
            "standard designation in a comment",
            "reference without DOI",
            "quantity outside the property set",
        ],
    ),
    "materials-cod.json": (
        "Crystallography Open Database",
        cod_pages,
        [
            "su in parentheses",
            "measurement temperature and pressure",
            "publication DOI",
            "entry revisions",
        ],
    ),
}


def fixture(name):
    provider, build, scenarios = FIXTURES[name]
    return {
        "authored": True,
        "provider": provider,
        "note": "Authored in the provider's documented response shape for fictional IDs and values; not a "
        "capture.",
        "scenarios": scenarios,
        "native_pages": build(),
    }


def write():
    from src.ingestion.materials_sources import replay_native_fixture
    from src.ingestion.source_packs import _digest

    config = json.loads((ROOT / "config/source_packs/materials.json").read_text())
    for name in FIXTURES:
        path = ROOT / "tests/fixtures/source_packs" / name
        path.write_text(json.dumps(fixture(name), indent=1, ensure_ascii=False) + "\n")
        raw = path.read_bytes()
        for source in config["sources"]:
            if source["fixture"]["path"].endswith(name):
                from src.ingestion.source_packs import validate_source_pack

                pack = validate_source_pack(config)
                resolved = next(
                    s for s in pack["sources"] if s["source_id"] == source["source_id"]
                )
                output = _digest(replay_native_fixture(resolved, json.loads(raw)))
                source["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()
                source["fixture"]["expected_output_hash"] = output
                print(name, source["fixture"]["sha256"], output)
    (ROOT / "config/source_packs/materials.json").write_text(
        json.dumps(config, indent=1, ensure_ascii=False) + "\n"
    )


if __name__ == "__main__":
    write()
