"""
Research domain pack (R7 / Track N1).

Bundles the paper-metadata enrichers, research panel ``ui_flags`` and
research-flavored telemetry. Importing this module registers
``ResearchDomainPack`` in the domain registry; the pack is *enabled* only
when ``"research"`` appears in ``config/domain_packs.json`` (or the
``NEURONEWS_ENABLED_PACKS`` env var). It is registered regardless so the
registry always knows the pack exists.
"""

from src.domains.research.pack import ResearchDomainPack
from src.domains.registry import register_pack

register_pack(ResearchDomainPack)

__all__ = ["ResearchDomainPack"]
