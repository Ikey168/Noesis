# Materials: source-contract audit, licence decisions and bounded coverage (MT01)

Tracking: #2060 · delivery issue #2079 · recorded 2026-09-28.

This audit decides, per candidate source, what the Materials pack may acquire,
how, and on what terms. It was written **without network access**. Endpoints,
parameters, identifiers, licences and release semantics come from the
providers' published documentation as the author knows it. **Every item marked
_(verify)_ must be checked against the live documentation, the live terms and a
real response before the first dated live run (MT14, #2092). No source is
`live` until that run exists.** The machine-readable copy of these decisions is
`PROVIDER_CONTRACTS` / `LIVE_VERIFICATION` in `src/ingestion/materials_sources.py`,
returned by the MCP tool `materials_source_contracts`.

Non-goals that apply to every source:

- computed and measured values are never merged, averaged or reconciled into a
  consensus or "best" value, and a computed value is never presented as a
  measurement;
- Noesis never predicts, estimates or fills in a property value; every value is
  a source's value with the source's method;
- no material selection, suitability-for-use or design-allowable claim;
- no unrestricted mirroring of a database, and no structure file beyond the
  bounded, licensed selection below.

## Access decisions

| Source | Delivers | Decision | Reason |
| --- | --- | --- | --- |
| Materials Project (api.materialsproject.org) | DFT-computed summary, thermodynamic, elastic and electronic properties; relaxed structures | `implement` | Documented REST API with a free personal API key; data licensed for reuse with attribution (_verify_ licence text, see below) |
| JARVIS-DFT (NIST, jarvis.nist.gov) | DFT-computed formation energy, band gaps (two functionals), elastic moduli; relaxed structures | `implement` | NIST publishes versioned JSON dataset snapshots (e.g. `dft_3d`) through `jarvis-tools` / figshare; one pinned snapshot file is read and filtered to the bounded selection (_verify_ snapshot URL and licence) |
| OQMD (oqmd.org) | DFT-computed formation energy, hull distance, band gap; relaxed structures | `implement` | Documented RESTful API (`/oqmdapi/formationenergy`) without authentication (_verify_ current host scheme and parameters) |
| NIST Chemistry WebBook (webbook.nist.gov, SRD 69) | experimental and evaluated thermochemical and phase-change data with literature references | `implement` (bounded) | No JSON API; one HTML species page per declared species ID, parsed for the documented condensed-phase thermochemistry and phase-change tables only. NIST SRD terms restrict redistribution (_verify_), so values are stored with citation and link back; pages are never bulk-crawled |
| Crystallography Open Database (crystallography.net/cod) | experimentally determined crystal structures (cell, space group, measurement temperature/pressure, publication) | `implement` | Documented search interface with JSON output, no authentication; data dedicated to the public domain (_verify_ CC0 statement) |
| NIST SRD (other databases, www.nist.gov/srd) | a catalogue of separately licensed databases | `link-only` | Many SRD products are licensed or sold (e.g. SRD 23 REFPROP, out of scope as fluid correlations). Each database needs its own terms review; none is acquired in v1. SRD 69 (the WebBook) is the only one implemented |
| AFLOW (aflow.org, AFLUX API) | DFT-computed properties | `not implemented` | A fourth computed source adds no v1 coverage the three implemented computed sources lack; the AFLUX query language and licence (_verify_) need their own adapter and review. Documented here so a later issue can pick it up |
| NOMAD (nomad-lab.eu, API v1) | heterogeneous uploaded calculations (raw code outputs) | `not implemented` | Calculations come from many codes, settings and uploaders; per-value method provenance would have to be derived from raw outputs, which is property extraction the pack excludes. Data CC BY 4.0 (_verify_). Link-only for citation of specific entries |

### Explicit exclusions (closed commercial sources)

| Source | Why excluded |
| --- | --- |
| MatWeb | Commercial site; terms of use prohibit automated retrieval and redistribution (_verify_ current wording). Values are largely vendor datasheets without method provenance |
| Ansys Granta (MI, EduPack, Selector) | Licensed commercial databases; no redistribution rights |
| Springer Materials | Subscription database; content licensed per institution, not redistributable |
| ASM handbooks and ASM Materials Platform | Subscription/copyrighted handbook content |

A value quoted from one of these in a paper can still reach a dossier as that
*paper's* citation (MT10); the database itself is never acquired.

## Per-source contract

| Source | Endpoint and authentication | Licence and attribution | Rate limits | Stable IDs | Release / versioning semantics |
| --- | --- | --- | --- | --- | --- |
| Materials Project | `https://api.materialsproject.org/materials/summary/`, `/materials/thermo/`, `/materials/elasticity/` (_verify_ paths); header `X-API-KEY` from `NOESIS_MATERIALS_PROJECT_API_KEY` (credential store, never committed) | data CC BY 4.0 (_verify_); cite A. Jain et al., *APL Materials* 1, 011002 (2013), doi:10.1063/1.4812323 and the database version | about 25 requests/s per key (_verify_); `_limit`/`_skip` paging, bounded by `max_pages` | `material_id` (`mp-<n>`); thermo documents carry `thermo_type` | named database versions (e.g. `2023.11.1`, _verify_ format) announced in the release notes; the version in effect is stated by the API (`meta`/heartbeat `db_version`, _verify_). The source pack declares the release label when the response does not state one, labelled `operator-declared` |
| JARVIS-DFT | pinned snapshot file `https://figshare.com/ndownloader/files/<id>` (_verify_), no authentication | NIST data; figshare record licence CC BY 4.0 (_verify_); cite K. Choudhary et al., *npj Comput. Mater.* 6, 173 (2020), doi:10.1038/s41524-020-00440-1 | figshare download limits (_verify_); one file per run | `jid` (`JVASP-<n>`) | snapshot name carries the date (e.g. `dft_3d_2021.8.18`, _verify_); the label and date are declared in the source pack |
| OQMD | `https://oqmd.org/oqmdapi/formationenergy?composition=...&fields=...&limit=...&offset=...` (_verify_ scheme and field names), no authentication | CC BY 4.0 (_verify_); cite J. E. Saal et al., *JOM* 65, 1501 (2013), doi:10.1007/s11837-013-0755-4 and S. Kirklin et al., *npj Comput. Mater.* 1, 15010 (2015), doi:10.1038/npjcompumats.2015.10 | none published (_verify_); polite single-threaded paging | `entry_id` (plus `icsd_id` when the entry derives from ICSD) | database releases `v1.5`, `v1.6`, … (_verify_); the response `meta` may state the version (_verify_), otherwise the source pack declares it |
| NIST Chemistry WebBook | `https://webbook.nist.gov/cgi/cbook.cgi?ID=<id>&Units=SI&Mask=2` (condensed phase) and `&Mask=4` (phase change) (_verify_ mask values), no authentication | SRD 69, © U.S. Secretary of Commerce under the Standard Reference Data Act (_verify_ redistribution terms); cite P. J. Linstrom and W. G. Mallard (eds.), NIST Chemistry WebBook, SRD 69, doi:10.18434/T4D303, plus each value's own reference | no published limit (_verify_); one request per declared species and mask | WebBook species ID (e.g. `C13463677`), InChI as printed on the page | the WebBook states a data-collection release on its pages (_verify_ wording); the source pack declares the release label/date, labelled `operator-declared` |
| COD | `https://www.crystallography.net/cod/result?formula=...&format=json` (_verify_), CIF at `/cod/<id>.cif` (not fetched in v1), no authentication | public domain / CC0 (_verify_); cite S. Gražulis et al., *Nucleic Acids Res.* 40, D420 (2012), doi:10.1093/nar/gkr900 and each entry's publication | none published (_verify_) | 7-digit COD ID; entry revision (`svnrevision`, _verify_) | continuous; an entry revision number identifies a changed entry. The release label of a run is the revision set, labelled `entry-revision` |

**Unavailable-access fallback.** A failed request (HTTP error, redirect, host
outside the declared set, budget exhausted, schema drift) fails that source's
run with its code; stored values and releases are unchanged and the provider
reads as stale. No value is marked withdrawn because a request failed.
Withdrawn or deprecated status is recorded only when the source states it (MP
`deprecated`, COD entry status).

**Units.** Every source's units are declared per field in the adapter and
normalised by the exact, pint-free tables in `src/kb/materials_units.py`; a
field whose unit the source does not state, or states ambiguously (e.g. `kcal`
without thermochemical/IT qualification), is `unit_unknown` and excluded from
normalised comparison.

## Bounded v1 coverage

| Source | Selection | Properties | Record ceiling |
| --- | --- | --- | --- |
| Materials Project | chemical systems `Ti-O` and `Al-O`; summary, thermo and elasticity documents for those systems | formation energy per atom, energy above hull, band gap, density, bulk and shear modulus (VRH), structure summary (space group, lattice, volume, sites) | 50 materials per run |
| JARVIS-DFT | the same chemical systems, filtered from one pinned `dft_3d` snapshot | formation energy per atom (OptB88vdW), band gap (OptB88vdW and TBmBJ, kept separate), bulk modulus Kv and shear modulus Gv (OptB88vdW), density, structure summary | 50 entries per run |
| OQMD | compositions `TiO2` and `Al2O3` | formation energy (`delta_e`), hull distance (`stability`), band gap, structure summary | 50 entries per run |
| NIST WebBook | declared species (rutile TiO2, corundum Al2O3) | standard enthalpy of formation, standard entropy, heat capacity at stated temperatures, fusion temperature | 10 species per run |
| COD | formulas `O2 Ti` and `Al2 O3` | space group, lattice, measurement temperature and pressure, publication reference | 50 entries per run |

The offline fixtures under `tests/fixtures/materials/` are **authored and
fictional** (IDs in the 99xxxxx range, invented numbers) in each provider's
documented shape; they prove parsing and semantics, not live coverage.
Offline and live evidence are reported separately: live runs belong in
`docs/development/materials-evidence/live-check-<date>.json` (MT14).
