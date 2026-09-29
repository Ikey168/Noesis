"""Bounded acquisition of materials property and structure sources (MT04-MT07, #2082-#2085).

One native connector, ``materials``, reads a source's declared documents
(at most :data:`MAX_DOCUMENTS` requests on the endpoint host) and parses each
provider's documented response format into ``noesis-material-record-v1``
entries that :class:`src.kb.materials_store.MaterialsProjector` stores:

* **Materials Project** (``mp-api-json``): summary, thermo and elasticity
  documents merged per ``material_id``. Energies come only from thermo
  documents, whose ``thermo_type`` names the functional and mixing scheme
  (GGA/GGA+U, r2SCAN, or the mixed scheme); values are ``computed``.
* **JARVIS-DFT** (``jarvis-dft-json``): entries of one pinned snapshot, keyed
  by ``jid``; OptB88vdW and TBmBJ band gaps stay separate values with their
  functional, never a generic "DFT"; ``"na"`` is a missing value.
* **OQMD** (``oqmd-rest-json``): ``/oqmdapi/formationenergy`` pages keyed by
  ``entry_id``; PBE with the published fit.
* **NIST Chemistry WebBook** (``webbook-html``): the condensed-phase
  thermochemistry, heat-capacity and phase-change tables of one species page.
  "Review" rows are ``evaluated``, every other literature row ``measured``;
  ``±`` uncertainties, temperatures, phases and each row's cited reference
  are kept. Standard-state (``°``) quantities carry the WebBook's standard
  state as a stated condition with that basis.
* **Crystallography Open Database** (``cod-json``): experimental structures
  with space group, lattice (su in parentheses kept as uncertainty),
  measurement temperature/pressure and the publication; the entry revision
  is the release ordinal.

Selections are bounded by declared documents, chemical systems and a record
ceiling; no structure file is fetched. JSON numbers are parsed as exact
decimals. Every unverified detail of the provider formats is marked
*(verify)* in ``docs/development/materials-evidence/source-audit.md``.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb import materials_records as mr

CONNECTOR = "materials"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
FIXTURE_SECRET = "fixture-credential-not-a-real-key"
MAX_DOCUMENTS = 5
MAX_RECORDS = 50
FORMATS = {
    "materials-project": "mp-api-json",
    "jarvis-dft": "jarvis-dft-json",
    "oqmd": "oqmd-rest-json",
    "nist-webbook": "webbook-html",
    "cod": "cod-json",
}
DATASET_REFERENCES = {
    "materials-project": [
        (
            "A. Jain et al., The Materials Project, APL Materials 1, 011002 (2013)",
            "10.1063/1.4812323",
        )
    ],
    "jarvis-dft": [
        (
            "K. Choudhary et al., The joint automated repository for various integrated simulations (JARVIS) "
            "for data-driven materials design, npj Computational Materials 6, 173 (2020)",
            "10.1038/s41524-020-00440-1",
        )
    ],
    "oqmd": [
        (
            "J. E. Saal et al., Materials Design and Discovery with High-Throughput Density Functional Theory: "
            "The Open Quantum Materials Database (OQMD), JOM 65, 1501 (2013)",
            "10.1007/s11837-013-0755-4",
        ),
        (
            "S. Kirklin et al., The Open Quantum Materials Database (OQMD): assessing the accuracy of DFT "
            "formation energies, npj Computational Materials 1, 15010 (2015)",
            "10.1038/npjcompumats.2015.10",
        ),
    ],
    "nist-webbook": [
        (
            "P. J. Linstrom and W. G. Mallard (eds.), NIST Chemistry WebBook, NIST Standard Reference "
            "Database Number 69",
            "10.18434/T4D303",
        )
    ],
    "cod": [
        (
            "S. Gražulis et al., Crystallography Open Database (COD): an open-access collection of crystal "
            "structures and platform for world-wide collaboration, Nucleic Acids Research 40, D420 (2012)",
            "10.1093/nar/gkr900",
        )
    ],
}
PROVIDER_CONTRACTS = {
    "materials-project": {
        "documentation": "https://docs.materialsproject.org/downloading-data/using-the-api",
        "access": "REST API (summary, thermo, elasticity documents) with a personal API key",
        "authentication": "X-API-KEY header from NOESIS_MATERIALS_PROJECT_API_KEY",
        "licence": "CC BY 4.0 with attribution (verify)",
        "method_classes": ["computed"],
        "identifiers": "material_id (mp-<n>)",
        "releases": "named database versions (verify format)",
        "decision": "implement",
        "status": "unverified-live",
    },
    "jarvis-dft": {
        "documentation": "https://jarvis.nist.gov/",
        "access": "one pinned dft_3d snapshot JSON file",
        "authentication": "none",
        "licence": "NIST data; figshare CC BY 4.0 (verify)",
        "method_classes": ["computed"],
        "identifiers": "jid (JVASP-<n>)",
        "releases": "dated snapshot name declared in the source pack",
        "decision": "implement",
        "status": "unverified-live",
    },
    "oqmd": {
        "documentation": "https://oqmd.org/static/docs/restful.html",
        "access": "RESTful formationenergy endpoint",
        "authentication": "none",
        "licence": "CC BY 4.0 (verify)",
        "method_classes": ["computed"],
        "identifiers": "entry_id (icsd_id when ICSD-derived)",
        "releases": "database versions v1.x (verify)",
        "decision": "implement",
        "status": "unverified-live",
    },
    "nist-webbook": {
        "documentation": "https://webbook.nist.gov/chemistry/",
        "access": "one HTML species page per declared ID",
        "authentication": "none",
        "licence": "NIST SRD 69 terms; redistribution restricted (verify)",
        "method_classes": ["measured", "evaluated"],
        "identifiers": "WebBook species ID, InChI, CAS",
        "releases": "declared in the source pack",
        "decision": "implement (bounded)",
        "status": "unverified-live",
    },
    "cod": {
        "documentation": "https://www.crystallography.net/cod/",
        "access": "search result JSON (no CIF files)",
        "authentication": "none",
        "licence": "public domain / CC0 (verify)",
        "method_classes": ["measured"],
        "identifiers": "7-digit COD ID and entry revision",
        "releases": "entry revision ordinal",
        "decision": "implement",
        "status": "unverified-live",
    },
    "nist-srd": {
        "decision": "link-only",
        "status": "not implemented",
        "reason": "separately licensed databases; only SRD 69 (the WebBook) is acquired",
    },
    "aflow": {
        "decision": "not implemented",
        "status": "not implemented",
        "reason": "no v1 coverage beyond the three computed sources; AFLUX and licence need their own review",
    },
    "nomad": {
        "decision": "not implemented",
        "status": "not implemented",
        "reason": "raw uploaded calculations would need property extraction, which the pack excludes",
    },
    "excluded": {
        "sources": ["MatWeb", "Ansys Granta", "Springer Materials", "ASM handbooks"],
        "reason": "closed commercial databases without redistribution rights",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified",
        "evidence": None,
        "note": "no dated live run yet (MT14); offline fixtures are authored and fictional",
    }
    for provider in FORMATS
}
_STANDARD = re.compile(
    r"\b(ASTM [A-Z]\d{1,5}(?:-\d{2,4})?|ISO \d{3,5}(?:-\d{1,2})?(?::\d{4})?|DIN (?:EN )?\d{3,5}"
    r"(?:-\d{1,2})?)\b"
)
_ESD = re.compile(r"^(-?\d+(?:\.\d+)?)\((\d+)\)$")


def _missing(value: Any) -> bool:
    return value is None or (
        isinstance(value, str)
        and value.strip().casefold() in {"", "na", "nan", "none", "null", "?", "."}
    )


def num(value: Any) -> str | None:
    """A source number as exact decimal text; missing markers stay absent."""

    if _missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, float):  # never produced: JSON is parsed with Decimal
        raise SourcePackError("schema_drift", "float leaked into a materials record")
    return str(value).strip()


def esd(value: Any) -> tuple[str | None, str | None]:
    """COD numbers with a standard uncertainty in parentheses: '4.5937(3)' -> ('4.5937', '0.0003')."""

    text = num(value)
    if text is None:
        return None, None
    match = _ESD.match(text)
    if not match:
        return text, None
    number, digits = match.groups()
    places = len(number.split(".")[1]) if "." in number else 0
    return number, format(Decimal(digits).scaleb(-places), "f")


def elements_key(formula: str) -> str:
    return "-".join(sorted(mr.parse_formula(formula)))


def _dataset_references(provider):
    return [
        mr.reference("dataset", text, doi=doi)
        for text, doi in DATASET_REFERENCES[provider]
    ]


def _retrieved_at(headers: Mapping[str, Any]) -> str:
    stamp = {str(k).casefold(): v for k, v in dict(headers or {}).items()}.get("date")
    parsed = email.utils.parsedate_to_datetime(stamp) if stamp else datetime.now(UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _crystal(provider):
    return {
        "value": "solid",
        "basis": f"{provider} record is a periodic crystal structure",
    }


# --------------------------------------------------------------- Materials Project

MP_THERMO = {
    "GGA_GGA+U": ("GGA/GGA+U", None),
    "R2SCAN": ("r2SCAN", None),
    "GGA_GGA+U_R2SCAN": ("GGA/GGA+U/r2SCAN (mixed)", "GGA_GGA+U_R2SCAN"),
}


def _mp(documents, release, retrieved_at, ceiling):
    by_id: dict[str, dict[str, Any]] = {}
    for kind, payload in documents:
        for item in payload.get("data") or []:
            mid = str(item.get("material_id") or "")
            if not mid:
                continue
            by_id.setdefault(mid, {"summary": None, "thermo": [], "elasticity": None})
            if kind == "summary":
                by_id[mid]["summary"] = item
            elif kind == "thermo":
                by_id[mid]["thermo"].append(item)
            elif kind == "elasticity":
                by_id[mid]["elasticity"] = item
    entries, skipped = [], []
    for mid, parts in sorted(by_id.items()):
        summary = parts["summary"]
        if summary is None:
            skipped.append(
                {
                    "native_id": mid,
                    "reason": "no summary document (formula and structure unknown)",
                }
            )
            continue
        if len(entries) >= ceiling:
            skipped.append({"native_id": mid, "reason": "record ceiling reached"})
            continue
        relax = mr.method_provenance(
            "computed",
            method="DFT structure relaxation (PAW)",
            functional="GGA/GGA+U",
            code="VASP",
            note="functional of the default relaxation (verify)",
        )
        values = []
        crystal = _crystal("materials-project")
        if num(summary.get("band_gap")) is not None:
            values.append(
                mr.property_value(
                    "band_gap",
                    num(summary["band_gap"]),
                    "eV",
                    field="summary.band_gap",
                    conditions=mr.condition_set(phase=crystal),
                    method=relax,
                )
            )
        if num(summary.get("density")) is not None:
            values.append(
                mr.property_value(
                    "density",
                    num(summary["density"]),
                    "g/cm^3",
                    field="summary.density",
                    conditions=mr.condition_set(phase=crystal),
                    method=relax,
                )
            )
        for thermo in sorted(parts["thermo"], key=lambda t: str(t.get("thermo_type"))):
            thermo_type = str(thermo.get("thermo_type") or "")
            if thermo_type not in MP_THERMO:
                skipped.append(
                    {"native_id": mid, "reason": f"unknown thermo_type {thermo_type!r}"}
                )
                continue
            functional, mixing = MP_THERMO[thermo_type]
            method = mr.method_provenance(
                "computed",
                method="DFT total energy (PAW)",
                functional=functional,
                mixing_scheme=mixing,
                code="VASP",
                note=f"thermo_type {thermo_type}",
            )
            for prop, field in (
                ("formation_energy_per_atom", "formation_energy_per_atom"),
                ("energy_above_hull", "energy_above_hull"),
            ):
                if num(thermo.get(field)) is not None:
                    values.append(
                        mr.property_value(
                            prop,
                            num(thermo[field]),
                            "eV/atom",
                            field=f"thermo.{field}",
                            conditions=mr.condition_set(phase=crystal),
                            method=method,
                        )
                    )
        elasticity = parts["elasticity"] or {}
        elastic_method = mr.method_provenance(
            "computed",
            method="DFT stress-strain elastic tensor (PAW)",
            functional="GGA/GGA+U",
            code="VASP",
        )
        for prop in ("bulk_modulus", "shear_modulus"):
            vrh = num((elasticity.get(prop) or {}).get("vrh"))
            if vrh is not None:
                values.append(
                    mr.property_value(
                        prop,
                        vrh,
                        "GPa",
                        field=f"elasticity.{prop}.vrh",
                        method=elastic_method,
                        conditions=mr.condition_set(
                            phase=crystal,
                            orientation={
                                "value": "polycrystalline Voigt-Reuss-Hill average",
                                "basis": "as published",
                            },
                        ),
                    )
                )
        symmetry = summary.get("symmetry") or {}
        lattice = (summary.get("structure") or {}).get("lattice") or {}
        structure = mr.structure(
            mid,
            method_class="computed",
            space_group={
                "number": symmetry.get("number"),
                "symbol": symmetry.get("symbol"),
            },
            crystal_system=symmetry.get("crystal_system"),
            lattice={
                k: num(lattice.get(k))
                for k in ("a", "b", "c", "alpha", "beta", "gamma")
            }
            if lattice
            else None,
            cell_setting="as served by the API (verify)",
            nsites=summary.get("nsites"),
            volume=None
            if num(summary.get("volume")) is None
            else {"value": num(summary["volume"]), "unit": "Å^3"},
        )
        identifiers = {
            k: sorted(str(x) for x in v)
            for k, v in (summary.get("database_IDs") or {}).items()
            if v
        }
        entries.append(
            mr.entry(
                "materials-project",
                mid,
                title=f"{summary.get('formula_pretty') or mid} ({mid})",
                material_record=mr.material(str(summary["formula_pretty"])),
                values=values,
                release_record=release,
                retrieved_at=retrieved_at,
                locator=f"https://next-gen.materialsproject.org/materials/{mid}",
                references=_dataset_references("materials-project"),
                structure_record=structure,
                status="deprecated" if summary.get("deprecated") is True else "active",
                identifiers=identifiers or None,
                source_updated_at=_date_or_instant(summary.get("last_updated")),
            )
        )
    return entries, skipped


def _date_or_instant(value):
    if _missing(value):
        return None
    text = str(value).strip().replace(" ", "T")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------- JARVIS

JARVIS_VALUES = (
    (
        "formation_energy_peratom",
        "formation_energy_per_atom",
        "eV/atom",
        "OptB88vdW",
        None,
    ),
    ("optb88vdw_bandgap", "band_gap", "eV", "OptB88vdW", None),
    ("mbj_bandgap", "band_gap", "eV", "TBmBJ", None),
    ("bulk_modulus_kv", "bulk_modulus", "GPa", "OptB88vdW", "Voigt average (Kv)"),
    ("shear_modulus_gv", "shear_modulus", "GPa", "OptB88vdW", "Voigt average (Gv)"),
    ("density", "density", "g/cm^3", "OptB88vdW", None),
)


def _det3(matrix):
    (a, b, c), (d, e, f), (g, h, i) = [[Decimal(str(x)) for x in row] for row in matrix]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _jarvis(documents, source, release, retrieved_at, systems, ceiling):
    del source
    entries, skipped = [], []
    items = [
        item
        for _, payload in documents
        for item in (payload if isinstance(payload, list) else [])
    ]
    for item in sorted(items, key=lambda i: str(i.get("jid"))):
        jid, formula = str(item.get("jid") or ""), str(item.get("formula") or "")
        if not jid or not formula:
            skipped.append(
                {"native_id": jid or None, "reason": "entry without jid or formula"}
            )
            continue
        if systems and elements_key(formula) not in systems:
            continue
        if len(entries) >= ceiling:
            skipped.append({"native_id": jid, "reason": "record ceiling reached"})
            continue
        values = []
        for field, prop, unit, functional, orientation in JARVIS_VALUES:
            number = num(item.get(field))
            if number is None:
                continue
            method = mr.method_provenance(
                "computed",
                method="DFT (PAW)",
                functional=functional,
                code="VASP",
                note=f"JARVIS field {field}",
            )
            values.append(
                mr.property_value(
                    prop,
                    number,
                    unit,
                    field=field,
                    method=method,
                    conditions=mr.condition_set(
                        phase=_crystal("jarvis-dft"),
                        orientation=None
                        if orientation is None
                        else {"value": orientation, "basis": "field definition"},
                    ),
                )
            )
        atoms = item.get("atoms") or {}
        volume, nsites = None, None
        if atoms.get("lattice_mat") and atoms.get("elements"):
            nsites = len(atoms["elements"])
            volume = {
                "value": format(abs(_det3(atoms["lattice_mat"])), "f"),
                "unit": "Å^3",
            }
        structure = mr.structure(
            jid,
            method_class="computed",
            space_group={
                "number": item.get("spg_number"),
                "symbol": item.get("spg_symbol"),
            },
            cell_setting="as published in the snapshot",
            nsites=nsites,
            volume=volume,
        )
        entries.append(
            mr.entry(
                "jarvis-dft",
                jid,
                title=f"{formula} ({jid})",
                material_record=mr.material(formula),
                values=values,
                release_record=release,
                retrieved_at=retrieved_at,
                locator=f"https://www.ctcms.nist.gov/~knc6/jsmol/{jid}.html",
                references=_dataset_references("jarvis-dft"),
                structure_record=structure,
                identifiers={"icsd": [str(item["icsd"])]}
                if num(item.get("icsd"))
                else None,
            )
        )
    return entries, skipped


# ----------------------------------------------------------------------- OQMD


def _oqmd(documents, source, release, retrieved_at, systems, ceiling):
    del source
    entries, skipped = [], []
    for _, payload in documents:
        if str(payload.get("response_message") or "OK").upper() != "OK":
            raise SourcePackError("schema_drift", "OQMD response_message is not OK")
        for item in sorted(
            payload.get("data") or [], key=lambda i: str(i.get("entry_id"))
        ):
            entry_id, formula = num(item.get("entry_id")), str(item.get("name") or "")
            if entry_id is None or not formula:
                skipped.append(
                    {"native_id": entry_id, "reason": "entry without entry_id or name"}
                )
                continue
            if systems and elements_key(formula) not in systems:
                continue
            if len(entries) >= ceiling:
                skipped.append(
                    {"native_id": entry_id, "reason": "record ceiling reached"}
                )
                continue
            fit = num(item.get("fit"))
            method = mr.method_provenance(
                "computed",
                method="DFT (PAW)",
                functional="PBE",
                code="VASP",
                mixing_scheme=None if fit is None else f"fit:{fit}",
                note="OQMD applies DFT+U to selected oxides (verify per entry)",
            )
            values = []
            for field, prop in (
                ("delta_e", "formation_energy_per_atom"),
                ("stability", "energy_above_hull"),
                ("band_gap", "band_gap"),
            ):
                number = num(item.get(field))
                if number is not None:
                    values.append(
                        mr.property_value(
                            prop,
                            number,
                            "eV" if prop == "band_gap" else "eV/atom",
                            field=field,
                            method=method,
                            conditions=mr.condition_set(phase=_crystal("oqmd")),
                        )
                    )
            natoms = item.get("natoms")
            volume = num(item.get("volume"))
            structure = mr.structure(
                entry_id,
                method_class="computed",
                space_group={"symbol": item.get("spacegroup")},
                cell_setting="as served by the API",
                nsites=natoms,
                volume=None if volume is None else {"value": volume, "unit": "Å^3"},
            )
            icsd = num(item.get("icsd_id"))
            entries.append(
                mr.entry(
                    "oqmd",
                    entry_id,
                    title=f"{formula} (OQMD entry {entry_id})",
                    material_record=mr.material(formula),
                    values=values,
                    release_record=release,
                    retrieved_at=retrieved_at,
                    locator=f"https://oqmd.org/materials/entry/{entry_id}",
                    references=_dataset_references("oqmd"),
                    structure_record=structure,
                    identifiers={"icsd": [icsd]} if icsd else None,
                )
            )
    return entries, skipped


# ------------------------------------------------------------------ WebBook


class _WebBookParser(HTMLParser):
    """Collects the header facts, data tables (with their section) and reference paragraphs of a species page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.section = None
        self.tables: list[dict[str, Any]] = []
        self.rows = None
        self.cell = None
        self.cell_links: list[str] = []
        self.row_links: list[list[str]] = []
        self.title = None
        self._h1 = False
        self._h2 = False
        self._li = None
        self.facts: list[str] = []
        self.refs: dict[str, dict[str, Any]] = {}
        self._ref = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h1":
            self._h1, self.title = True, ""
        elif tag == "h2":
            self._h2, self.section = True, ""
        elif tag == "li":
            self._li = ""
        elif tag == "table" and "data" in (attrs.get("class") or ""):
            self.rows = []
            self.tables.append(
                {
                    "section": self.section,
                    "label": attrs.get("aria-label"),
                    "rows": self.rows,
                }
            )
        elif tag == "tr" and self.rows is not None:
            self.rows.append([])
            self.row_links = []
        elif tag in {"td", "th"} and self.rows is not None:
            self.cell, self.cell_links = "", []
        elif tag == "sub" and self.cell is not None:
            self.cell += "_"
        elif tag == "a":
            if self.cell is not None and attrs.get("href"):
                self.cell_links.append(attrs["href"])
            if self._ref is not None and attrs.get("href"):
                self._ref["links"].append(attrs["href"])
            if attrs.get("id", "").startswith("ref-"):
                self._ref = {"id": attrs["id"], "text": "", "links": []}
        elif tag == "br" and self._ref is not None:
            self._ref["text"] += " "

    def handle_endtag(self, tag):
        if tag == "h1":
            self._h1 = False
        elif tag == "h2":
            self._h2 = False
        elif tag == "li" and self._li is not None:
            self.facts.append(" ".join(self._li.split()))
            self._li = None
        elif tag in {"td", "th"} and self.cell is not None and self.rows:
            self.rows[-1].append(
                {"text": " ".join(self.cell.split()), "links": self.cell_links}
            )
            self.cell = None
        elif tag == "table":
            self.rows = None
        elif tag == "p" and self._ref is not None:
            ref = self._ref
            self.refs["#" + ref["id"]] = {
                "text": " ".join(ref["text"].split()),
                "links": ref["links"],
            }
            self._ref = None

    def handle_data(self, data):
        if self._h1:
            self.title += data
        if self._h2:
            self.section += data
        if self._li is not None:
            self._li += data
        if self.cell is not None:
            self.cell += data
        if self._ref is not None:
            self._ref["text"] += data


