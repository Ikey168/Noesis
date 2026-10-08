---
name: research-gaps
description: Find and justify research gaps for a Noesis paper from the evidence instead of asserting them — register explicit support thresholds (primary, independent, current, method-adequate sources) and ranking weights, record coverage per claim, population, time range, geography and methodology, discover shortfall, contradiction, citation-chain, retraction and unknown-coverage gaps, explain each with its evidence, and turn the confirmed ones into the paper's "gap" statements and future-work agenda. Use when writing an introduction's motivation, a review's discussion or research agenda, or a grant rationale, e.g. on AI in education. Drives the noesis-knowledge-engine MCP research-gap tools.
---

# Research gaps

"Little is known about X" is a claim, and reviewers check it. In Noesis a gap is
a record with a type, thresholds, the evidence that falls short, and a status.
Tools are on the **`noesis-knowledge-engine`** MCP server; run them in the
**paper-project** namespace after the corpus exists (**science-overview**,
or **systematic-review** through **screening-extraction**).

## 1. Fix the policy before looking

`register_research_gap_policy(namespace, semantic_version="1.0.0", thresholds={...}, weights={...})`

| Threshold (default) | Meaning for a claim to count as supported |
|---|---|
| `min_primary` (1) | primary studies, not reviews or commentary |
| `min_independent` (2) | distinct independence groups (different teams/datasets) |
| `min_current` (1) | sources current for the question |
| `min_method_adequate` (1) | sources whose design can answer it |

Ranking weights (defaults): `decision_relevance` 0.3, `uncertainty_reduction`
0.25, `feasibility` 0.2, `freshness` 0.15, `policy_priority` 0.1, `cost` 0.1.

Agree the thresholds with the user and write them in the methods: they define
what "gap" means in the paper. For an effect claim in AI-in-education research,
a defensible setting is `min_independent: 2` and `min_method_adequate: 1`, where
*method-adequate* means a controlled comparison with a measured learning
outcome, and *current* means post-November 2022 for generative-AI questions.
Policies are immutable; change them with a new version (`supersedes_policy_id`),
never by editing.

## 2. Record coverage

For each object the paper cares about, `record_research_coverage(namespace,
object_kind, object_id, dimension={...}, coverage_known=True, supports=[...])`.

- `object_kind`: `claim`, `entity`, `event`, `time-range`, `geography`,
  `methodology`.
- `dimension`: the slice, e.g. `{"isced": "1", "subject": "mathematics",
  "country": "low-income"}`. One record per slice you will argue about.
- `supports`: one entry per piece of evidence, each with `evidence_id`, and
  where known `source_id`, `primary`, `independence_group`, `current`,
  `method_adequate`, `accessible`, `retracted`, `stance`
  (`supports`/`contradicts`) and `cites_source_ids`. Set these from the
  extracted study fields (design, date, team), not by impression.
- `coverage_known=False` when you did not look: unknown is different from
  absent, and the paper must not present it as a gap.

Useful slices for AI in education: education level × subject, country income
group, learner population (students with disabilities, multilingual learners),
outcome type (learning vs. perception), duration (single session vs. a term or
longer), and design (controlled vs. pre/post).

## 3. Discover and explain

- `discover_research_gaps(namespace, policy_version="1.0.0")` evaluates every
  coverage record against the policy. Gap types:

  | `gap_type` | Meaning |
  |---|---|
  | `missing-primary` | fewer primary studies than `min_primary` |
  | `insufficient-independent-support` | fewer independence groups than `min_independent` |
  | `missing-current-support` | fewer current sources than `min_current` |
  | `methodologically-inadequate` | fewer method-adequate sources than `min_method_adequate` |
  | `unresolved-contradiction` | supporting and contradicting evidence both present |
  | `missing-original-source` / `circular-citation` | findings that trace back to a source not in the corpus, or cite each other in a loop |
  | `retracted-support` | support includes a retracted work (see **reference-integrity**) |
  | `inaccessible-evidence` | support that cannot currently be read |
  | `unknown-coverage` | `coverage_known=False`: nobody looked; a gap in the corpus, not the field |

- `list_research_gaps(namespace, status="open", gap_type=...)` and
  `explain_research_gap(namespace, gap_id)` show which threshold failed, by how
  much, and with which evidence.
- `update_research_gap_status(namespace, gap_id, status="in-progress"|"resolved"|"dismissed",
  reason=..., evidence=[...])` once checked; e.g. dismiss a coverage gap that a
  targeted search closes, with that search as evidence.
- `prioritize_research_gaps(namespace, budget, max_tasks)` ranks open gaps for
  the future-work section.

Before calling a gap real, run one targeted search for it (**science-overview**,
step 2). A gap that disappears after one search was a gap in the corpus, not in
the field. Report it as a limitation, if anything.

## 4. Write it into the paper

Each gap statement in **paper-draft** is a `sourced` assertion with an
`artifact` dependency on the gap record (id and revision from
`get_research_gap`), plus `source` dependencies for the studies it counts.
Use the threshold language:

> "Of the controlled studies of generative-AI tutoring identified, none
> examined learners in ISCED 1 for longer than one term (threshold: at least
> two independent method-adequate studies; found: 0)."

Not: "There is a lack of research on young learners."

Use `unresolved-contradiction` gaps for the discussion (both sides cited),
`missing-original-source` and `circular-citation` gaps to warn where a widely
repeated figure cannot be traced to a primary study, and the prioritized list
for the research agenda. `unknown-coverage`, `inaccessible-evidence` and
`retracted-support` gaps belong in the limitations, not the gap statements.

## Rules

- Gaps are relative to the searched corpus and the stated policy; say both.
- Unknown coverage is never reported as absence.
- Do not lower thresholds after seeing the results to manufacture or erase
  gaps; a new policy version with a reason is visible in the record.
