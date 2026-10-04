# Media outlets and ownership evidence (news.outlets)

Evidence for the News bundle's proposed `news.outlets` provider (subdomain
`media-outlets-ownership`, wave 2 tracker #2736; no per-track issue yet).

- [source-audit.md](source-audit.md) - MO01: per-candidate contract (Media
  Ownership Monitor, MAVISE, KEK media database), licence, access, the
  data-minimisation decision for owners who are natural persons, and the
  bounded first coverage should a source survive. Written without network
  access; terms not re-verified live. None of the three survives.
- [source-audit.md, amendment](source-audit.md#amendment-further-candidates-2754) -
  #2754: Ofcom licence lists, the medienanstalten TV station database,
  EMFA Article 6 national databases and Wikidata ownership statements through
  the existing `wikidata` connector. Also written offline. Only Wikidata
  survives, narrowly and `unverified-live`, as "what Wikidata states" and
  never as a register; the rest are `not-implemented`, and the subdomain stays
  a gap until a tracker's live validation passes.
- Offline evidence: none yet. No fixtures, provider or
  `src/ingestion/media_outlets_sources.py` exist.
- Live evidence: none yet. A `not-implemented` source becomes
  `unverified-live` only after an operator confirms a documented export and
  reuse terms, as the audit sets out; Wikidata's ownership statements are
  `unverified-live` because the existing connector's access path and CC0
  terms are documented, not because anything was fetched.
