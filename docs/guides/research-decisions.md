# Research decisions

`create_research_decision` stores the context of an actual choice. Its `content`
can contain a pinned project (`id`, `namespace`, `revision`) or an explicit
`null` project for a standalone choice, two or more options
(`id`, `description`), constraints, assumptions, revisioned evidence observations,
subjective preferences, the selected option ID, a rationale, and review conditions.
Constraints, assumptions, preferences, and review conditions are explicit text
lists. Observations use the project's revisioned evidence-reference format.
For a standalone choice, `decision_context` is required. It records the question,
stakes, required confidence, stop condition, uncertainty, missing inputs, and an
optional deadline timestamp (Unix milliseconds or `null`). A two-option yes/no
choice can therefore close without opening a research project. The stored
`noesis-decision-v2` record is a finalized user choice; it does not assert that
missing evidence was acquired. Project-pinned records without this context retain
the `noesis-decision-v1` contract. A decision with a declared evidence budget
uses `noesis-decision-v3`.

Bounded collection is opt-in through an `evidence_budget` with `max_items`
(1–50), declared relevance `criteria`, and a `stop_condition`. Each existing
observation must have one matching `evidence_assessments` entry before a budgeted
decision is saved. `record_decision_evidence` appends one revision-pinned evidence
reference and records the criterion, supports/contradicts/context/irrelevant
assessment, and rationale. It requires the current decision revision and a
unique command key; a replay returns the stored receipt. The cap is enforced
before another item can be added. This records an author's relevance assessment;
it does not independently establish that evidence is correct or that the choice
is well supported.

```json
{
  "project": null,
  "decision_context": {
    "question": "Renew the service?",
    "stakes": "One month of cost",
    "required_confidence": "Moderate",
    "stop_condition": "Current usage is known",
    "uncertainty": "Future usage is unknown",
    "missing_inputs": ["Future usage"],
    "deadline_at_ms": null
  },
  "evidence_budget": {
    "max_items": 5,
    "criteria": ["current usage", "renewal cost"],
    "stop_condition": "Stop when each declared criterion has one current source check"
  },
  "evidence_assessments": [],
  "options": [{"id": "yes", "description": "Renew"}, {"id": "no", "description": "Cancel"}],
  "constraints": ["Within budget"],
  "assumptions": [],
  "observations": [],
  "preferences": ["Avoid unused subscriptions"],
  "selected_action": "no",
  "rationale": "There is no current use",
  "review_conditions": ["Reconsider if use resumes"]
}
```

The server records the decision author and time. Replaying the creation key does
not overwrite an existing decision.

`inspect_research_decision` reopens current or historical revisions.
`revise_research_decision` requires the current revision and full new context;
previous actions, preferences, and project baselines remain intact. Calls require
current decision read/write scope, ownership, and namespace access. A linked
project also requires current project read access. Revocation applies to
historical decisions too. Review conditions are
recorded declarations. `create_decision_condition_watch` can pin an exact source
revision, assumption dependency, or quantitative threshold to a standalone
decision, subject to current evidence access. A changed source creates a durable
review task; it does not silently change the user's chosen option.

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

A Decision Support intake session can cite a standalone decision with
`{"kind":"decision","id":"decision:<id>","namespace":"<namespace>","version":1}`.
Start the session with `start_intake_mode`, record `selected_option` and `rationale`
through `command_intake_mode`, and complete it independently of an open research
session. A Modulo caller can add its versioned artifact link and an Exploration or
Deep Research `origin`. The deterministic handoff fixture verifies command replay,
history, and current-owner access; it does not establish user decision quality.
