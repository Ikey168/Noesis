"""Adaptive scraping behaviors (#883, #884, #882).

Pure-logic policies (no aiohttp/playwright imports) that the async engine
wires in thinly, so every decision here is offline-testable in the CI gate:

- :mod:`.escalation` — dynamic HTTP→Playwright fetch escalation (#883)
- :mod:`.rate`       — AIMD per-source adaptive rate limiting (#884)
- :mod:`.selector_repair` — LLM-assisted selector self-repair (#882)
"""
