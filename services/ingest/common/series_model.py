"""
Statistical-series data model for the beyond-text expansion (Track A).

A ``SeriesRecord`` is a statistical time series harvested from an official
provider (World Bank, FRED, Eurostat, ...). Series are *evidence* for checking
quantitative claims; they are deliberately **not** ``Document`` records, because
they are versioned, revisable, and numeric rather than textual. The contract is
``dataset-series-v1`` (``contracts/schemas/jsonschema/dataset-series-v1.json``).

Observations are vintaged by ``as_of`` so a claim check records which revision
it ran against and stays replayable.

See ``docs/architecture/EVIDENCE_DATASETS_PLAN.md``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from numbers import Real
from typing import Any, Dict, List, Optional

# Mirrored from the dataset-series-v1 contract enum.
FREQUENCIES = ("annual", "quarterly", "monthly", "weekly", "daily", "irregular")


@dataclass
class Observation:
    """One observed value at a period. ``value`` is None for a reported gap."""

    period: str
    value: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {"period": self.period, "value": self.value}


@dataclass
class SeriesRecord:
    """A statistical time series (dataset-series-v1)."""

    series_id: str
    provider: str
    title: str
    frequency: str
    as_of: int  # milliseconds since epoch (provider vintage)
    observations: List[Observation] = field(default_factory=list)
    unit: Optional[str] = None
    geography: Optional[str] = None
    license: Optional[str] = None
    source_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.series_id:
            raise ValueError("series_id must be a non-empty string")
        if not self.provider:
            raise ValueError("provider must be a non-empty string")
        if not self.title:
            raise ValueError("title must be a non-empty string")
        if self.frequency not in FREQUENCIES:
            raise ValueError(
                f"Invalid frequency {self.frequency!r}; expected one of {FREQUENCIES}"
            )
        if not isinstance(self.as_of, int) or isinstance(self.as_of, bool) or self.as_of < 0:
            raise ValueError("as_of must be a non-negative integer (ms since epoch)")
        # Coerce mapping observations (e.g. from_dict of a raw payload) to objects.
        coerced: List[Observation] = []
        for obs in self.observations:
            if isinstance(obs, Observation):
                coerced.append(obs)
            elif isinstance(obs, dict):
                coerced.append(Observation(period=obs["period"], value=obs.get("value")))
            else:
                raise ValueError(f"observation must be an Observation or dict, got {type(obs)!r}")
            if not coerced[-1].period:
                raise ValueError("observation.period must be a non-empty string")
            val = coerced[-1].value
            if val is not None and not isinstance(val, Real):
                raise ValueError("observation.value must be a number or null")
        self.observations = coerced

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dataset-series-v1 payload dict."""
        payload = asdict(self)
        payload["observations"] = [o.to_dict() for o in self.observations]
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "SeriesRecord":
        """Build a SeriesRecord from a dataset-series-v1 payload dict."""
        known = {
            "series_id",
            "provider",
            "title",
            "frequency",
            "as_of",
            "observations",
            "unit",
            "geography",
            "license",
            "source_url",
            "metadata",
        }
        kwargs = {k: v for k, v in payload.items() if k in known}
        obs = kwargs.get("observations", []) or []
        kwargs["observations"] = [
            o if isinstance(o, Observation) else Observation(period=o["period"], value=o.get("value"))
            for o in obs
        ]
        return cls(**kwargs)
