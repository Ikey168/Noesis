"""
LLM-assisted selector self-repair for drifted sources (#882).

When a site redesign breaks a source's CSS selectors, drift detection (#878)
flags it and the generic cascade (#877) keeps recovering content — but the
selector fix itself was a human reading HTML and editing ``config_async.json``.
This module automates the repair as an auditable pipeline:

1. :func:`propose_selectors` — prompt an **injectable** LLM callable
   (``Callable[[str], str]``, no default binding) with a reduced view of a
   fetched page and the target fields; parse a strict-JSON selector proposal.
2. :func:`validate_selectors` — apply each candidate with bs4 and score token
   containment against reference text (typically the generic cascade's
   extraction, used as ground truth). A candidate that grabs the wrong block
   (nav, boilerplate) scores low and is rejected.
3. :func:`repair_source` — propose + validate; returns accepted selectors and
   a per-field report. **Never writes config implicitly.**
4. :func:`apply_to_config` — the explicit, separate persistence step into the
   canonical sources config (#881).

The agent plane can drive this end-to-end: drift alert → sample page →
propose → validate → apply (or open a suggestion) where policy allows.
Fully offline-testable with a fake LLM callable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional

DEFAULT_FIELDS = ("title", "content", "author", "date")
REQUIRED_FIELDS = ("title", "content")   # repair is accepted only if these pass
MIN_OVERLAP = 0.5                        # token containment vs reference text
MIN_CONTENT_CHARS = 200                  # content candidate must extract this much
_MAX_PROMPT_HTML = 20_000                # chars of reduced HTML shown to the LLM

Llm = Callable[[str], str]


class SelectorRepairError(ValueError):
    """Raised when an LLM proposal cannot be parsed into selectors."""


@dataclass
class RepairResult:
    """Outcome of one repair attempt — nothing is persisted implicitly."""

    accepted: bool
    selectors: Dict[str, str] = field(default_factory=dict)   # validated only
    report: Dict[str, Any] = field(default_factory=dict)      # per-field detail


# --------------------------------------------------------------------------- #
# 1. Proposal
# --------------------------------------------------------------------------- #


def build_prompt(html: str, fields: Iterable[str] = DEFAULT_FIELDS) -> str:
    """Deterministic prompt: reduced page HTML + a strict-JSON instruction."""
    reduced = _reduce_html(html)[:_MAX_PROMPT_HTML]
    field_list = ", ".join(fields)
    return (
        "You are repairing CSS selectors for a news scraper. Below is the HTML "
        "of one article page from the source. Propose one CSS selector per "
        f"field ({field_list}) that extracts that field on this page and is "
        "likely to generalize across the site's articles (prefer stable "
        "attributes over auto-generated class names).\n\n"
        "Respond with ONLY a JSON object mapping field names to CSS selector "
        'strings, e.g. {"title": "h1.headline", "content": "article p"}. '
        "Omit fields you cannot locate. No prose, no code fences.\n\n"
        f"HTML:\n{reduced}"
    )


def propose_selectors(
    html: str,
    llm: Llm,
    fields: Iterable[str] = DEFAULT_FIELDS,
) -> Dict[str, str]:
    """Ask the LLM for candidate selectors; strict parse, tolerant recovery.

    Accepts a bare JSON object or one embedded in a chatty response; anything
    unparseable raises :class:`SelectorRepairError`. Only string values for
    requested fields survive.
    """
    response = llm(build_prompt(html, fields))
    payload = _parse_json_object(response)
    wanted = set(fields)
    return {
        k: v.strip()
        for k, v in payload.items()
        if k in wanted and isinstance(v, str) and v.strip()
    }


def _parse_json_object(response: str) -> Dict[str, Any]:
    try:
        obj = json.loads(response)
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    match = re.search(r"\{.*\}", response, flags=re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except ValueError:
            pass
    raise SelectorRepairError(
        "LLM response did not contain a JSON object of selectors"
    )


# --------------------------------------------------------------------------- #
# 2. Validation
# --------------------------------------------------------------------------- #


def validate_selectors(
    html: str,
    selectors: Dict[str, str],
    reference_text: Optional[str] = None,
    min_overlap: float = MIN_OVERLAP,
) -> Dict[str, Dict[str, Any]]:
    """Apply candidates with bs4 and judge them against the reference text.

    - ``content``: must extract >= MIN_CONTENT_CHARS and, when a reference is
      supplied, have token containment >= ``min_overlap`` within it — a
      candidate matching nav/boilerplate fails this.
    - other fields: must extract a non-empty value; when a reference exists,
      the value's tokens must appear in it (title pulled from the wrong
      widget fails).
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: Dict[str, Dict[str, Any]] = {}
    for fieldname, selector in selectors.items():
        entry: Dict[str, Any] = {"selector": selector, "ok": False}
        try:
            nodes = soup.select(selector)
        except Exception as exc:  # invalid CSS
            entry["error"] = f"invalid selector: {exc}"
            results[fieldname] = entry
            continue
        text = _normalize(" ".join(n.get_text(separator=" ") for n in nodes))
        entry["extracted_chars"] = len(text)
        if not text:
            entry["error"] = "selector matched nothing"
            results[fieldname] = entry
            continue
        overlap = _containment(text, reference_text) if reference_text else None
        entry["overlap"] = overlap
        if fieldname == "content":
            entry["ok"] = len(text) >= MIN_CONTENT_CHARS and (
                overlap is None or overlap >= min_overlap
            )
        else:
            entry["ok"] = overlap is None or overlap >= min_overlap
        results[fieldname] = entry
    return results


