# Contributing to Noesis

## Setup and checks

Create a virtual environment and install the minimal or task-specific profile
documented in `README.md`. Run `mise run check` for the maintained core gateway
and contract suite. Use focused pytest and Ruff commands while iterating; run
specialist workflow, model, browser, or live-provider gates only when the change
touches them and record those results separately.

Acceptance evidence names the command, result, environment, data fixture, and
any omitted external gate. A module importing, an index being present, or a
schema validating does not by itself prove retrieval quality, citation
integrity, recovery, or an external integration.

## Change contract

- Preserve source bytes/text, annotations, claims, citations, provenance, and
  stable identifiers. Do not replace an authority with a derived index.
- Version schemas and contracts under `contracts/`; add compatibility and
  regression tests with each contract change.
- Record durable architecture decisions under
  `docs/architecture/decisions/ADR-NNN-short-title.md` and supersede rather than
  rewriting accepted decisions.
- Document environment-variable names and safe defaults. Never commit live
  credentials, private corpora, production databases, or recovery material.
- Do not commit virtual environments, caches, coverage, model caches, local
  warehouses, indexes, logs, or generated exports unless a documented fixture
  or release process explicitly requires the artifact.
- Use reviewable branches and commits. Do not push, release, deploy, ingest
  external data, delete state, or rewrite shared history without authorization.