WEBBOOK_QUANTITIES = {
    "Δ_fH°_solid": ("standard_enthalpy_of_formation", "solid"),
    "S°_solid": ("standard_entropy", "solid"),
    "T_fus": ("fusion_temperature", None),
}
WEBBOOK_STANDARD_STATE = {
    "temperature": {"value": "298.15", "unit": "K"},
    "pressure": {"value": "1", "unit": "bar"},
}


def _plusminus(text):
    parts = [p.strip() for p in text.replace("±", "&plusmn;").split("&plusmn;")]
    value = num(parts[0].replace(" ", ""))
    uncertainty = num(parts[1].replace(" ", "")) if len(parts) > 1 else None
    return value, uncertainty


def _webbook_reference(cell, refs, comment):
    links = [link for link in cell["links"] if link.startswith("#ref-")]
    result = []
    for link in links:
        ref = refs.get(link) or {"text": cell["text"], "links": []}
        doi = next(
            (
                re.sub(r"^https?://(?:dx\.)?doi\.org/", "", item)
                for item in ref["links"]
                if re.match(r"^https?://(?:dx\.)?doi\.org/10\.", item)
            ),
            None,
        )
        result.append(
            mr.reference("value", ref["text"] or cell["text"], doi=doi, locator=link)
        )
    if not links and cell["text"]:
        result.append(mr.reference("value", cell["text"]))
    standard = _STANDARD.search(comment or "")
    if standard:
        result.append(mr.reference("value", comment, standard=standard.group(1)))
    return result


