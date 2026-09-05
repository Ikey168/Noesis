# Research decisions

`create_research_decision` stores the context of an actual choice. Its `content`
contains a pinned project (`id`, `namespace`, `revision`), two or more options
(`id`, `description`), constraints, assumptions, revisioned evidence observations,
subjective preferences, the selected option ID, a rationale, and review conditions.
Constraints, assumptions, preferences, and review conditions are explicit text
lists. Observations use the project's revisioned evidence-reference format.
The server records the decision author and time. Replaying the creation key does
not overwrite an existing decision.

`inspect_research_decision` reopens current or historical revisions.
`revise_research_decision` requires the current revision and full new context;
previous actions, preferences, and project baselines remain intact. Calls require
current decision read/write scope, ownership, namespace access, and project read
access. Revocation applies to historical decisions too. Review conditions are
recorded declarations; automatic alert routing is a separate integration.

`calculate_decision_sensitivity` accepts a decision revision, nonnegative criterion
weights, an input utility map for every option, up to 100 weight scenarios, and
input provenance. Example:

```json
{
  "weights": {"cost": 1, "quality": 1},
  "inputs": {"a": {"cost": 1, "quality": 0}, "b": {"cost": 0, "quality": 1}},
  "scenarios": [{"assumption": "Cost matters twice as much", "weights": {"cost": 2}}],
  "provenance": "Author-supplied utilities normalized to 0–1; larger is preferred"
}
```

The baseline ties A and B; the scenario puts A first. Results retain tied groups,
missing inputs, changed orderings, the pinned decision hash, formula version,
decimal precision, and all declared inputs. Each calculation has an idempotent
stored receipt. Missing positive-weight inputs leave an option unranked; they do
not become zero. Inputs must already have comparable scales and direction.
This is a weighted utility calculation, not a causal model or an automatic choice.
The selected action remains the author's decision.
