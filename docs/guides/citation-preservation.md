# Citation preservation

Citation preservation is policy-gated. Versioned policies decide whether
robots-restricted, privately accessible, or specifically licensed content may
be retained; whether excerpts and assets are included; which archives are
approved; and the maximum capture size. A denied capture produces an explicit
omission in its manifest rather than bypassing access or legal constraints.

At citation time Noesis records the source URL and redirects, response facts,
retrieval time, exact locator, excerpts, asset references, generation, policy
revision, and provenance. Preserved content is addressed by its SHA-256 hash,
so duplicate bytes are stored once while citation identities stay distinct.
Partial downloads and truncation are explicit. A deterministic manifest makes
retry-after-crash and replay checks safe.

Verification always runs against a pinned preserved snapshot. Results distinguish
support, contradiction, ambiguous OCR drift, a moved passage, content that no
longer contains the cited passage, and an unverifiable omission. The result
retains the original assertion, locator, blob hash, and calibrated confidence.

Availability checks distinguish ordinary failures, soft 404s, paywalls, and
takedowns. Archive repair is a two-step preview and acceptance process. Only an
archive allowed by the pinned policy with an exact content-hash match is
eligible. Acceptance records an equivalent-copy relation and never changes the
original evidence identity or locator.

MCP permissions use `knowledge:citation:read`, `knowledge:citation:write`,
`knowledge:citation:capture`, and `knowledge:citation:repair`. Reads are bounded
and namespace-isolated; snapshot inspection does not expose preserved content.
Dependency-complete exports include manifests, exact policy revisions,
verification records, health observations, and repairs for all six primary
Noesis research domains.
