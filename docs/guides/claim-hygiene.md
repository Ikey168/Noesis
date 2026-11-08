# Claim hygiene: boilerplate, subject gate and stored Jev verdicts

Three independent clean-ups for the claim store and the claim link graph.
None of them deletes data: excluded claims stay in `argument_claims`, and
withheld links move to `claim_links_withheld`. Every operation records its
id and can be undone.

## 1. Boilerplate claims

A sentence-level claim detector also picks up page furniture. Two filters
remove it.

**At extraction.** `extract_claims` drops sentences that
`src.argument_mining.boilerplate.boilerplate_reason` recognises:

| Reason | Examples |
|---|---|
| `too_short` | Fewer than 4 words or 20 characters ("It was bad.", "5 min read") |
| `navigation` | "Skip to main content", "Next post: …", "Read more about …", "Related: …", breadcrumbs (`Home \| Blog \| Malware`) |
| `share` | "Share this post on LinkedIn", "Facebook Twitter LinkedIn Email" |
| `byline` | "By Jane Doe, Senior Researcher", "Posted by A. Kim on March 3", "Published on 2026-03-04", "Jane Doe is a senior threat researcher at Acme." |
| `marketing` | "Subscribe to our newsletter", "Book a demo", "All rights reserved", "Trusted by 150k+ orgs" |

All patterns except `too_short` apply only to sentences of at most 25
words. A real claim that mentions a newsletter or a share button is kept.
The following are kept too, and the tests check that they are:

- "By March, the botnet …"
- "By default, the package …"
- "Next, the loader …"

**Corpus-wide.** `src.kb.claim_exclusions.run_claim_exclusion_pass` marks
claims in `argument_claim_exclusions` with a reason and run id:

- the same furniture patterns, for claims stored before this filter existed;
- `repeated_on_site`: the same wording (case, accents and punctuation folded)
  in at least `min_site_documents` (default 3) distinct documents of one
  site. The site is `documents.source_id`, or the URL host when no source is
  recorded. The same sentence on two different sites is kept, because that
  is corroboration, not furniture.

The pass also mirrors each exclusion into `argument_claims.excluded_reason`,
so any reader can filter with `excluded_reason IS NULL`. The side table
stays authoritative, because it carries the run ids used for undo. The
claim linking pass, the cluster read view and the conflict graph skip
excluded claims. By default the pass also withholds existing links that
touch an excluded claim. The production workflow runs the pass after
materialising claims, and `kb_brief.py --harvest` runs it before linking,
so excluded claims cost neither NLI calls nor Jev questions.

```bash
python3 scripts/exclude_boilerplate_claims.py                 # plan only
python3 scripts/exclude_boilerplate_claims.py --apply
python3 scripts/exclude_boilerplate_claims.py --undo RUN_ID
python3 scripts/exclude_boilerplate_claims.py --restore CLAIM_ID --note "real claim"
```

`--restore` reinstates claims, records an override so later passes leave
them alone, and returns their withheld links when the other endpoint is not
excluded.

## 2. Subject gate for contradictions

Two install hooks from two different campaigns are not in conflict, however
similar the sentences are. `src.kb.claim_subjects.claim_subjects` recognises
what a claim is about:

- **indicators**:
  - file hashes (MD5, SHA-1, SHA-256);
  - hosts: domains, defanged forms (`evil[.]example[.]com`, `hxxp`) and IPv4;
  - file names (`update.exe`);
  - wallets (Bitcoin, Ethereum);
  - CVE ids;
  - packages (`name@1.2.3`, "package event-stream", "the `x` package",
    `npm install x`);
- **names**: malware families, campaigns and actors named next to a type word
  ("the Lumma stealer", "tracked as UNC5537", "dubbed GhostLoader");
- **entities**: other proper names. Ecosystems and platforms (npm, GitHub,
  Windows) and algorithm names are context, not subjects.
- A sentence-initial capitalised word ("Lumma does not …") is a *weak*
  entity: it can confirm a shared subject but never blocks a pair.

`subject_gate` decides each pair:

| Relation | Allowed when |
|---|---|
| `contradicts` | Both claims share a subject. Shared indicators are required when both name indicators. When neither names anything, `allow_unidentified` (default true) decides. |
| `supports`, `duplicate` | Not blocked, unless both claims name indicators and share none |

The claim linking pass applies the gate by default
(`run_claim_linking_pass(..., subject_gate=True, allow_unidentified_subjects=True)`).
A pair whose indicators differ is skipped before NLI runs, and a
contradiction without a shared subject is not proposed. Blocked pairs are
counted in `summary["subject_gate"]`. The conflict graph
(`detect_conflict(..., subject_gate=True)`) applies the same rule. Pass
`subject_gate=False` to restore the old behaviour.

## 3. Applying a run's stored Jev verdicts

A shadow-mode linking run writes every planned link and stores Jev's verdict
for each one. `src.kb.jev_verdicts.apply_jev_verdicts` then does what gate
mode would have done, using only those stored verdicts, with no new Jev
requests:

- a link Jev answered against is **withheld**, together with any
  `supersedes`/`corrects`/`retracts` transition the same run derived from
  it;
- a `supports` link Jev read the other way round (`b_supports_a`) is
  **reversed**: the original is withheld and the support is written from the
  other claim, with method suffix `+jev_reversed`.

```bash
python3 scripts/apply_jev_link_verdicts.py --run-id RUN_ID              # plan only
python3 scripts/apply_jev_link_verdicts.py --run-id RUN_ID --apply
python3 scripts/apply_jev_link_verdicts.py --undo OP_ID
python3 scripts/apply_jev_link_verdicts.py --keep-only --keep CLAIM_A CLAIM_B contradicts --note "sentence plus hashes"
```

Jev's verdicts stay in `claim_link_jev_assessments` unchanged, so its
mistakes remain visible. A `--keep` override:

- is stored in `claim_link_jev_overrides`;
- keeps the link when the verdicts are applied;
- restores the link if it was already withheld or reversed;
- makes later gate-mode runs write the link as planned, with action
  `override_written`.

Applying the verdicts also records a review state for every assessed link
in `claim_link_review_state`: `confirmed`, `reversed`, `withheld`,
`unreviewed` or `kept_override`. It then rebuilds clusters, because
withheld duplicates change them. Undo removes the states and rebuilds
clusters again.

Gate mode (`--jev-links gate`) now does the same automatically:
- rejected links are withheld;
- reversed supports are written the right way round (action `reversed`,
  counted in `summary["jev"]["reversed"]`);
- links with a stored override are written as planned.
