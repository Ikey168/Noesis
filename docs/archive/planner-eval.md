# Planner evaluation harness (M5)

The generative-UI planner (`src/genui/planner.py`) maps a user intent to a set of
facets and a canvas of panels. M5 makes that mapping measurable instead of
asserted.

## Golden fixture set (M5.1)

`tests/data/planner_golden.json` is the labeled ground truth: a set of
representative intents, each mapped to

- `facets`: the facets the planner should select (the primary one must appear in
  the planner's top facets), and
- `must_panels`: the minimal set of panels that must appear in the generated
  canvas (the recall target).

Every facet in `FACET_KEYWORDS` is covered by at least one intent. The fixtures
are validated by `tests/unit/genui/test_planner_golden.py`, which also asserts
the shipped heuristic planner satisfies them (the baseline).

The scorer (M5.2) measures panel-selection precision and recall against this set
across the heuristic and LLM planners, and CI (M5.3) fails the build on a
regression below threshold.
