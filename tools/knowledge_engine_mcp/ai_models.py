"""Technology AI models and datasets entry points (``technology.ai-models``, #2742): a model's or dataset's records as
of a date side by side, its revision and declared-licence history, reviewable cross-source identity, citation links to
Literature, dataset DOIs and OSS packages, evidence bundles and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``technology-ai-models``: sources ``huggingface-hub``,
``openml`` and ``epoch-ai``, each an optional default-off feature of the Technology bundle). Every answer cites its
source, record revision (Hub sha, OpenML id and version, or Epoch vintage) and as-of time, and is checked against the
AI01 minimisation decision before it leaves.

Exclusions (declared by every answering tool): no model weights or dataset files, no capability, safety, quality, risk
or openness verdict, no leaderboard or ranking, no download, like or trending counts, no licence-compliance
interpretation, no merging of self-reported card results with OpenML evaluations or Epoch estimates, and no inferred
training data, compute or parameters.
"""

READ = "knowledge:technical:ai-models:read"
WRITE = "knowledge:technical:ai-models:write"
REVIEW = "knowledge:technical:ai-models:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings "
    "or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results "
    "with OpenML evaluations or Epoch estimates."
)

AI_MODELS_WRITES = {
    "propose_ai_model_identity_matches",
    "review_ai_model_identity_match",
    "revert_ai_model_identity_match",
    "link_ai_model_records",
    "create_ai_models_monitor",
    "run_ai_models_monitor",
}
AI_MODELS_READS = {
    "ai_models_source_contracts",
    "ai_models_readiness",
    "list_ai_model_records",
    "ai_model_records_as_of",
    "ai_model_revision_history",
    "list_ai_model_identity_matches",
    "list_ai_model_links",
    "export_ai_model_evidence",
    "poll_ai_models_monitor",
}
AI_MODELS_TOOLS = AI_MODELS_WRITES | AI_MODELS_READS
AI_MODELS_SCOPES = {
    "ai_models_source_contracts": [],
    "ai_models_readiness": [READ],
    "list_ai_model_records": [READ],
    "ai_model_records_as_of": [READ],
    "ai_model_revision_history": [READ],
    "list_ai_model_identity_matches": [READ],
    "list_ai_model_links": [READ],
    "export_ai_model_evidence": [READ],
    "poll_ai_models_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_ai_model_identity_matches": [WRITE],
    "review_ai_model_identity_match": [REVIEW],
    "revert_ai_model_identity_match": [REVIEW],
    "link_ai_model_records": [WRITE],
    "create_ai_models_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_ai_models_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return AI_MODELS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.ai_models_records import AiModelsError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise AiModelsError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions declared and is checked against the AI01 minimisation decision."""
    from src.ingestion.ai_models_sources import EXCLUSIONS
    from src.kb.ai_models_records import (
        AiModelsError,
        excluded_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or excluded_paths(answer):
        raise AiModelsError("minimisation", "an answer would carry a person field, a popularity count, a card body "
                                            "or a verdict")
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = AI_MODELS_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def ai_models_source_contracts() -> dict:
        """Per-source access decisions (Hugging Face Hub metadata, OpenML, Epoch AI), keys, limits, terms, revision
        models, caps, bounded coverage and the AI01 minimisation decision; every source is unverified-live until a
        dated live run.
        Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings
        or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results
        with OpenML evaluations or Epoch estimates."""
        from src.ingestion.ai_models_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            DOCUMENTED_NOT_ACQUIRED,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "documented_not_acquired": DOCUMENTED_NOT_ACQUIRED,
                "minimisation": MINIMISATION, "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def ai_models_readiness() -> dict:
        """Which AI models features (Hub, OpenML, Epoch) are selected, the stores, per-source revisions and
        staleness."""
        from src.kb.ai_models_records import readiness

        return run_tool("ai_models_readiness", readiness)

    @mcp.tool()
    def list_ai_model_records(namespace: str, source: str | None = None, record_kind: str | None = None) -> dict:
        """AI model and dataset records (Hub repositories, OpenML datasets and tasks, Epoch rows) with their current
        revision and state."""
        from src.kb.ai_models_records import authorize
        from src.kb.ai_models_store import AiModelsStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"records": AiModelsStore(conn, initialize=False).records(namespace, source=source,
                                                                             record_kind=record_kind)}

        return run_tool("list_ai_model_records", op)

    @mcp.tool()
    def ai_model_records_as_of(namespace: str, subject: str | dict, as_of: str | int | None = None) -> dict:
        """A model's or dataset's records as of a date: each source's revision current at the date side by side
        (the subject and records an accepted identity match ties to it), self-reported results, OpenML evaluations
        and Epoch estimates each with who reported them; every item cites the Hub sha, the OpenML id and version, or
        the Epoch vintage, with its as-of time.
        Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings
        or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results
        with OpenML evaluations or Epoch estimates."""
        from src.kb.ai_models_queries import AiModelsQueries

        return run_tool("ai_model_records_as_of", lambda conn: AiModelsQueries(conn).records_as_of(
            namespace, subject, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def ai_model_revision_history(namespace: str, subject: str | dict) -> dict:
        """Every revision of a model or dataset with sha or version and time, the gated, disabled, deactivated,
        removed and renamed states as source-stated revisions, and declared-licence changes quoted as declared (SPDX
        only on an exact id match).
        Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings
        or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results
        with OpenML evaluations or Epoch estimates."""
        from src.kb.ai_models_queries import AiModelsQueries

        return run_tool("ai_model_revision_history", lambda conn: AiModelsQueries(conn).revision_history(
            namespace, subject, scopes=who()[1]))

    @mcp.tool()
    def list_ai_model_identity_matches(namespace: str, state: str | None = None,
                                       record_id: str | None = None) -> dict:
        """Cross-source identity matches (Epoch entry to Hub model, Hub dataset to OpenML dataset) with method,
        evidence, confidence and review history, and the records still unmatched; nothing is merged."""
        from src.kb.ai_models_identity import AiModelsIdentity

        def op(conn):
            identity = AiModelsIdentity(conn, initialize=False)
            return {"matches": identity.matches(namespace, scopes=who()[1], state=state, record_id=record_id),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return run_tool("list_ai_model_identity_matches", op)

    @mcp.tool()
    def list_ai_model_links(namespace: str, record_id: str | None = None, kind: str | None = None,
                            state: str | None = None) -> dict:
        """Links of records to papers, dataset DOIs and registry packages by stated identifier or accepted match,
        each pinning the record revision; absent providers and targets are reported."""
        from src.kb.ai_models_links import AiModelsLinks

        return run_tool("list_ai_model_links", lambda conn: {
            "links": AiModelsLinks(conn, initialize=False).links(namespace, scopes=who()[1], record_id=record_id,
                                                                 kind=kind, state=state)})

    @mcp.tool()
    def export_ai_model_evidence(namespace: str, subject: str | dict, as_of: str | int | None = None) -> dict:
        """An evidence bundle (noesis-evidence-bundle-v1) for a model's or dataset's records as of a date, citing
        every item with source, record revision and as-of time; offline evidence is an explicit omission.
        Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings
        or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results
        with OpenML evaluations or Epoch estimates."""
        from src.kb.ai_models_queries import AiModelsQueries

        def op(conn):
            queries = AiModelsQueries(conn)
            return queries.evidence_bundle(queries.records_as_of(namespace, subject, scopes=who()[1], as_of=as_of))

        return run_tool("export_ai_model_evidence", op)

    @mcp.tool()
    def poll_ai_models_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an AI models monitor's events (new revisions, licence changes, gating, removals, renames)."""
        from src.kb.ai_models_monitoring import AiModelsMonitor

        return run_tool("poll_ai_models_monitor", lambda conn: AiModelsMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def propose_ai_model_identity_matches(namespace: str) -> dict:
        """Propose Epoch-to-Hub-model and Hub-dataset-to-OpenML matches on stated identifiers only (repository id,
        OpenML id, then shared arXiv id or DOI); a shared name is never a match; nothing is used before review."""
        from src.kb.ai_models_identity import AiModelsIdentity

        return run_tool("propose_ai_model_identity_matches", lambda conn: AiModelsIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_ai_model_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed identity match with a reason (by a principal other than the proposer);
        recorded as an entity identity decision, never a merge."""
        from src.kb.ai_models_identity import AiModelsIdentity

        return run_tool("review_ai_model_identity_match", lambda conn: AiModelsIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_ai_model_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert a reviewed identity match; answers stop putting the records side by side."""
        from src.kb.ai_models_identity import AiModelsIdentity

        return run_tool("revert_ai_model_identity_match", lambda conn: AiModelsIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_ai_model_records(namespace: str, record_id: str | None = None) -> dict:
        """Link records to papers (paper connector, scholarly sources), dataset DOIs (DataCite) and registry
        packages by stated identifier or accepted match; library_name stays stated text; absent providers degrade to
        provider_absent.
        Exclusions: no weights or dataset files, no capability, safety, quality, risk or openness verdict, no rankings
        or download, like or trending counts, no licence-compliance interpretation, no merging of self-reported results
        with OpenML evaluations or Epoch estimates."""
        from src.kb.ai_models_links import AiModelsLinks

        return run_tool("link_ai_model_records", lambda conn: AiModelsLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1], record_id=record_id), write=True)

    @mcp.tool()
    def create_ai_models_monitor(namespace: str, request_key: str, target: dict,
                                 delivery: dict | None = None) -> dict:
        """Subscribe to a model or dataset (record id, repository id, OpenML dataset id or Epoch model) or a source:
        notices of new revisions, declared-licence changes, gating and removals (record changes, not assessments)."""
        from src.kb.ai_models_monitoring import AiModelsMonitor

        return run_tool("create_ai_models_monitor", lambda conn: AiModelsMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_ai_models_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an AI models monitor at a committed watermark; notices cite the revisions before and after."""
        from src.kb.ai_models_monitoring import AiModelsMonitor

        return run_tool("run_ai_models_monitor", lambda conn: AiModelsMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
