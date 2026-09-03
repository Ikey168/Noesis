# Clean Heat fixture

This directory contains a wholly synthetic policy-monitor scenario. All names,
institutions, URLs, rules, votes, memos, and measurements are fictional. The
authors dedicate the fixture files to the public domain under CC0-1.0.

- `corpus.json` is the reviewable manifest: seven reporting/policy documents,
  a silent primary-text revision, one filing observation, one private memo,
  fixed timestamps, and the redistribution declaration.
- `votes.json` is the local input consumed by the registered legislative
  connector.
- `domains.yml` defines disjoint public and private corpus-view domains.
- `expected.json` freezes the logical receipt and watch expectations.
- `live.example.yml` is disabled by default and documents opt-in live source,
  permission, and rate-limit requirements. The offline demo never reads it.

The generated legislative and filing documents inherit the manifest's CC0
declaration during canonical connector ingestion. The private memo is included
only to exercise authorization boundaries; it is never part of the public
expected receipt or default evidence bundle.
