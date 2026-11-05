# Market pack: BaFin capital-market notices

The Market bundle's optional `bafin-notices` feature (#2106) assembles the
capital-market notices published under German and EU law for a German-listed
issuer, holder or person:

* voting-rights notifications (WpHG §§ 33 ff.): thresholds touched, the chain of
  controlled undertakings as stated, and percentages split into § 33 (shares),
  § 38 (instruments) and § 39 (total);
* managers' transactions (Art. 19 MAR), with every trade and the aggregate as
  published;
* published net short positions (Art. 11 SSR) with publication-end semantics;
* the BaFin company database (authorised entities and their licences);
* BaFin warnings on unauthorised business and published measures.

Every notice carries its event time, its publication time, its corrections, its
source and its locator. Every query applies the Market publication cutoff: **a
notice published after the as-of cutoff is never seen**, even when its event
date is earlier. The feature is an expansion of Market, not a new pack. It adds
one provider (`market.bafin`, record owner `src/domains/market/bafin_notices.py`)
and one source pack (`bafin-capital-market-notices` 1.0.0).

No answer is investment advice, a signal, a trading recommendation or a
sentiment score. Holdings are never summed across notifiers or inferred below a
publication threshold. A position that stops being published is "below
publication threshold or closed", never 0 %.

## Enabling

The feature is off by default. Select it with the Market root
(`features: ["bafin-notices"]`). Composition feature ids only allow hyphens, so
the tracking issue's `bafin_notices` is spelled `bafin-notices`. The feature
binds `market.bafin`, `market.core`, `market.lei`, `platform.entity-identity`,
`platform.subscriptions` and `platform.source-runtime`, and
`packs/market/pack.json` (v1) is unchanged.
`src.domains.market.bafin_notices.feature_enabled` reads the active plan.

The projection into the ownership graph is a Corporate Ownership feature,
`bafin-voting-rights` (off by default), which binds `market.bafin` beside
`ownership.core`. Corporate Ownership already depends on `market.lei`, so a
Market feature that required ownership capabilities would form a bundle cycle.
The composition therefore runs from Corporate Ownership to Market, and the
`market.bafin` descriptor declares the ownership composition in its semantic
constraints.

## Sources (pack `bafin-capital-market-notices` 1.0.0)

| Source | What | Access decision |
| --- | --- | --- |
| `bafin-voting-rights` | AnteileInfo result-list CSV export, declared column mapping | `implement` (unverified-live) |
| `bafin-managers-transactions` | DealingsInfo result-list CSV export; person data withdrawn when a transaction leaves the listing | `implement` (unverified-live) |
| `bundesanzeiger-net-short-positions` | historical and current position lists (CSV) | `implement` (unverified-live; the download may be session-bound) |
| `bafin-company-database` | InstInfo export, bounded to declared BaFin IDs (none declared yet) | `implement` (unverified-live) |
| `bafin-warnings-measures` | BaFin RSS feeds; never a complete listing | `implement` (unverified-live) |

The Unternehmensregister and licensed news-wire distributions are not
implemented. The bounded issuer set (DAX 40 constituents, starting with the
declared ISINs) and the publication window (from 2025-01-01), plus every claim
still to verify live, are in
`docs/development/bafin-notices-evidence/source-audit.md`.

## Record semantics

* **Current and history.** Re-acquiring unchanged content adds nothing. Content
  is compared as a normalised, source-independent representation, and only with
  the current revision, so a reversion is a new revision. "Current" follows the
  source's own date, then observation order. A late-arriving older export is
  kept as history.
* **Corrections** ("Korrektur") are notices linked to the notice they correct,
  by stated identifier or by same issuer, notifier and corrected publication
  date. Queries show one notice per chain: the latest published by the cutoff.
* **Removals** from a complete listing are listing observations
  (`no_longer_listed`, with the date observed), never deletions.
* **Publication clock.** A publication date counts as the end of that day (UTC).
  When the source states none, the first observation time is the clock, never
  the event date. An edited revision is visible from the time it was first
  observed.
* **Personal data.** Managers' names are held in a person table under a
  pseudonymous reference scoped to the notice and its issuer. Revisions only
  hold that reference. Natural persons are matched only within their issuer,
  and a cross-issuer candidate only exists when a reviewer proposes it (or,
  in person-research mode, `NOESIS_PERSON_RESEARCH`, when the same normalised
  name appears under different issuers; it still stays proposed until reviewed).

## Holdings of one holder across issuers

`bafin_holdings_of_holder` is the inverse of `bafin_holders_as_of`: instead of
"who holds issuer X" it answers "what does holder Y hold, issuer by issuer, as
of a date" under the same `public_and_acquired` cutoffs.

* **The holder** is a party record key (`bafin:party:...` for an organisation,
  `bafin:person:...` for a natural person), an organisation name (keyed by its
  normalised name, which is the same record in every issuer), or
  `kind="natural_person"` with a name and the ISIN of an issuer whose notice
  names the person. Persons are keyed per issuer, so a name alone never finds
  a person elsewhere.
* **Per issuer**, the answer lists the latest notification per notifier
  published by the cutoff that names the holder as notifier or as a member of
  the stated chain, with the chain, corrections, earlier notifications, staleness
  and every stated percentage beside its WpHG basis (§ 33, § 38, § 39). Nothing is
  summed across issuers or bases. A holding whose latest notification is below
  the lowest threshold is listed apart as stated; an issuer without a
  notification naming the holder is absent, never 0 %.
