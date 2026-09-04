"""Economic knowledge domain-pack registration."""

from src.domains.economic.pack import EconomicsDomainPack
from src.domains.registry import register_pack

register_pack(EconomicsDomainPack)

__all__ = ["EconomicsDomainPack"]
