"""
The backing abstraction: one read interface, two storage realizations.

Consumers of a knowledge domain must never be able to tell whether it is
served from the shared corpus (``corpus-view``) or from a provisioned
namespace (``namespace``). Every read the KB contract will expose is declared
here; a backing that has not yet implemented a call raises
:class:`NotImplementedError` so the gap is loud, not silent.

The read surface mirrors the planned ``noesis-kb-v1`` contract:

- retrieve: :meth:`DomainBacking.documents`, :meth:`DomainBacking.search`,
  :meth:`DomainBacking.claims`, :meth:`DomainBacking.entities`
- diff:     :meth:`DomainBacking.diff`
- meta:     :meth:`DomainBacking.coverage` (always implemented — it reports
  the backing type and readiness even before the data paths land)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:  # pragma: no cover - import cycle guard, typing only
    from src.kb.registry import DomainDefinition


class DomainBacking:
    """Read interface every knowledge domain answers, whatever its storage.

    Subclasses implement the data paths incrementally; until then each call
    raises :class:`NotImplementedError` naming the backing, so a consumer
    hitting an unfinished path gets a diagnosable error instead of silence.
    """

    #: machine-readable backing discriminator, overridden per subclass
    backing_type: str = "abstract"

    def __init__(self, definition: "DomainDefinition") -> None:
        self.definition = definition

    # -- retrieve -----------------------------------------------------------

    def documents(
        self,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Member documents, newest first, each row citing its source."""
        raise self._not_implemented("documents")

    def search(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """Semantic + lexical search scoped to this domain."""
        raise self._not_implemented("search")

    def claims(
        self,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Clustered, cited claims for this domain."""
        raise self._not_implemented("claims")

    def entities(self, name: Optional[str] = None) -> List[Dict[str, Any]]:
        """Canonical entities (with aliases) mentioned in this domain."""
        raise self._not_implemented("entities")

    # -- diff ---------------------------------------------------------------

    def diff(self, since: str) -> Dict[str, Any]:
        """What changed since ``since`` — the primitive consumers reduce to."""
        raise self._not_implemented("diff")

    # -- meta ---------------------------------------------------------------

    def coverage(self) -> Dict[str, Any]:
        """Domain metadata: backing, sources, freshness, embedding model.

        Always answerable. Subclasses extend the payload with real corpus
        stats once their data paths exist; ``ready`` flips to True when the
        retrieve surface is implemented.
        """
        return {
            "domain": self.definition.name,
            "backing": self.backing_type,
            "embedding_model": self.definition.embedding_model,
            "feeds": [feed.url for feed in self.definition.feeds],
            "tags": list(self.definition.tags),
            "ready": False,
        }

    # -- helpers ------------------------------------------------------------

    def _not_implemented(self, call: str) -> NotImplementedError:
        return NotImplementedError(
            f"{call}() is not implemented yet for domain "
            f"{self.definition.name!r} (backing {self.backing_type!r})"
        )


class CorpusViewBacking(DomainBacking):
    """Domain served by membership rows + views over the shared corpus.

    The data paths (membership pass, per-domain views) are wired by the
    corpus-view implementation issue; until then only :meth:`coverage`
    answers.
    """

    backing_type = "corpus-view"


class NamespaceBacking(DomainBacking):
    """Domain served by a provisioned namespace with its own storage.

    Wired against the provisioning plane in a later increment; until then
    only :meth:`coverage` answers. ``namespace`` defaults to the domain name
    at validation time, so the field is always present here.
    """

    backing_type = "namespace"

    def coverage(self) -> Dict[str, Any]:
        payload = super().coverage()
        payload["namespace"] = self.definition.namespace
        return payload
