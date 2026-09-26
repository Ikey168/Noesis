"""Pack and workflow composition (docs/architecture/pack-workflow-composition.md).

Modules:

* :mod:`src.composition.contracts` - manifest, provider, plan, readiness and
  activation-receipt contracts plus the ``noesis-pack-v1`` adapter (C02).
* :mod:`src.composition.resolver` - pure, deterministic resolution (C03).
* :mod:`src.composition.readiness` - operation-specific readiness and catalog
  explanations (C04).
* :mod:`src.composition.lifecycle` - durable selection, activation journal and
  generation switch (C05).
* :mod:`src.composition.sources` - source-pack impact, schedule ownership,
  acquisition reuse and shared limits (C06).
* :mod:`src.composition.workflows` - workflow templates, plan binding and the
  authorized dispatcher (C07).

Imports stay lazy between modules so the pure resolver never pulls in storage.
"""
