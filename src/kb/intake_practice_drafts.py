"""Reviewable practice drafts from current, owner-visible intake source revisions."""

from __future__ import annotations

from typing import Any

from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _reference, _text
from src.kb.intake_practice import IntakePracticeStore
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.research_projects import ResearchProjectError

CONTRACT = "noesis-intake-practice-draft-v1"
_SOURCES = {
    "exploration_source": (
        "intake_exploration_sources", "intake_exploration_source_revisions",
        "source_id", "version",
    ),
    "intake_feed_item": (
        "intake_inbox_items", "intake_inbox_item_revisions",
        "item_id", "source_version",
    ),
}
_KINDS = {"recall", "procedure", "explanation"}
_BUNDLE_ITEMS = {"concept", "evidence_card"}


class IntakePracticeDrafts:
    def __init__(self, conn: Any):
        self.conn = conn

    def build(
        self, namespace: str, selections: list[dict], *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        IntakeStore._authorize(
            {"namespace": namespace, "owner": principal_id}, principal_id, scopes,
        )
        if not isinstance(selections, list) or not 1 <= len(selections) <= 20:
            raise IntakeError("invalid_selection", "select one to 20 current sources or research-bundle items")
        cards = []
        for raw in selections:
            if not isinstance(raw, dict) or set(raw) - {
                "reference", "practice_kind", "item_kind", "item_id",
            } or not {"reference", "practice_kind"} <= set(raw):
                raise IntakeError("invalid_selection", "selection needs reference and practice_kind")
            ref = _reference(raw["reference"], namespace, scopes)
            kind = raw["practice_kind"]
            if kind not in _KINDS:
                raise IntakeError("invalid_practice", "unsupported practice kind")
            answer_reference = ref
            if ref["kind"] in _SOURCES:
                if set(raw) != {"reference", "practice_kind"} or ref["namespace"] != namespace:
                    raise IntakeError("unsupported_source", "intake source selections are limited to this namespace")
                table, revision_table, identity, version = _SOURCES[ref["kind"]]
                exists = self.conn.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
                    [table],
                ).fetchone()
                revision_exists = self.conn.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
                    [revision_table],
                ).fetchone()
                if not exists or not revision_exists:
                    raise IntakeError("source_unavailable", "source snapshot is unavailable")
                row = self.conn.execute(
                    f"SELECT title,{version} FROM {table} WHERE namespace=? AND owner=? AND {identity}=?",
                    [namespace, principal_id, ref["id"]],
                ).fetchone()
                if not row or int(row[1]) != ref["version"]:
                    raise IntakeError("source_superseded", "inspect the current source revision before drafting")
                exact = self.conn.execute(
                    f"SELECT 1 FROM {revision_table} WHERE namespace=? AND owner=? "
                    f"AND {identity}=? AND {version}=?",
                    [namespace, principal_id, ref["id"], ref["version"]],
                ).fetchone()
                if not exact:
                    raise IntakeError("source_unavailable", "pinned source revision is unavailable")
                source_title = _text(row[0], "source title", limit=1000)
                prompt = {
                    "recall": f"Without looking it up, recall the key point in {source_title}.",
                    "procedure": f"Without looking it up, explain how to apply the procedure in {source_title}.",
                    "explanation": f"Without looking it up, explain {source_title} to someone else.",
                }[kind]
            elif ref["kind"] == "research_bundle":
                if set(raw) != {"reference", "practice_kind", "item_kind", "item_id"}:
                    raise IntakeError("invalid_selection", "research-bundle selection needs item_kind and item_id")
                item_kind = raw["item_kind"]
                if item_kind not in _BUNDLE_ITEMS:
                    raise IntakeError("unsupported_source", "select a concept or Evidence Card from a Research Bundle")
                item_id = _text(raw["item_id"], "item_id", limit=128)
                item = self._current_bundle_item(
                    ref, item_kind, item_id, principal_id=principal_id, scopes=scopes,
                )
                answer_reference = {**ref, "locator": {"section": f"{item_kind}s/{item_id}"}}
                if item_kind == "concept":
                    prompt = f"Without looking it up, explain the concept {item['name']} and how it applies."
                else:
                    prompt = f"Without looking it up, explain what evidence card {item_id} supports or challenges."
            else:
                raise IntakeError("unsupported_source", "select an intake source or a Research Bundle concept/Evidence Card")
            cards.append({
                "kind": kind, "proposed_prompt": prompt,
                "answer_reference": answer_reference,
                "mastery_criterion": "Author defines a checkable unaided response",
                "answer_status": "pending_author_review",
                "requires_author_answer": True,
            })
        result = {"contract": CONTRACT, "namespace": namespace,
                  "owner": principal_id, "cards": cards,
                  "limitations": ["Prompt cues do not supply an answer; every answer and mastery criterion needs author review"]}
        return {**result, "sha256": _hash(result)}

    def _current_bundle_item(
        self, ref: dict, item_kind: str, item_id: str, *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        store = IntakeResearchBundleStore(self.conn, initialize=False)
        try:
            bundle = store.inspect(
                ref["namespace"], ref["id"], principal_id=principal_id,
                scopes=scopes,
            )
        except (IntakeError, ResearchProjectError) as exc:
            raise IntakeError("source_unavailable", "current Research Bundle access is required") from exc
        if bundle["revision"] != ref["version"]:
            raise IntakeError("source_superseded", "inspect the current Research Bundle revision before drafting")
        project = store._project(
            ref["namespace"], bundle["project_id"], principal_id, scopes,
        )
        if (bundle["project_revision"] != project["revision"]
                or bundle["question_revision"] != project["question_revision"]):
            raise IntakeError("source_superseded", "Research Bundle no longer matches its project")
        section = "concepts" if item_kind == "concept" else "cards"
        item = next((value for value in bundle["document"][section]
                     if value["id"] == item_id), None)
        if item is None:
            raise IntakeError("source_unavailable", "selected concept or Evidence Card is not in this bundle")
        cited = item.get("card_ids", []) if item_kind == "concept" else [item_id]
        source_status = bundle["checks"].get("source_status", {})
        if any(source_status.get(card_id) == "superseded" for card_id in cited):
            raise IntakeError("source_superseded", "selected evidence includes a corrected source")
        if any(source_status.get(card_id) != "current" for card_id in cited):
            raise IntakeError("source_unavailable", "selected evidence source is unavailable")
        return item

    def create_pack(
        self, namespace: str, request_key: str, title: str,
        selections: list[dict], draft_sha256: str, reviewed_cards: list[dict], *,
        principal_id: str, scopes: set[str], interval_days: list[int] | None = None,
    ) -> dict:
        draft = self.build(namespace, selections, principal_id=principal_id, scopes=scopes)
        if draft["sha256"] != draft_sha256:
            raise IntakeError("draft_conflict", "source or draft changed; rebuild and review the pack")
        if not isinstance(reviewed_cards, list) or len(reviewed_cards) != len(draft["cards"]):
            raise IntakeError("invalid_practice", "review every proposed card")
        cards = []
        for proposal, reviewed in zip(draft["cards"], reviewed_cards, strict=True):
            if not isinstance(reviewed, dict) or set(reviewed) != {
                "prompt", "answer", "mastery_criterion", "approved"
            } or reviewed["approved"] is not True:
                raise IntakeError("review_required", "approve a prompt, answer, and criterion for each card")
            cards.append({
                "kind": proposal["kind"],
                "prompt": _text(reviewed["prompt"], "prompt", limit=2000),
                "answer": _text(reviewed["answer"], "answer", limit=5000),
                "mastery_criterion": _text(reviewed["mastery_criterion"], "criterion", limit=1000),
                "references": [proposal["answer_reference"]],
            })
        return IntakePracticeStore(self.conn).create_pack(
            namespace, request_key, title, cards,
            principal_id=principal_id, scopes=scopes, interval_days=interval_days,
        )