* **Identity.** Other records join the holder only through accepted,
  unreverted identity decisions (`review_bafin_identity_match`), followed
  transitively; the answer names the joined records and the accepted candidate
  ids. Proposed candidates are listed under `unjoined_candidates` with the
  other record's holdings, never merged into the holder's. A reverted decision
  no longer joins. Natural persons join across issuers only in person-research
  mode; with the switch off a person is read within its own issuer. A
  similar-name candidate is never accepted, so a reviewer adds the attribute
  evidence they checked (`propose_bafin_identity_link(..., attributes=[...])`)
  before accepting.
* **Evidence.** Every listed notification is pinned by revision;
  `export_bundle=true` returns a `noesis-evidence-bundle-v1` with one evidence
  object per pinned revision (`export_holdings_bundle`).

## MCP tools (noesis-knowledge-engine)

| Tool | Scopes | Semantics |
| --- | --- | --- |
| `bafin_holders_as_of(namespace, isin, as_of, acquired_by_ms?, threshold?)` | `market:bafin:read` | latest notification per notifier published by the cutoff, chain as stated, percentages by basis, thresholds reached; stale holders keep their last notice date |
| `bafin_holdings_of_holder(namespace, as_of, party?, name?, kind?, isin?, acquired_by_ms?, export_bundle?)` | `market:bafin:read` | per issuer, the holder's latest notification by the cutoff (as notifier or chain member) with percentages by basis; joined only through accepted identity decisions, proposed candidates listed unjoined; never summed |
| `bafin_managers_transactions(namespace, date_from, date_to, isin?, person?, as_of?, acquired_by_ms?)` | `market:bafin:read` | notifications in the window with trades and aggregate; amendments supersede |
| `bafin_net_short_positions(namespace, isin, as_of, acquired_by_ms?)` | `market:bafin:read` | latest published position per holder; ended positions are "below publication threshold or closed"; a labelled sum of published positions |
| `bafin_warnings_for_entity(namespace, name?, party?, as_of?, acquired_by_ms?)` | `market:bafin:read` | name-equal strings (not attributed) and reviewed matches; removals kept |
| `bafin_authorisation_status(namespace, as_of, bafin_id?, name?, acquired_by_ms?)` | `market:bafin:read` | listing and licences as of the date |
| `bafin_notice_dossier(namespace, isin, as_of, ..., market_namespace?, lei_namespace?, export_bundle?)` | `market:bafin:read` (+ `market:instruments:read` / `knowledge:companies:read` when those namespaces are given) | holders, dealings, short positions, warnings and measures naming related entities and their authorisation, every notice pinned by revision; `export_bundle` returns a `noesis-evidence-bundle-v1` |
| `inspect_bafin_notice(namespace, notice_id)` | `market:bafin:read` | revisions, listing history and correction chain |
| `list_bafin_identity_candidates` / `propose_bafin_identity_matches` / `propose_bafin_identity_link` / `review_bafin_identity_match` / `revert_bafin_identity_match` | `market:bafin:read` + `knowledge:ownership:*` | reviewable identity in the shared ownership state machine |
| `project_bafin_voting_rights(namespace, ownership_namespace, issuers?)` | `market:bafin:read`, `knowledge:ownership:read`, `knowledge:ownership:write` | `voting_rights` control assertions per chain member and basis, citing the notice revision |
| `create_bafin_notice_monitor` / `run_bafin_notice_monitor` / `poll_bafin_notice_monitor` | `market:bafin:read` + subscriptions | watch an issuer, a holder, a manager within an issuer or the warning list; alerts carry receipts that cite the notice revision |

Every public entry point returns `not_ready` before any BaFin source has run.
Market as-of snapshots (`src/domains/market/asof.py`) accept an optional
`bafin_notices` selection and pin only the revisions published and acquired by
their cutoffs.

## Example journey (offline fixtures, fictional issuer)

```
bafin_holders_as_of(namespace="market-bafin", isin="DE000MSTR014", as_of="2026-03-04")  -> no holders
bafin_holders_as_of(..., as_of="2026-03-05")  -> Fiktiva Holding SE, 5.12 % (§ 39), chain of three
bafin_holders_as_of(..., as_of="2026-03-20")  -> the correction VR-2026-0007 (5.21 %) supersedes
bafin_net_short_positions(..., as_of="2026-05-01") -> Kurzfrist Capital LLP "below publication threshold or closed"
bafin_notice_dossier(..., as_of="2026-07-02", export_bundle=true) -> cited dossier and evidence bundle
```

Holdings across issuers (both fictional issuers in scope,
`tests/unit/bafin_harness.py: acquire_cross_issuer`):

```
bafin_holdings_of_holder(namespace="market-bafin", name="Fiktiva Holding SE", as_of="2026-05-01")
  -> DE000MSTR014: VR-2026-0007 (§ 33 5.21 %, § 39 5.21 %); DE000BSPL026: VR-2026-0002 (§ 33 3.01 %)
bafin_holdings_of_holder(..., as_of="2026-05-20")
  -> DE000BSPL026 moves to notified_below_lowest_threshold: VR-2026-0013 (2.4 % as stated)
bafin_holdings_of_holder(..., name="Erika Musterfrau", kind="natural_person", isin="DE000MSTR014", as_of="2026-05-20")
  -> DE000MSTR014 only; with NOESIS_PERSON_RESEARCH=1 the DE000BSPL026 person is an unjoined candidate
     until a reviewer accepts it, then both issuers are returned
```

Offline evidence: `tests/unit/domains/test_bafin_acceptance.py` and
`tests/unit/domains/test_bafin_holdings_of_holder.py`. Bounded live
evidence is still outstanding (#2132) and will be recorded under
`docs/development/bafin-notices-evidence/`.
