# Film and broadcast evidence (culture.film-broadcast)

Evidence for the proposed `culture.film-broadcast` provider of the `culture`
bundle ([ADR-007](../../architecture/decisions/ADR-007-culture-bundle.md))
(subdomain `film-broadcast`, audit issue #2820, wave 3 tracker #2814; no
per-track issue yet).

- [source-audit.md](source-audit.md) - FB01: per-candidate contract for
  Wikidata, the European Audiovisual Observatory's LUMIERE and MAVISE, and
  TMDB; licence, access, the data-minimisation decision for persons named on
  film items, and the bounded first coverage. Written without network access;
  terms not re-verified live. Only Wikidata survives, `unverified-live`, as
  "what Wikidata states" and never as a register; LUMIERE, MAVISE (MO01 finding
  reused) and TMDB are `not-implemented`.
- Offline evidence: none yet. No fixtures, provider or ingestion module exist.
- Live evidence: none yet. Wikidata is `unverified-live` because the existing
  connector's access path and CC0 terms are documented, not because anything
  was fetched.