def _webbook_method(code):
    code = (code or "").strip()
    if code.casefold() == "review":
        return mr.method_provenance(
            "evaluated", evaluation="WebBook method code 'Review' (evaluated data)"
        )
    return mr.method_provenance(
        "measured",
        technique=None if code in {"", "N/A"} else f"WebBook method code {code!r}",
    )


def _webbook(documents, source, release, retrieved_at):
    entries, skipped = [], []
    for document, text in documents:
        parser = _WebBookParser()
        parser.feed(text)
        facts = {}
        for fact in parser.facts:
            if ":" in fact:
                key, value = fact.split(":", 1)
                facts[key.strip().casefold()] = value.strip()
        formula = (facts.get("formula") or "").replace(" ", "")
        species = str(dict(document.get("params") or {}).get("ID") or "")
        if not formula or not species:
            raise SourcePackError(
                "schema_drift", "WebBook page without formula or species ID"
            )
        values = []
        for table in parser.tables:
            rows = table["rows"]
            if not rows:
                continue
            header = [c["text"] for c in rows[0]]
            if header[:3] == ["Quantity", "Value", "Units"]:
                for row in rows[1:]:
                    cells = {h: c for h, c in zip(header, row)}
                    quantity = cells["Quantity"]["text"].replace(" ", "")
                    if quantity not in WEBBOOK_QUANTITIES:
                        skipped.append(
                            {
                                "native_id": species,
                                "reason": f"quantity {quantity!r} outside the property set",
                            }
                        )
                        continue
                    prop, phase = WEBBOOK_QUANTITIES[quantity]
                    value, uncertainty = _plusminus(cells["Value"]["text"])
                    if value is None:
                        continue
                    comment = cells.get("Comment", {"text": ""})["text"]
                    standard = "°" in quantity
                    conditions = mr.condition_set(
                        **(WEBBOOK_STANDARD_STATE if standard else {}),
                        phase=phase,
                        other={
                            "condition_basis": "WebBook standard state (°): 298.15 K, 1 bar (verify)"
                        }
                        if standard
                        else None,
                    )
                    values.append(
                        mr.property_value(
                            prop,
                            value,
                            cells["Units"]["text"],
                            conditions=conditions,
                            method=_webbook_method(
                                cells.get("Method", {"text": ""})["text"]
                            ),
                            uncertainty=None
                            if uncertainty is None
                            else {"value": uncertainty, "kind": "± as published"},
                            references=_webbook_reference(
                                cells.get("Reference", {"text": "", "links": []}),
                                parser.refs,
                                comment,
                            ),
                            field=f"{table['section']}: {quantity}",
                        )
                    )
            elif header and header[0].startswith("C_p,") and len(header) >= 3:
                unit = re.search(r"\(([^)]+)\)", header[0])
                phase = (
                    header[0].split("(")[0].replace("C_p,", "").replace("_", "").strip()
                    or None
                )
                for row in rows[1:]:
                    cells = {h: c for h, c in zip(header, row)}
                    value, uncertainty = _plusminus(row[0]["text"])
                    temperature = num(row[1]["text"])
                    if value is None:
                        continue
                    comment = cells.get("Comment", {"text": ""})["text"]
                    values.append(
                        mr.property_value(
                            "heat_capacity_cp",
                            value,
                            unit.group(1) if unit else None,
                            conditions=mr.condition_set(
                                temperature=None
                                if temperature is None
                                else {"value": temperature, "unit": "K"},
                                phase=phase,
                            ),
                            method=_webbook_method(
                                cells.get("Method", {"text": ""})["text"]
                            ),
                            uncertainty=None
                            if uncertainty is None
                            else {"value": uncertainty, "kind": "± as published"},
                            references=_webbook_reference(
                                cells.get("Reference", {"text": "", "links": []}),
                                parser.refs,
                                comment,
                            ),
                            field=f"{table['section']}: {header[0]}",
                        )
                    )
        inchi = facts.get("iupac standard inchi")
        entries.append(
            mr.entry(
                "nist-webbook",
                species,
                title=f"{(parser.title or species).strip()} (WebBook {species})",
                material_record=mr.material(
                    formula,
                    name=(parser.title or "").strip() or None,
                    cas=facts.get("cas registry number"),
                    inchi=inchi,
                ),
                values=values,
                release_record=release,
                retrieved_at=retrieved_at,
                locator=f"https://webbook.nist.gov/cgi/cbook.cgi?ID={species}",
                references=_dataset_references("nist-webbook"),
            )
        )
    del source
    return entries, skipped


