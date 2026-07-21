"""Interest profiles: the reader-owned configuration that drives a brief.

A profile is a small JSON document (see ``config/interest_profile.json``)
listing named topics and the keywords that select corpus documents for each.
Resolution order: an explicit path argument, the ``NOESIS_INTEREST_PROFILE``
environment variable (``NEURONEWS_INTEREST_PROFILE`` accepted as fallback),
the repo default at ``config/interest_profile.json``, and finally a built-in
starter profile so the brief always renders.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

_ENV_VARS = ("NOESIS_INTEREST_PROFILE", "NEURONEWS_INTEREST_PROFILE")


@dataclass
class InterestTopic:
    name: str
    keywords: List[str] = field(default_factory=list)


@dataclass
class InterestProfile:
    name: str = "default"
    window_hours: int = 24
    topics: List[InterestTopic] = field(default_factory=list)


# A usable starting point when no profile file exists anywhere.
_BUILTIN = {
    "name": "starter",
    "window_hours": 24,
    "topics": [
        {"name": "Economics & markets",
         "keywords": ["econom", "inflation", "interest rate", "central bank",
                      "market", "gdp", "recession"]},
        {"name": "Technology",
         "keywords": ["tech", "artificial intelligence", "software",
                      "semiconductor", "chip", "startup"]},
        {"name": "Web3",
         "keywords": ["web3", "crypto", "blockchain", "bitcoin", "ethereum",
                      "defi", "stablecoin", "token"]},
        {"name": "Local events",
         "keywords": ["local", "city council", "municipal", "community"]},
    ],
}


def _default_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "config" / "interest_profile.json"


def _parse(raw: dict) -> InterestProfile:
    topics = [
        InterestTopic(
            name=str(t.get("name") or "Untitled"),
            keywords=[str(k) for k in (t.get("keywords") or []) if str(k).strip()],
        )
        for t in (raw.get("topics") or [])
    ]
    topics = [t for t in topics if t.keywords]
    return InterestProfile(
        name=str(raw.get("name") or "default"),
        window_hours=int(raw.get("window_hours") or 24),
        topics=topics,
    )


def load_profile(path: Optional[str] = None) -> InterestProfile:
    """Load an interest profile, falling back to the built-in starter set."""
    candidates: List[Path] = []
    if path:
        candidates.append(Path(path))
    for var in _ENV_VARS:
        env = os.environ.get(var)
        if env:
            candidates.append(Path(env))
    candidates.append(_default_path())

    for candidate in candidates:
        try:
            raw = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        profile = _parse(raw)
        if profile.topics:
            return profile
    return _parse(_BUILTIN)
