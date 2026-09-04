"""Technical knowledge pack registration."""

from src.domains.registry import register_pack
from src.domains.technical.pack import TechnicalDomainPack

register_pack(TechnicalDomainPack)

__all__ = ["TechnicalDomainPack"]
