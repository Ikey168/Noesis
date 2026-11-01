# Patents evidence (technology.patents)

Evidence for the Technology bundle's `technology.patents` provider (subdomain
`patents`, EPO OPS source `epo-ops-patents`, store `src/kb/patents.py`).

- [depth-audit.md](depth-audit.md) - DP01 (#2827, wave 4 tracker #2825):
  second-publisher audit under ADR-006. Recommends the USPTO (PatentsView
  PatentSearch API or the Open Data Portal) for `technology.patents` after a
  provider widening of `noesis-patent-part-v1`; DPMAregister and WIPO
  PATENTSCOPE recorded as `not-implemented`. Families only through published
  identifiers. Written without network access (2026-11-01); terms marked
  _verify_, no source live.
- Offline evidence: `tests/fixtures/source_packs/patents-epo-ops.json`. Live
  evidence: none yet.
