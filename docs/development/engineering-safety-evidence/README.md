# Engineering Safety evidence (#2059)

| Evidence | Kind | Where |
| --- | --- | --- |
| Source-contract audit and access decisions (ES01) | written decision, every unverified claim marked **(verify)** | [source-audit.md](source-audit.md) |
| Offline journey: subject to cited dossier (ES16) | fixture-driven test, sockets blocked, no credentials | `tests/unit/engineering_safety/test_acceptance.py` |
| Per-source parsing on authored fixtures (ES03-ES09) | unit tests | `tests/unit/engineering_safety/test_sources.py` |
| Bounded live validation (ES17, #2078) | **not done**: no dated live run exists | this directory, once recorded |

Offline evidence proves parsing, revision, identity, citation, query and
monitoring behaviour on **authored, fictional** envelopes in each provider's
documented shape. It does not prove that the live services answer in that
shape, that the terms allow automated retrieval, or that the declared
selections cover the intended aircraft types, vehicles and operators. Every
implemented source stays `unverified-live` (see `LIVE_VERIFICATION` in
`src/ingestion/engineering_safety_sources.py`) until a dated live run is
recorded here.
