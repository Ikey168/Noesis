# Cross-language knowledge

Noesis keeps original-language evidence as the source of truth. A language text
record captures the exact original Unicode string, its NFC normalization,
language, script, locale, direction, revision, and explicit code-switch spans.
Normalization is searchable metadata; it never overwrites the original.

Multilingual aliases and transliterations are evidence-bearing candidates.
Reviewers may accept or reject them, or retain ambiguity for homonyms,
historical spellings, and abbreviations. Claim alignments distinguish
`translated`, `equivalent`, `narrower`, `broader`, and `divergent`; comparison
always returns both original wordings and assessments of negation, modality,
idioms, and numeric formatting.

Translations identify their human, source, or model producer and version. New
model output creates a new version, while reviews and disputes append history.
Partial-passage locators and alternatives remain attached to the translation.

The MCP surface provides recording and review operations plus original-text
retrieval, aligned-claim comparison, and multilingual search. Search interleaves
languages after scoring so a high-volume language cannot monopolize a bounded
result set. Translation hits contain the original wording and language.

Scopes are `knowledge:cross-language:read`, `knowledge:cross-language:write`,
and `knowledge:cross-language:review`. All storage and lookup operations are
namespace-bound and mutation events are audited.
