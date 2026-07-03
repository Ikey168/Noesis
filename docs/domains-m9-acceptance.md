# M9 acceptance: the domain-pack lifecycle, package to live

Milestone M9 (issues #687-#690) turns a domain pack from a code module into a
distributable, installable unit. This is the acceptance record; its executable
form is `scripts/domains/m9_acceptance.py`, run in CI by
`tests/unit/domains/test_m9_acceptance.py`.

## What it proves

The harness runs the full lifecycle on the shipped example pack
(`packs/energy/pack.json`) and confirms its capabilities are live in a fresh
instance.

1. **Package (M9.1).** The manifest is packaged into `noesis-pack-v1` and
   validates against the contract (`src/domains/pack_format.py`). A pack is one
   `pack.json` bundling panels, planner keywords, enrichers, provisioning
   templates and UI flags — all declarative, no Python.
2. **Publish (M9.2).** It publishes to a registry
   (`src/domains/pack_registry.py`) and is discoverable and versioned there
   (`<root>/<name>/<version>/pack.json`, immutable versions, semver latest).
3. **Install (M9.3).** A fresh instance installs it from the registry
   (`src/domains/pack_install.py`) with no code changes: panels register into the
   catalog, keywords into the planner, enrichers compile to pure functions, and
   provisioning templates become deployable.
4. **Live.** Every capability is exercised:
   - the pack panel `energy_outages` resolves in the catalog and validates in a
     ui-spec;
   - the `energy_tag` enricher runs and tags a matching document;
   - the `energy` ui_flag is active in `merged_ui_flags`;
   - the pack's keywords steer the planner (`grid megawatt capacity` scores the
     trend facet);
   - the `energy_kg` provisioning template deploys through the Provisioner.

The harness cleans up every runtime registration at the end, so it leaves no
global state behind.

## Result

```
1. package: energy 1.0.0 validates: True
2. publish: discoverable=True, versions=['1.0.0']
3. install: energy 1.0.0 panels=['energy_outages'] enrichers=['energy_tag'] templates=['energy_kg']
4. live: panel=True, enricher=True, ui_flag=True, planner=True, template_deployed=True

RESULT: OK - packaged, published, installed, and every capability is live
```

## Why this matters

Before M9, adding a domain meant editing the catalog, the planner keyword map,
the enricher registry, and wiring a `DomainPack` in code. After M9, a domain is
data: authored once as a manifest, published to a registry, and installed into
any instance — the ecosystem story for third-party and per-tenant domains.
