"""Units, QC flags and parameter names for operational weather records (WX07, #2170).

**Units** are converted with exact rational arithmetic (:class:`fractions.Fraction`)
over declared, source-documented factors. Only the final result is rounded
(half-even at a stated precision), and the native value and unit are kept next
to it. The conversion needs no ``pint``: the CI lane has none, and a weather
conversion must not silently turn into "unconvertible" when an optional
dependency is missing. Each conversion carries a receipt digest of its inputs
and factors, so it can be replayed. :func:`src.integrations.units.convert_physical`
stays the pint owner for the Climate pack. :func:`cross_check` compares a factor
with it when pint is installed, and this module never depends on it.

**QC flags** keep the native flag verbatim. :func:`qc_common` adds one
documented common state from :data:`QC_STATES`:

* ``passed`` — the publisher states quality control is finished;
* ``checked_partial`` — some documented control was applied, not all;
* ``provisional`` — only formal checks, or explicitly not yet controlled;
* ``suspect`` — the publisher marks the value questionable (e.g. METAR ``$``);
* ``failed`` — the publisher marks it as failing control;
* ``unknown`` — the scheme or flag has no documented meaning here.

Verification excludes ``suspect`` and ``failed`` values and counts them.

**Parameters** map native element names onto a small common vocabulary
(:data:`PARAMETERS`). Two quantities are mapped to the same name only when they
are the same physical quantity: a METAR altimeter setting and a MOSMIX
mean-sea-level pressure are kept apart.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from fractions import Fraction
from typing import Any

# native unit -> (canonical unit, factor, offset); canonical = value * factor + offset (exact rationals).
UNITS: dict[str, tuple[str, Fraction, Fraction]] = {
    "°C": ("°C", Fraction(1), Fraction(0)),
    "degC": ("°C", Fraction(1), Fraction(0)),
    "wmoUnit:degC": ("°C", Fraction(1), Fraction(0)),
    "1/10 °C": ("°C", Fraction(1, 10), Fraction(0)),
    "K": ("°C", Fraction(1), Fraction(-27315, 100)),
    "°F": ("°C", Fraction(5, 9), Fraction(-160, 9)),
    "F": ("°C", Fraction(5, 9), Fraction(-160, 9)),
    "wmoUnit:degF": ("°C", Fraction(5, 9), Fraction(-160, 9)),
    "m/s": ("m/s", Fraction(1), Fraction(0)),
    "wmoUnit:m_s-1": ("m/s", Fraction(1), Fraction(0)),
    "kt": ("m/s", Fraction(1852, 3600), Fraction(0)),
    "km/h": ("m/s", Fraction(5, 18), Fraction(0)),
    "wmoUnit:km_h-1": ("m/s", Fraction(5, 18), Fraction(0)),
    "mph": ("m/s", Fraction(1609344, 3600000), Fraction(0)),
    "hPa": ("hPa", Fraction(1), Fraction(0)),
    "Pa": ("hPa", Fraction(1, 100), Fraction(0)),
    "wmoUnit:Pa": ("hPa", Fraction(1, 100), Fraction(0)),
    # 1 inHg = 3386.389 Pa (NIST SP 811 conventional value, 0 °C mercury) (verify).
    "inHg": ("hPa", Fraction(3386389, 100000), Fraction(0)),
    "mm": ("mm", Fraction(1), Fraction(0)),
    # 1 kg of water over 1 m² is a 1 mm column (the meteorological convention for precipitation amounts).
    "kg/m2": ("mm", Fraction(1), Fraction(0)),
    "kg m-2": ("mm", Fraction(1), Fraction(0)),
    "m": ("m", Fraction(1), Fraction(0)),
    "wmoUnit:m": ("m", Fraction(1), Fraction(0)),
    "SM": ("m", Fraction(1609344, 1000), Fraction(0)),
    "%": ("%", Fraction(1), Fraction(0)),
    "wmoUnit:percent": ("%", Fraction(1), Fraction(0)),
    "°": ("°", Fraction(1), Fraction(0)),
    "deg": ("°", Fraction(1), Fraction(0)),
    "wmoUnit:degree_(angle)": ("°", Fraction(1), Fraction(0)),
}
PINT_NAMES = {
    "°C": "degree_Celsius",
    "K": "kelvin",
    "°F": "degree_Fahrenheit",
    "m/s": "meter / second",
    "kt": "knot",
    "km/h": "kilometer / hour",
    "mph": "mile / hour",
    "hPa": "hectopascal",
    "Pa": "pascal",
    "inHg": "inch_Hg",
    "mm": "millimeter",
    "m": "meter",
    "SM": "mile",
}
PRECISION = 6

QC_STATES = ("passed", "checked_partial", "provisional", "suspect", "failed", "unknown")
EXCLUDED_QC = frozenset({"suspect", "failed"})
# DWD CDC quality levels (QN / QN_9) as documented by DWD (verify).
DWD_QN = {
    "1": ("provisional", "only formal control"),
    "2": ("checked_partial", "controlled with individually defined criteria"),
    "3": ("checked_partial", "automatic control and correction (ROUTINE)"),
    "5": ("checked_partial", "historic, subjective procedures"),
    "7": ("checked_partial", "second control done, before correction"),
    "8": ("checked_partial", "quality control outside ROUTINE"),
    "9": ("checked_partial", "not all parameters corrected"),
    "10": ("passed", "quality control finished, all corrections finished"),
}
QC_SCHEMES = {
    "dwd-qn": "DWD CDC quality level QN/QN_9 (kept verbatim)",
    "awc-qcfield": "aviationweather.gov qcField (bit semantics unconfirmed, verify); METAR '$' maintenance flag",
    "none-published": "the publisher states no QC flag for this value",
}

# Native element names -> common parameter names (quantity, not wording).
PARAMETERS = {
    "dwd-cdc": {
        "TT_10": "air_temperature",
        "TT_TU": "air_temperature",
        "RF_10": "relative_humidity",
        "RF_TU": "relative_humidity",
        "TD_10": "dew_point",
        "PP_10": "station_pressure",
        "TM5_10": "air_temperature_5cm",
        "R1": "precipitation_1h",
    },
    "dwd-mosmix": {
        "TTT": "air_temperature",
        "Td": "dew_point",
        "FF": "wind_speed",
        "DD": "wind_direction",
        "PPPP": "pressure_msl",
        "RR1c": "precipitation_1h",
        "N": "cloud_cover",
        "R101": "probability_precipitation_1h_gt_0.1mm",
    },
    "aviationweather": {
        "temp": "air_temperature",
        "dewp": "dew_point",
        "wspd": "wind_speed",
        "wdir": "wind_direction",
        "wgst": "wind_gust",
        "altim": "altimeter_setting",
        "slp": "pressure_msl",
        "visib": "visibility",
    },
    "nws": {
        "temperature": "air_temperature",
        "windSpeed": "wind_speed",
        "windDirection": "wind_direction",
        "probabilityOfPrecipitation": "nws_probability_of_precipitation",
        "relativeHumidity": "relative_humidity",
        "dewpoint": "dew_point",
    },
    "open-meteo": {
        "temperature_2m": "air_temperature",
        "relative_humidity_2m": "relative_humidity",
        "dew_point_2m": "dew_point",
        "wind_speed_10m": "wind_speed",
        "pressure_msl": "pressure_msl",
    },
}


def common_parameter(provider: str, native: str) -> str | None:
    return PARAMETERS.get(provider, {}).get(native)


def _receipt(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalise(
    value: str | None, unit: str | None, *, precision: int = PRECISION
) -> dict[str, Any] | None:
    """Exact conversion of a published decimal to the canonical unit, or ``None`` (missing value or unmapped unit)."""

    if value is None or unit not in UNITS:
        return None
    target, factor, offset = UNITS[unit]
    exact = Fraction(Decimal(str(value))) * factor + offset
    # round() on a Fraction is exact and rounds half to even; only this final step rounds.
    rounded = Decimal(round(exact * 10**precision)).scaleb(-precision)
    text = format(rounded.normalize(), "f")
    method = "identity" if factor == 1 and offset == 0 else "exact rational factor"
    receipt = _receipt(
        {
            "value": str(value),
            "unit": unit,
            "target": target,
            "factor": str(factor),
            "offset": str(offset),
            "precision": precision,
        }
    )
    return {
        "value": text,
        "unit": target,
        "native_value": str(value),
        "native_unit": unit,
        "method": method,
        "factor": str(factor),
        "offset": str(offset),
        "rounding": f"half-even at 1e-{precision}",
        "receipt_sha256": receipt,
    }


def cross_check(unit: str) -> dict[str, Any]:
    """Compare a declared factor with ``src.integrations.units.convert_physical`` when pint is installed."""

    try:
        import pint  # noqa: F401
    except ImportError:
        return {
            "unit": unit,
            "status": "not_checked",
            "reason": "pint is not installed (optional)",
        }
    from src.integrations.units import convert_physical

    target, _, _ = UNITS[unit]
    if unit not in PINT_NAMES or target not in PINT_NAMES:
        return {
            "unit": unit,
            "status": "not_checked",
            "reason": "no pint expression declared",
        }
    ours = Decimal(normalise("1", unit, precision=6)["value"])
    theirs = Decimal(
        convert_physical("1", PINT_NAMES[unit], PINT_NAMES[target], precision=6)[
            "result"
        ]["value"]
    )
    return {
        "unit": unit,
        "status": "agrees" if abs(ours - theirs) <= Decimal("0.000002") else "differs",
        "ours": str(ours),
        "pint": str(theirs),
    }


def qc_common(
    scheme: str, native: str | None, *, raw_text: str | None = None
) -> dict[str, Any]:
    """The common QC state for a native flag; the native flag and scheme are returned with it."""

    state, meaning = "unknown", "no documented meaning for this flag"
    if scheme == "dwd-qn" and native is not None:
        state, meaning = DWD_QN.get(
            str(native).strip(), ("unknown", "undocumented DWD quality level")
        )
    elif scheme == "awc-qcfield":
        if raw_text and raw_text.rstrip().endswith("$"):
            state, meaning = (
                "suspect",
                "METAR maintenance indicator '$' (station needs maintenance)",
            )
        else:
            meaning = "qcField kept verbatim; its semantics are unconfirmed (verify)"
    elif scheme == "none-published":
        meaning = "the publisher states no quality control for this value"
    out = {"scheme": scheme, "common": state, "meaning": meaning}
    if native is not None:
        out["native"] = str(native)
    return out
