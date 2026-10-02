# Platform transparency evidence

Evidence for the OSINT pack's `osint.platform-transparency` provider (tracker
#2580, subdomain `social-platforms`).

- [`source-audit.md`](source-audit.md) - SP01: per-source contracts, the
  data-minimisation decision, bounded first coverage and `LIVE_VERIFICATION`.
- Offline acceptance:
  `tests/unit/domains/test_platform_transparency_acceptance.py` replays the
  authored fixtures under `tests/fixtures/platform_transparency/` through the
  source-pack runtime with sockets blocked.
- Live evidence: none. Every source is `unverified-live`; Lumen is
  `gated-not-granted`. The dated live run is SP14 (#2650).
