# Bounded persistent research loops

`create_persistent_research_loop` binds existing prioritized research-gap tasks to persisted feasible source plans and a project's revisioned questions. Each binding compiles a pinned acquire → derive → query recipe. `run_persistent_research_loop` executes those recipes through the source planner, production text extraction, semantic maintenance, and gap reassessment. Prepared fixture outputs cannot complete a production loop.

Create a project, prioritize its gaps, and persist feasible source plans first. Example creation arguments (replace the IDs with actual objects):

```json
{
  "namespace": "projects", "project_id": "project:…", "request_key": "cycle-1",
  "bindings": [{
    "gap_namespace": "scientific", "gap_task_id": "gap-task:…",
    "plan_namespace": "scientific", "plan_id": "source-plan:…",
    "question_index": 0, "domain": "scientific",
    "cost_ceiling": {"requests": 2}, "minimum_semantic_score": 0.3
  }],
  "limits": {
    "max_iterations": 2, "max_results": 10, "max_retries": 1,
    "timeout_ms": 180000, "independent_sources_per_domain": 1
  }
}
```

The runtime pins plan, gap, question, recipe, implementation, model and package identities. Changed inputs require a new loop; an identical request key reopens the existing loop. The first runtime supports source plans declaring zero marginal cost and cached local ClaimBuster/all-MiniLM models. It rejects priced source plans until a provider-specific metered cost guard is available. There is no implicit model download or external paid inference. Source packs must be configured and enabled, and their licenses must permit the requested non-redistributing acquisition. Credentials are resolved only from explicitly authorized `NOESIS_*` environment references.

Each action reserves its whole declared ceiling from the shared project budget before acquisition. Request ceilings must cover selected/fallback pages and retries. Completed actions conservatively charge that ceiling, rather than claiming a measured provider bill. Unknown usage on an interrupted action stays held. Settlement, completed-recipe replay, and project run links are idempotent. Retrying never resets the original deadline, consumed results, iterations, or attempt limit.

Use `inspect_persistent_research_loop` to inspect state, `cancel_persistent_research_loop` to request cancellation, and `resume_persistent_research_loop` for an eligible blocked/cancelled run. A completed or stopped loop requires a new request to perform new work. Project pause or question revision changes stop existing work. Reads and writes enforce current ownership and namespace/domain access; execution also needs `knowledge:projects:execute` plus the actual recipe/source/gap operation scopes (or operator access).

The public run call requires a file-backed warehouse and admits at most two background workers. `wait_ms` is bounded to 60 seconds and clipped to the persisted deadline, which starts at admission. Backpressure returns immediately. Cancellation is checked between provider operations and before coverage publication. A noncooperative provider retains its bounded worker slot and reservation until it returns; returning from the public call does not assert that such a provider has been killed.

Coverage includes only matching revisions acquired by the action. Every iteration rechecks cumulative evidence against the latest committed canonical lifecycle, excluding corrected or withdrawn revisions. Duplicate content does not count as another source. Distinct source IDs are a declared independence proxy, not verified editorial independence. Reaching the configured per-domain threshold is a workflow stop condition, not a scientific conclusion or entailment judgment. Gap observations explicitly leave primary-source and method adequacy unverified, so a quality gap may remain open. No new independent evidence, exhausted selected actions, limits, and unavailable providers have explicit stop/block reasons.

## Europe PMC acquisition

The adapter requests core JSON records, maps individual article identities and abstract text, retains the original fields, and uses native cursor pagination within page/result/response-byte limits. Missing abstracts are marked title-only. A retraction notice is distinguished from an article explicitly typed as retracted. See the [Europe PMC REST documentation](https://europepmc.org/RestfulWebService) and [service reference](https://europepmc.org/docs/EBI_Europe_PMC_Web_Service_Reference.pdf).

## Validation

The unit fixtures exercise accounting, replay, retraction, deadlines and provider failure; they do not establish live research quality. The opt-in test uses two public Europe PMC article abstracts, the actual cached extraction and embedding models, and two domains. It also verifies that replay performs no additional source run:

```sh
NOESIS_LIVE_RESEARCH=1 NOESIS_RESEARCH_LOOP_EVIDENCE_PATH=docs/development/workflow-implementation-evidence/research-loop-e2e.json .venv/bin/python -m pytest tests/integration/kb/test_research_loop_live.py -q
```

The evidence JSON records the actual configuration and observed workflow results. Independent human quality validation remains separate.

Local research workers configure a process-wide PyTorch intra-operation thread budget once, defaulting to two threads (`NOESIS_MODEL_THREADS`, permitted range 1–8). The value is part of the pinned runtime identity. Changing it requires a worker restart; an active worker does not silently change another run's resource configuration. This avoids severe oversubscription on high-core-count hosts. The budget is shared by PyTorch models in that process, so configure it before serving other model workloads.

The final live cycle also passed after canonical source-revision validation was tightened. The validation directory retains an upstream-acquisition failure and an extraction-budget stop alongside the successful run. Transport timeouts/availability failures now retain retryable codes, and HTTP error status/retry headers are preserved without copying upstream error bodies. A failed run never becomes successful through fixture substitution.
