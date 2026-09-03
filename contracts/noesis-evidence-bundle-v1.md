# noesis-evidence-bundle-v1

A portable, content-addressed package for carrying Noesis answers, claims,
integrity records, and machine-checkable receipts outside the warehouse.
The authoritative JSON Schema is
`contracts/schemas/jsonschema/noesis-evidence-bundle-v1.json`.

## Security and truth boundary

Successful verification proves that the bundle matches its manifest, local
references resolve, evidence requirements are present, and analytic envelopes
are structurally honest. It does **not** prove that a source is truthful, that
an inference is correct, or that an external URL still serves the cited bytes.
No verifier step fetches a URL.

## Canonical representation

`noesis-json-c14n-v1` accepts JSON values only, rejects NaN and infinity, sorts
object keys lexicographically, emits UTF-8 without ASCII escaping, removes
insignificant whitespace, and uses Python-compatible JSON number rendering.
It is intentionally not labeled RFC 8785.

Every object digest is SHA-256 over the canonical form of:

```json
{"id": "…", "type": "…", "payload": {}, "references": []}
```

The manifest repeats each object id, type, and digest. `bundle_id` is SHA-256
over the canonical header: contract, creation time, operation, roots, manifest,
external references, and completeness. Object contents are therefore covered
indirectly through their manifest digests without a cyclic hash.

## Object model

- `answer` — structured answer statements. Each factual statement has cited
  `evidence_refs` or an explicit `unverifiable`, `uncited`, or refusal verdict.
- `claim` — one claim, its model provenance, corroboration, contradiction, and
  cited carrying/evidence documents.
- `integrity` — the integrity-ledger view of a document. Every finding keeps its
  evidence locators, including both sides of a revision.
- `receipt` — an existing machine-checkable Noesis flow, such as the evidence
  showcase.
- `evidence` — a normalized source locator referenced by another object.
- `model_pin` — the immutable model name and resolved revision for any
  `pretrained:` or `zero-shot:` prediction mode in the exported result.

`references` are bundle-local object ids and must form a closed graph. `roots`
name the objects a consumer requested.

Exporters require `include_private=True` when a result or locator is marked
private and record that choice in operation inputs. Connection-based exporters
also accept an explicit `visibility` because authorization remains the calling
application's responsibility; the bundle layer never opens a warehouse or
widens domain access on its own.

## Content modes and completeness

Evidence metadata is embedded as objects. Large source bytes can be declared
under `external_references`:

- `adjacent`: a relative path beside the bundle with a required SHA-256 digest;
  absolute paths, `..`, and symlink escapes are rejected.
- `external`: an opaque URI and optional known digest. Offline verification
  never fetches it and reports `valid_with_external_references`.

Known omissions set `completeness.status` to `partial`, producing an
`incomplete` verification status rather than silently treating the bundle as
complete.

## Verification statuses and exit codes

| Status | Meaning | Exit |
|---|---|---:|
| `valid` | Complete, schema-valid, hashes and local references verified | 0 |
| `valid_with_external_references` | Valid embedded content; external bytes were not fetched | 0 |
| `incomplete` | Structurally sound but declared content is omitted or unavailable | 2 |
| `invalid` | Schema, digest, reference, evidence, honesty, or path check failed | 1 |

Run either entry point from a checkout:

```bash
./noesis verify evidence.json --json
python -m src.evidence_bundle verify evidence.json --json
python scripts/evidence_bundle.py verify evidence.json
```

The unified `noesis verify` command planned by the packaging epic should call
this same library rather than implement a second verifier.

The repository verifier caps the JSON envelope at 64 MiB and hashes adjacent
files as a stream. Large source material belongs in adjacent or external
references rather than an unbounded embedded payload.

## Compatibility

New optional object payload fields may be added without changing the bundle
envelope. Changes to canonicalization, digest inputs, required envelope fields,
or existing field meaning require a new contract version. Internal DuckDB
table names are never part of the wire format.
