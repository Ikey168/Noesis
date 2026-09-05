"""MinHash candidate generation; exact/provenance decisions remain in Noesis."""

from collections import defaultdict
from itertools import combinations
from .common import IntegrationError, finite, version, digest


def candidate_pairs(
    signals, *, threshold=0.5, num_perm=128, seed=1, max_pairs=1_000_000
):
    from datasketch import MinHash, MinHashLSH

    threshold = finite(threshold, "LSH threshold", 0.01, 0.99)
    if len(signals) > 100000 or not 16 <= num_perm <= 512:
        raise IntegrationError("input_limit", "Candidate index bounds exceeded")
    pairs = set()

    def add(a, b):
        if a == b:
            return
        pairs.add(tuple(sorted((a, b))))
        if len(pairs) > max_pairs:
            raise IntegrationError(
                "candidate_limit",
                "Candidate pair budget exceeded; no partial inference published",
            )

    ids = sorted(signals)
    for field in ("text_shingles", "word_fingerprints"):
        index = MinHashLSH(threshold=threshold, num_perm=num_perm)
        for identity in ids:
            values = set(signals[identity].get(field) or [])
            if not values:
                continue
            mh = MinHash(num_perm=num_perm, seed=seed)
            mh.update_batch([str(v).encode() for v in sorted(values)])
            for other in index.query(mh):
                add(identity, other)
            index.insert(identity, mh)
    # Preserve every explicit shared provenance signal, even with dissimilar text.
    for field in (
        "content_hash",
        "canonical_url",
        "explicit_upstreams",
        "bylines",
        "media_hashes",
        "outbound_links",
        "quote_markers",
        "claim_fingerprints",
        "publisher_owner",
    ):
        groups = defaultdict(list)
        for identity in ids:
            values = signals[identity].get(field)
            if not isinstance(values, list):
                values = [values]
            for v in set(v for v in values if v):
                groups[v].append(identity)
        for members in groups.values():
            for a, b in combinations(members, 2):
                add(a, b)
    return sorted(pairs), {
        "backend": "datasketch-minhash",
        "version": version("datasketch"),
        "threshold": threshold,
        "num_perm": num_perm,
        "seed": seed,
        "candidate_pairs": len(pairs),
        "input_sha256": digest(signals),
        "index_policy": "rebuild from current signals on each run",
        "recall": "approximate lexical candidates plus exhaustive shared-provenance candidates",
    }
