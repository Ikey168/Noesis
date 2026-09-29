from src.domains.legal.pack import LegalDomainPack
from src.domains.registry import register_pack

register_pack(LegalDomainPack)

__all__ = ["LegalDomainPack"]
