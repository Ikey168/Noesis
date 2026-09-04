# Quantitative semantic layer

Noesis treats a number as a versioned knowledge object rather than an
unlabelled scalar. Metric definitions bind a stable identity to a unit,
dimension, frequency, population, aliases, provider-native series mappings,
and an optional safe formula. Changes append immutable metric revisions, so a
calculation can pin the exact definition it used.

Observations preserve valid time, release time, retrieval time, provider,
series identifier, vintage, preliminary status, adjustment, missingness, and
provenance. A revised provider value creates another vintage and links back to
the prior observation. Default series reads select the latest vintage for each
period and provider as known at `as_of_ms`; callers can explicitly request all
vintages. Compatible observations are also projected into the established
`dataset_series` and `dataset_observations` tables.

The unit registry supports aliases, compound dimensions, affine units, and
versioned currencies. Conversions use decimal arithmetic and half-even
rounding. Currency conversion requires an explicit matching rate receipt;
currency redenomination requires a registered successor and factor. Inflation
adjustment and frequency aggregation similarly retain their exact inputs.
Derived formulas use a deliberately small arithmetic language and reject
unsupported syntax or declared input-dimension mismatches.

Every transformation produces a durable content-addressed calculation receipt
with ordered input identifiers, the metric formula revision when applicable,
and a replayable hash. Repeating the same request returns the original receipt
instead of manufacturing a new timestamp.

Comparability assessments inspect dimensions, seasonal adjustment, provider
changes, and sourced series breaks. Supported breaks cover definitions,
methodologies, geographies, rebases, baskets, and provider switches. A break is
reported rather than silently splicing incompatible observations together.

MCP access is divided into `knowledge:quantitative:read`,
`knowledge:quantitative:write`, and `knowledge:quantitative:calculate`. The
surface provides metric discovery and history, vintage/as-of reads,
comparability explanations, unit and metric registration, observations and
breaks, conversions, formulas, frequency and inflation transforms, and receipt
replay.
