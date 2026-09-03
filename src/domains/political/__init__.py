"""Political knowledge domain pack registration."""

from src.domains.political.pack import PoliticalDomainPack
from src.domains.registry import register_pack

register_pack(PoliticalDomainPack)

__all__ = ["PoliticalDomainPack"]
