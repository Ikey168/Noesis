# Legal pack: German federal statutes and Federal Law Gazette promulgations

The Legal bundle's optional `federal-statutes` feature (#2105) answers what a
German federal statute provision **said** on a date, which Federal Law
Gazette (BGBl) acts amended it, which Bundestag dossier produced each act,
which EU acts they state they implement, and which federal court decisions
cite the provision. It is an expansion of Legal, not a new pack: one more
provider (`legal.federal-statutes`, record owner `src/kb/legal_federal.py` on
top of `LegalStore`) plus three sources in the existing `legal-research`
source pack (1.3.0).

Every answer says which evidence it rests on:

* **source-stated** - rechtsinformationen.bund.de states the version's validity
  interval (`validity_from` / `validity_to`);
* **observed** - gesetze-im-internet.de showed this text on a date; no validity
  is stated, and the source's "Stand" note is kept verbatim.

The two are never conflated. No answer states that a provision is in force,
determines applicability, computes a consolidation from amending
instructions, or gives legal advice.

## Enabling

Off by default. Select it with the Legal root (`features: ["federal-statutes"]`;
the composition schema only allows hyphenated feature ids, so the tracking
issue's `federal_statutes` is spelled `federal-statutes`). It binds
`legal.federal-statutes`, `legal.core`, `political.core` (Bundestag DIP
dossiers), `platform.subscriptions` and `platform.source-runtime`, and composes
with the `sanctions` feature. `packs/legal/pack.json` (v1) is unchanged.
`src.kb.legal_federal.feature_enabled` reads the active plan.

## Sources (pack `legal-research` 1.3.0)

| Source | What | Access decision |
| --- | --- | --- |
| `gii-federal-statutes` | gesetze-im-internet.de TOC + per-statute XML for the bounded set | `implement` (unverified-live) |
| `ris-federal-statute-versions` | rechtsinformationen.bund.de expressions (LegalDocML.de, ELI, stated validity) | `implement` behind the feature (trial service, unverified-live) |
| `bgbl-federal-promulgations` | recht.bund.de digital BGBl listing + promulgations (since 2023) | `implement` (unverified-live); pre-2023 BGBl is link-only |

Bounded statute set: BGB, HGB, GmbHG, AktG, WpHG, KWG, VwVfG, GG, ZPO, InsO,
VwGO. See `docs/development/federal-statutes-evidence/source-audit.md` for
terms, identifiers, validity semantics and every claim still to verify live.
1.3.0 adds sources only and changes none, so the existing `^1.1.0` and
`^1.0.0` pins keep resolving; `legal.federal-statutes` pins `^1.3.0`.

## MCP tools (noesis-knowledge-engine)

| Tool | Scopes | Semantics |
| --- | --- | --- |
| `get_federal_provision(namespace, statute, provision, as_of)` | `knowledge:legal:read` | source-stated version covering the date; else nearest observed version on or before it ("observed on <date>, validity not stated"); else "no version on record"; differing sources side by side (`conflict`) |
| `compare_provision_versions(namespace, statute, provision, left, right)` | `knowledge:legal:read` | provision-level diff between two version ids or two dates, plus the amendment acts the later version itself names |
| `list_amendment_acts(namespace, statute?, provision?)` | `knowledge:legal:read` | BGBl citation, promulgation date, entry-into-force text, instructions with locators, dossier and EU links |
| `decisions_citing_provision(namespace, statute, provision?, court?, date_from?, date_to?)` | `knowledge:legal:read` | explicit citations with decision locator (Rn./paragraph + offsets) and the version selected for the decision date; `a.F.` points to the preceding differing version or stays flagged |
| `resolve_statutory_citation(namespace, citation)` | `knowledge:legal:read` | `§ 5 Abs. 2 Satz 1 Nr. 3`, `§§ 33 ff.`, `Art. 20 Abs. 3 GG`, `i.V.m.`, `a.F./n.F.`; unknown abbreviations unresolved |
| `list_federal_statute_versions(namespace, statute)` | `knowledge:legal:read` | every version with its basis, stated validity or sightings |
| `link_amendment_dossiers(namespace, dossier_namespace?)` | `knowledge:legal:write`, `knowledge:political:dossier:read` | `enacted_as` links where a DIP dossier stage states the act's BGBl citation; unmatched acts keep the reason |
| `create_statute_monitor` / `run_statute_monitor` / `poll_statute_monitor` | legal read + subscriptions | watch a statute, provision or act; events: new act, changed observed text, new/corrected stated version, new citing decision, dossier link |

Reads return `not_ready` before any federal source has run.

## Example journey (offline fixtures, fictional statute)

```
get_federal_provision(namespace="global", statute="MPHG", provision="§ 5 Abs. 2 MPHG", as_of="2031-02-01")
  -> status "observed", label "observed on 2030-06-01, validity not stated"
compare_provision_versions(..., provision="§5/abs2", left="2030-03-15", right="2031-02-01")
  -> "zehn" -> "fünf Handelstagen"; act BGBl. 2030 I Nr. 45 named by the later version's Stand note
decisions_citing_provision(..., statute="MPHG", provision="§5/abs2")
  -> two decisions; the "a.F." citation points to the source-stated version valid from 2029-06-01
```

Offline evidence: `tests/unit/domains/test_federal_statutes_acceptance.py`.
Live evidence: outstanding (#2119), to be recorded under
`docs/development/federal-statutes-evidence/`.
