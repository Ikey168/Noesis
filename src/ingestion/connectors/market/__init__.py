"""Licensed market-data adapters.

Adapters normalize provider responses but do not create entitlements.  Callers
must supply license and entitlement identifiers that are already authorized by
the market stores.
"""

from src.ingestion.connectors.market.fmp import (
    FmpEodAdapter,
    FmpListing,
    FmpMarketDataError,
)

__all__ = ["FmpEodAdapter", "FmpListing", "FmpMarketDataError"]