# --------------------------------------------------------------------------- #
# 3. Repair (propose + validate; no writes)
# --------------------------------------------------------------------------- #


def repair_source(
    source_cfg: Dict[str, Any],
    html: str,
    llm: Llm,
    reference_text: Optional[str] = None,
    fields: Iterable[str] = DEFAULT_FIELDS,
) -> RepairResult:
    """One auditable repair attempt for a drifted source.

    When no ``reference_text`` is given, the generic cascade (#877) extracts
    one from the page as ground truth. Accepted only when every field in
    ``REQUIRED_FIELDS`` validates; the returned selectors contain only the
    candidates that passed. Persisting is a separate explicit step
    (:func:`apply_to_config`).
    """
    if reference_text is None:
        from src.ingestion.extract import extract_article

        extracted = extract_article(html)
        reference_text = extracted.text if extracted else None

    try:
        candidates = propose_selectors(html, llm, fields)
    except SelectorRepairError as exc:
        return RepairResult(accepted=False, report={"error": str(exc)})

    validation = validate_selectors(html, candidates, reference_text)
    passing = {f: v["selector"] for f, v in validation.items() if v["ok"]}
    accepted = all(f in passing for f in REQUIRED_FIELDS)
    return RepairResult(
        accepted=accepted,
        selectors=passing,
        report={
            "source": source_cfg.get("name"),
            "fields": validation,
            "reference_chars": len(reference_text or ""),
        },
    )


# --------------------------------------------------------------------------- #
# 4. Explicit persistence into the canonical config (#881)
# --------------------------------------------------------------------------- #


def apply_to_config(path: str, source_name: str, selectors: Dict[str, str]) -> None:
    """Merge accepted selectors into ``sources[name].article_selectors``.

    Explicit and auditable by design — nothing else in this module writes.
    Raises ``KeyError`` when the source is not in the config.
    """
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    for entry in config.get("sources", []):
        if entry.get("name") == source_name:
            entry.setdefault("article_selectors", {}).update(selectors)
            break
    else:
        raise KeyError(f"source '{source_name}' not found in {path}")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _reduce_html(html: str) -> str:
    """Strip scripts/styles/svg and collapse whitespace for the prompt."""
    reduced = re.sub(
        r"<(script|style|svg|noscript)\b.*?</\1>", " ", html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    reduced = re.sub(r"<!--.*?-->", " ", reduced, flags=re.DOTALL)
    return re.sub(r"[ \t]+", " ", reduced)


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if len(t) >= 3}


def _containment(candidate: str, reference: str) -> float:
    """Fraction of candidate tokens that appear in the reference text."""
    cand = _tokens(candidate)
    if not cand:
        return 0.0
    return len(cand & _tokens(reference)) / len(cand)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
