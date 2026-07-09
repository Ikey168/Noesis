"""
Shared normalization for dataset connectors: units, frequency, and geography.

Provider modules stay thin by delegating to these helpers so a claim can be
resolved against a series regardless of which provider harvested it (e.g. a
claim about "Germany" matches whether the series codes it ``DE`` or ``DEU``).
"""

from __future__ import annotations

from typing import Optional

from services.ingest.common.series_model import FREQUENCIES

# Provider frequency tokens -> the dataset-series-v1 enum.
_FREQUENCY_ALIASES = {
    "a": "annual",
    "annual": "annual",
    "year": "annual",
    "yearly": "annual",
    "q": "quarterly",
    "quarter": "quarterly",
    "quarterly": "quarterly",
    "m": "monthly",
    "month": "monthly",
    "monthly": "monthly",
    "w": "weekly",
    "week": "weekly",
    "weekly": "weekly",
    "d": "daily",
    "day": "daily",
    "daily": "daily",
}

# Common unit strings -> a small normalized vocabulary. Unknown units pass
# through unchanged (lower-cased) so nothing is silently dropped.
_UNIT_ALIASES = {
    "%": "percent",
    "percent": "percent",
    "pct": "percent",
    "percentage": "percent",
    "usd": "usd",
    "us dollar": "usd",
    "us dollars": "usd",
    "$": "usd",
    "index": "index",
    "count": "count",
    "number": "count",
    "persons": "count",
    "people": "count",
}

# ISO 3166 alpha-3 -> alpha-2 for the codes that appear in the first providers.
# Extended as providers are added; unknown codes pass through unchanged.
_ISO3_TO_ISO2 = {
    "deu": "DE", "usa": "US", "gbr": "GB", "fra": "FR", "ita": "IT",
    "esp": "ES", "nld": "NL", "bel": "BE", "che": "CH", "aut": "AT",
    "swe": "SE", "nor": "NO", "dnk": "DK", "fin": "FI", "prt": "PT",
    "irl": "IE", "pol": "PL", "grc": "GR", "can": "CA", "jpn": "JP",
    "chn": "CN", "ind": "IN", "bra": "BR", "aus": "AU", "rus": "RU",
}


def normalize_frequency(raw: Optional[str]) -> str:
    """Map a provider frequency token to the contract enum; default 'irregular'."""
    if not raw:
        return "irregular"
    token = str(raw).strip().lower()
    if token in FREQUENCIES:
        return token
    return _FREQUENCY_ALIASES.get(token, "irregular")


def normalize_unit(raw: Optional[str]) -> Optional[str]:
    """Normalize a unit string to the small vocabulary; unknown units lower-case
    through unchanged. Returns None for an empty/None input."""
    if raw is None:
        return None
    token = str(raw).strip()
    if not token:
        return None
    return _UNIT_ALIASES.get(token.lower(), token.lower())


def normalize_geography(raw: Optional[str]) -> Optional[str]:
    """Normalize a geography code to ISO 3166 alpha-2 when recognized.

    Accepts alpha-2 (returned upper-cased) or alpha-3 (mapped when known);
    unrecognized codes pass through upper-cased so provider region codes (e.g.
    Eurostat NUTS) are preserved rather than dropped."""
    if raw is None:
        return None
    token = str(raw).strip()
    if not token:
        return None
    lowered = token.lower()
    if len(token) == 3 and lowered in _ISO3_TO_ISO2:
        return _ISO3_TO_ISO2[lowered]
    return token.upper()
