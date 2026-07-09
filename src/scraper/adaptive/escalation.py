"""
Dynamic HTTP→Playwright fetch escalation policy (#883).

``requires_js`` is a static per-source flag: when a site adds client-side
rendering, the plain-HTTP path starts returning shells, extraction yields
nothing, and the source silently goes dark until someone edits the config.

``FetchEscalationPolicy`` makes that adaptive. The engine consults it after
each HTTP pass:

- an empty HTTP pass ⇒ ``assess()`` says ``escalate`` and the engine retries
  the source through the Playwright path within the same run;
- ``promote_after`` *consecutive* escalations that succeed via JS sticky-
  promote the source to JS-first for the rest of the process (reported, so
  the canonical config #881 can be updated permanently);
- empty JS passes demote a promoted source again (no flapping in either
  direction — both transitions require consecutive evidence).

Pure logic, injectable-free, no async imports — fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

OK = "ok"
ESCALATE = "escalate"


@dataclass
class _SourceState:
    http_empty_streak: int = 0
    js_success_streak: int = 0
    js_empty_streak: int = 0
    promoted: bool = False
    escalations: int = 0


class FetchEscalationPolicy:
    """Decides when a source should be retried / promoted to the JS path."""

    def __init__(self, promote_after: int = 2, demote_after: int = 2):
        self.promote_after = promote_after
        self.demote_after = demote_after
        self._sources: Dict[str, _SourceState] = {}

    def _state(self, source_id: str) -> _SourceState:
        return self._sources.setdefault(source_id, _SourceState())

    # ------------------------------------------------------------------ #
    # Decisions
    # ------------------------------------------------------------------ #

    def should_use_js(self, source_id: str, configured_requires_js: bool) -> bool:
        """JS-first when configured OR sticky-promoted by observed behavior."""
        return configured_requires_js or self._state(source_id).promoted

    def assess(self, source_id: str, articles_found: int) -> str:
        """Judge an HTTP pass; ``escalate`` means retry via Playwright now."""
        state = self._state(source_id)
        if articles_found > 0:
            state.http_empty_streak = 0
            return OK
        state.http_empty_streak += 1
        state.escalations += 1
        return ESCALATE

    def record_js_result(self, source_id: str, articles_found: int) -> None:
        """Feed back what the JS path produced (escalated or JS-first runs)."""
        state = self._state(source_id)
        if articles_found > 0:
            state.js_empty_streak = 0
            state.js_success_streak += 1
            if not state.promoted and state.js_success_streak >= self.promote_after:
                state.promoted = True
        else:
            state.js_success_streak = 0
            state.js_empty_streak += 1
            if state.promoted and state.js_empty_streak >= self.demote_after:
                state.promoted = False

    # ------------------------------------------------------------------ #
    # Reporting
    # ------------------------------------------------------------------ #

    def promoted_sources(self) -> List[str]:
        """Sources that should have ``requires_js: true`` persisted (#881)."""
        return sorted(s for s, st in self._sources.items() if st.promoted)

    def report(self) -> Dict[str, Dict[str, Any]]:
        return {
            source_id: {
                "promoted": st.promoted,
                "escalations": st.escalations,
                "http_empty_streak": st.http_empty_streak,
                "js_success_streak": st.js_success_streak,
                "js_empty_streak": st.js_empty_streak,
            }
            for source_id, st in sorted(self._sources.items())
        }
