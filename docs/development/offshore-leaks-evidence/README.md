# Offshore leaks evidence (corporate-ownership `offshore-leaks` feature)

Evidence for the Corporate Ownership bundle's optional `offshore-leaks` feature
(#2888, provider `icij-offshore-leaks`).

- [source-audit.md](source-audit.md): publisher, download format, licence
  (ODbL / CC BY-SA, attribution), ICIJ's caveat, personal-data assessment,
  mapping decisions, bounds and exclusions. It was written without access to
  ICIJ's hosts. Each item is marked verified (via web-search quotes of ICIJ
  pages) or **verify**.
- Offline evidence: the authored fixture
  `tests/fixtures/ownership/icij-offshore-leaks.json` holds fictional nodes in
  the ICIJ CSV shape, with 10-digit node ids outside ICIJ's range. It is
  pinned in `config/source_packs/corporate-ownership.json` (source
  `icij-offshore-leaks`) and replays through the real adapter.
  `tests/unit/ownership/test_offshore_leaks.py` covers the bounded selection,
  verbatim roles, owner-scoped persons, withheld person addresses, quoted
  `same_as`/`similar` links, the never-acceptable `similar-name` basis, the
  forbidden-verdict guard, person-research mode, and the bulk-runner subset
  projected with the archive digest. It also covers the entity dossier and
  its export, with offshore links cited to the leak dataset and node id.
  Offline status: covered offline, not live.
- Live evidence: none. The source is `unverified-live` until a dated run
  checks the archive URL, member names and columns.
- Guide: [corporate-ownership-offshore-leaks.md](../../guides/corporate-ownership-offshore-leaks.md).