# ------------------------------------------------------------------------ COD


def _cod(documents, source, retrieved_at, ceiling):
    del source
    entries, skipped = [], []
    items = [
        item
        for _, payload in documents
        for item in (payload if isinstance(payload, list) else [])
    ]
    for item in sorted(items, key=lambda i: str(i.get("file"))):
        cod_id = num(item.get("file"))
        formula = str(item.get("formula") or "").strip("- ").strip()
        if cod_id is None or not formula:
            skipped.append(
                {"native_id": cod_id, "reason": "entry without COD id or formula"}
            )
            continue
        if len(entries) >= ceiling:
            skipped.append({"native_id": cod_id, "reason": "record ceiling reached"})
            continue
        revision = num(item.get("svnrevision"))
        release = mr.release(
            f"rev-{revision}" if revision else "rev-unstated",
            basis="entry-revision",
            sequence=int(revision) if revision and revision.isdigit() else None,
        )
        lattice = {
            k: esd(item.get(k))[0] for k in ("a", "b", "c", "alpha", "beta", "gamma")
        }
        measurement = {}
        if num(item.get("celltemp")) is not None:
            measurement["temperature"] = {
                "value": esd(item["celltemp"])[0],
                "unit": "K",
            }
        elif num(item.get("diffrtemp")) is not None:
            measurement["temperature"] = {
                "value": esd(item["diffrtemp"])[0],
                "unit": "K",
            }
        if num(item.get("cellpressure")) is not None:
            measurement["pressure"] = {
                "value": esd(item["cellpressure"])[0],
                "unit": "kPa",
            }
        number = item.get("sgNumber")
        structure = mr.structure(
            cod_id,
            method_class="measured",
            space_group={
                "symbol": item.get("sg"),
                "number": int(number)
                if num(number) and str(number).isdigit()
                else None,
            },
            lattice={k: v for k, v in lattice.items() if v is not None} or None,
            cell_setting="conventional (as reported in the CIF)",
            volume=None
            if esd(item.get("vol"))[0] is None
            else {"value": esd(item["vol"])[0], "unit": "Å^3"},
            nsites=_cod_sites(item.get("Z"), formula),
            measurement=measurement,
        )
        citation = ", ".join(
            str(item[k])
            for k in ("authors", "title", "journal", "year", "volume", "firstpage")
            if not _missing(item.get(k))
        )
        references = [
            mr.reference(
                "value",
                citation or f"COD {cod_id} publication",
                doi=num(item.get("doi")),
            )
        ]
        references += _dataset_references("cod")
        status = str(item.get("status") or "").casefold()
        entries.append(
            mr.entry(
                "cod",
                cod_id,
                title=f"{formula} (COD {cod_id})",
                material_record=mr.material(formula),
                values=[],
                release_record=release,
                retrieved_at=retrieved_at,
                locator=f"https://www.crystallography.net/cod/{cod_id}.html",
                references=references,
                structure_record=structure,
                status="withdrawn"
                if status in {"retracted", "withdrawn"}
                else "active",
                source_updated_at=_date_or_instant(item.get("date")),
            )
        )
    return entries, skipped


