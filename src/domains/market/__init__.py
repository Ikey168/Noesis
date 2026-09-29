"""Financial market domain services."""

from src.domains.market.actions import (
    MarketActionError,
    MarketCorporateActionStore,
    ensure_market_action_schema,
)
from src.domains.market.instruments import (
    MarketInstrumentError,
    MarketInstrumentStore,
    ensure_market_instrument_schema,
)
from src.domains.market.financial_facts import (
    MarketFinancialFactError,
    MarketFinancialFactStore,
    ensure_market_financial_fact_schema,
)
from src.domains.market.prices import (
    MarketIngestBudget,
    MarketPriceError,
    MarketPriceIngestor,
    MarketPriceStore,
    ensure_market_price_schema,
)
from src.domains.market.asof import MarketAsOfError, MarketAsOfSnapshotStore
from src.domains.market.quality import MarketQualityError, MarketQualityStore
from src.domains.market.entitlements import (
    MarketEntitlementError,
    MarketEntitlementStore,
)
from src.domains.market.metrics import MarketMetricError, MarketMetricStore
from src.domains.market.dashboard import (
    MarketCompanyDashboardStore,
    MarketDashboardError,
)
from src.domains.market.screeners import (
    MarketScreenerError,
    MarketScreenerStore,
    ensure_market_screener_schema,
)
from src.domains.market.alerts import (
    ALERT_DELIVER_SCOPE,
    ALERT_EXECUTE_SCOPE,
    ALERT_READ_SCOPE,
    ALERT_WRITE_SCOPE,
    MarketAlertError,
    MarketAlertStore,
    ensure_market_alert_schema,
)
from src.domains.market.quantitative import (
    MarketQuantitativeError,
    MarketQuantitativeStore,
    ensure_market_quantitative_schema,
)
from src.domains.market.research import (
    MarketResearchError,
    MarketResearchStore,
    ensure_market_research_schema,
)
from src.domains.market.operations import (
    MarketOperationsError,
    MarketOperationsStore,
    OPERATIONS_EXECUTE_SCOPE,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    ensure_market_operations_schema,
)
from src.domains.market.specialized import (
    MarketSpecializedError,
    MarketSpecializedStore,
    SPECIALIZED_READ_SCOPE,
    SPECIALIZED_WRITE_SCOPE,
    ensure_market_specialized_schema,
)

__all__ = [
    "MarketActionError",
    "MarketAsOfError",
    "MarketAsOfSnapshotStore",
    "MarketCorporateActionStore",
    "MarketInstrumentError",
    "MarketInstrumentStore",
    "MarketFinancialFactError",
    "MarketFinancialFactStore",
    "MarketEntitlementError",
    "MarketEntitlementStore",
    "MarketMetricError",
    "MarketMetricStore",
    "MarketCompanyDashboardStore",
    "MarketDashboardError",
    "MarketScreenerError",
    "MarketScreenerStore",
    "MarketAlertError",
    "MarketAlertStore",
    "MarketQuantitativeError",
    "MarketQuantitativeStore",
    "MarketResearchError",
    "MarketResearchStore",
    "MarketOperationsError",
    "MarketOperationsStore",
    "MarketIngestBudget",
    "MarketPriceError",
    "MarketPriceIngestor",
    "MarketPriceStore",
    "MarketQualityError",
    "MarketQualityStore",
    "ensure_market_action_schema",
    "ensure_market_instrument_schema",
    "ensure_market_financial_fact_schema",
    "ensure_market_price_schema",
    "ensure_market_screener_schema",
    "ensure_market_alert_schema",
    "ensure_market_quantitative_schema",
    "ensure_market_research_schema",
    "ensure_market_operations_schema",
    "MarketSpecializedError",
    "MarketSpecializedStore",
    "ensure_market_specialized_schema",
    "ALERT_READ_SCOPE",
    "ALERT_WRITE_SCOPE",
    "ALERT_EXECUTE_SCOPE",
    "OPERATIONS_READ_SCOPE",
    "OPERATIONS_WRITE_SCOPE",
    "OPERATIONS_EXECUTE_SCOPE",
    "SPECIALIZED_READ_SCOPE",
    "SPECIALIZED_WRITE_SCOPE",
    "ALERT_DELIVER_SCOPE",
]
