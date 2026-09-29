# Materials pack guide

Tracking: [#2060](https://github.com/Ikey168/Noesis/issues/2060). The Materials pack takes a material
(formula, source record or source ID) to its cited property record: every value with its unit, the
conditions it holds under, the method class, uncertainty as published, the dataset release it came
from and the papers and test-method standards it cites. It never averages, merges or predicts a
value, and never presents a computed value as a measurement.

Source contracts, licences and bounded coverage: [source audit](../development/materials-evidence/source-audit.md).
Every source is **unverified live**; the offline evidence below uses authored, fictional fixtures.

## Sources and bounded coverage

| Source | Method class | Keyed by | Release |
| --- | --- | --- | --- |
| Materials Project | computed (functional and mixing scheme from `thermo_type`) | `mp-<n>` | database version stated by the API |
| JARVIS-DFT | computed (OptB88vdW, TBmBJ kept apart) | `JVASP-<n>` | pinned snapshot, declared in the source pack |
| OQMD | computed (PBE, published fit) | `entry_id` | database version stated by the API |
| NIST Chemistry WebBook | measured or evaluated, as the page presents each row | species ID, InChI | one declared release; changes are corrections |
| Crystallography Open Database | measured structures | COD ID | entry revision (source ordinal) |

The selection is the Ti-O and Al-O systems (at most 50 records per source); nothing else is mirrored
and no CIF file is fetched. Sources run through the source-pack runtime
(`config/source_packs/materials.json`, connector `materials`) and are projected by
`src.kb.materials_store.MaterialsProjector`.

## Records

`noesis-material-record-v1` (`src/kb/materials_records.py`): material identity, structure, property
value (native value and unit, published uncertainty), condition set (temperature, pressure, phase,
orientation, other; anything not stated is `unstated`), method provenance (`measured` with technique,
`computed` with method, functional, code and version, or `evaluated`) and dataset release. Units are
normalised exactly (`src/kb/materials_units.py`, no pint): one canonical unit per property, offset units,
per-formula-unit vs per-atom handled explicitly, unknown or ambiguous units marked `unit_unknown` and
excluded from normalised comparison.

The store keys a value by record, property, condition set, method and release. Re-acquisition that
matches the current version adds nothing; a changed value, including a reversion, is a new
`correction`; a late older release or capture is history, never a correction. "Current" means the
provider's newest release (release date, else source ordinal, else observation order).

## Identity

Composition groups (same reduced formula) are labels, not identities. Phase-level matches are
proposed from stated ICSD/COD cross-references or equal space group plus volume per atom within 10%,
reviewed through the shared identity state machine, and never proposed for different space groups,
so polymorphs never merge. This is the optional `phase-identity` feature (default off).

## Tools (noesis-knowledge-engine)

| Tool | Semantics |
| --- | --- |
| `lookup_material` | records by formula, record key or source ID with composition and phase groups |
| `material_properties` | cited dossier; value- and dataset-level citations resolved by DOI or standard designation only (`standards_namespace` needs `knowledge:standards:read`) |
| `compare_material_property` | aligns only the same property, method class, reviewed phase and conditions within 1 K / 2 %; everything else side by side with reasons; no average |
| `search_materials_by_property` | bounded to acquired records; current value per source first; each hit cites its values |
| `propose_material_matches`, `review_material_match`, `revert_material_match` | reviewable phase-level identity |
| `material_release_changes` | added, changed (both values), deprecated/withdrawn as stated, absent otherwise |
| `create_material_watch`, `run_material_watch`, `poll_material_watch` | subscription watches at committed watermarks |
| `materials_bundle_status`, `materials_source_contracts`, `register_material_schemas` | readiness, contracts, schema registration |

Reads answer `not_ready` until a materials source has run.

## Evidence

Offline: `tests/unit/materials/` and `tests/unit/domains/test_materials_acceptance.py` (run with and
without pint). Live checks belong in `docs/development/materials-evidence/` (MT14, #2092) and have not
been run.

## Exclusions

Closed commercial databases and handbooks (MatWeb, Granta, Springer Materials, ASM), material
selection or suitability claims, property prediction, and unrestricted mirroring.