def _cod_sites(z, formula):
    """Atoms in the cell from the stated Z (formula units per cell) and the formula; unknown otherwise."""

    text = num(z)
    if text is None or not text.isdigit():
        return None
    total = sum(mr.parse_formula(formula).values())
    return int(int(text) * total) if total.denominator == 1 else None


# -------------------------------------------------------------------- adapter


class MaterialsAdapter:
    accepts_transport = True

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        spec = dict(self.source.get("materials") or {})
        self.provider = str(spec.get("provider") or "")
        if self.provider not in FORMATS or spec.get("format") != FORMATS.get(
            self.provider
        ):
            raise SourcePackError(
                "unknown_connector",
                "materials sources declare a known provider and its format",
            )
        self.documents = [dict(d) for d in spec.get("documents") or []]
        self.ceiling = int(spec.get("max_records") or 0)
        if (
            not 1 <= len(self.documents) <= MAX_DOCUMENTS
            or not 1 <= self.ceiling <= MAX_RECORDS
        ):
            raise SourcePackError(
                "unbounded_source",
                f"materials sources declare 1-{MAX_DOCUMENTS} documents and a "
                f"record ceiling of 1-{MAX_RECORDS}",
            )
        for document in self.documents:
            if not str(document.get("path") or "").startswith("/"):
                raise SourcePackError(
                    "unsafe_endpoint",
                    "document paths are absolute paths on the endpoint host",
                )
        self.systems = {
            "-".join(sorted(s.split("-"))) for s in spec.get("chemical_systems") or []
        }
        self.release_spec = dict(spec.get("release") or {})
        self.secret = secret
        self.transport = transport or partial(
            HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
        )
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "materials": {
                "provider": self.provider,
                "documents": len(self.documents),
                "max_records": self.ceiling,
                "chemical_systems": sorted(self.systems),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _url(self, document):
        parts = urlsplit(self.source["endpoint"])
        return f"{parts.scheme}://{parts.netloc}{document['path']}"

    def _fetch(self, document):
        # Checked at request time, like the other connectors, so compiling an adapter never needs the secret.
        if self.source.get("auth", {}).get("kind") == "required-secret" and not self.secret:
            raise SourcePackError(
                "authentication_failed", "the source requires its configured API key"
            )
        headers = {
            "Accept": "text/html"
            if self.provider == "nist-webbook"
            else "application/json"
        }
        if self.provider == "materials-project":
            headers["X-API-KEY"] = str(self.secret)
        response = self.transport(
            url=self._url(document),
            params=dict(document.get("params") or {}),
            headers=headers,
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        status = int(response.get("status", 200))
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed",
                f"{self.provider} rejected the credential (HTTP {status})",
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited", f"{self.provider} rate-limited the request"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"{self.provider} returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError(
                "schema_drift", f"{self.provider} returned HTTP {status}"
            )
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "response exceeds the source byte budget"
            )
        return raw, dict(response.get("headers") or {})

    def _release(self, stated=None):
        if stated:
            return mr.release(
                str(stated),
                basis="provider-stated",
                released_on=self.release_spec.get("released_on"),
            )
        label = self.release_spec.get("label")
        if not label:
            raise SourcePackError(
                "mapping_failed",
                "no release stated by the source or declared in the source pack",
            )
        return mr.release(
            str(label),
            basis="operator-declared",
            released_on=self.release_spec.get("released_on"),
        )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if cursor is not None or dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "materials selections are declared in the source pack",
            )
        documents, digests, total, stamps = [], [], 0, []
        for document in self.documents:
            raw, headers = self._fetch(document)
            total += len(raw)
            digests.append(hashlib.sha256(raw).hexdigest())
            stamps.append(_retrieved_at(headers))
            try:
                payload = (
                    raw.decode("utf-8")
                    if self.provider == "nist-webbook"
                    else json.loads(raw.decode("utf-8"), parse_float=Decimal)
                )
            except ValueError as exc:
                raise SourcePackError(
                    "schema_drift", f"{self.provider} response is not parseable"
                ) from exc
            documents.append((document, payload))
        retrieved_at = max(stamps)
        try:
            if self.provider == "materials-project":
                meta = next(
                    (p.get("meta") or {} for _, p in documents if isinstance(p, dict)),
                    {},
                )
                release = self._release(meta.get("db_version"))
                entries, skipped = _mp(
                    [(d.get("kind"), p) for d, p in documents],
                    release,
                    retrieved_at,
                    self.ceiling,
                )
            elif self.provider == "jarvis-dft":
                entries, skipped = _jarvis(
                    documents,
                    self.source,
                    self._release(),
                    retrieved_at,
                    self.systems,
                    self.ceiling,
                )
            elif self.provider == "oqmd":
                meta = next(
                    (p.get("meta") or {} for _, p in documents if isinstance(p, dict)),
                    {},
                )
                entries, skipped = _oqmd(
                    documents,
                    self.source,
                    self._release(meta.get("db_version")),
                    retrieved_at,
                    self.systems,
                    self.ceiling,
                )
            elif self.provider == "nist-webbook":
                entries, skipped = _webbook(
                    documents, self.source, self._release(), retrieved_at
                )
            else:
                entries, skipped = _cod(
                    documents, self.source, retrieved_at, self.ceiling
                )
        except mr.MaterialRecordError as exc:
            raise SourcePackError("mapping_failed", f"{self.provider}: {exc}") from exc
        records = [
            {
                "id": f"{e['provider']}:{e['native_id']}",
                "title": e["title"],
                "url": e["locator"],
                "language": "en",
                "material_entry": e,
            }
            for e in entries
        ]
        return RuntimePage(
            tuple(records),
            None,
            total,
            receipt={
                "status": 200,
                "provider": self.provider,
                "documents": len(documents),
                "response_sha256": digests,
                "entries": len(records),
                "skipped": skipped,
                "retrieved_at": retrieved_at,
            },
        )


ADAPTERS = {CONNECTOR: MaterialsAdapter}


def fixture_request(document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""

    query = urlencode(
        sorted((str(k), str(v)) for k, v in dict(document.get("params") or {}).items())
    )
    return document["path"] + ("?" + query if query else "")


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by path and sorted query."""

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        query = urlencode(
            sorted((str(k), str(v)) for k, v in dict(params or {}).items())
        )
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("source_unavailable", f"no native page for {key}")
        body = page["body"]
        content = (
            body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content.encode(),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = MaterialsAdapter(
        source,
        transport=fixture_transport(list(fixture["native_pages"])),
        secret=FIXTURE_SECRET,
    )
    page = adapter.fetch_page(
        {"operation": min(source["operations"]), "parameters": {}}, cursor=None
    )
    return [dict(item) for item in page.records]


__all__ = [
    "ADAPTERS",
    "FIXTURE_SECRET",
    "LIVE_VERIFICATION",
    "PROVIDER_CONTRACTS",
    "fixture_request",
    "fixture_transport",
    "replay_native_fixture",
]
