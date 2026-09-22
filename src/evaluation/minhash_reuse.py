"""Evaluation-only MinHash LSH candidate generation for text reuse.

The index deliberately remains a candidate generator. Final reuse/dependency
decisions use exact shingle Jaccard or explicit provenance links so an LSH miss
cannot erase a mandatory provenance relationship.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


def _tokens(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.casefold(), flags=re.UNICODE)


def shingles(text: str, size: int) -> set[str]:
    tokens = _tokens(text)
    if not tokens:
        return set()
    if len(tokens) < size:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 1.0


@dataclass(frozen=True)
class ReuseRecord:
    record_id: str
    text: str
    revision: str
    provenance_links: tuple[str, ...] = ()


class MinHashReuseIndex:
    """Versioned optional LSH index with deterministic rebuild/replay."""

    def __init__(
        self,
        *,
        shingle_size: int = 5,
        num_perm: int = 128,
        threshold: float = 0.45,
        seed: int = 1,
    ) -> None:
        if not 1 <= shingle_size <= 20:
            raise ValueError("shingle_size must be between 1 and 20")
        if not 16 <= num_perm <= 512:
            raise ValueError("num_perm must be between 16 and 512")
        if not 0 < threshold < 1:
            raise ValueError("threshold must be between zero and one")
        self.shingle_size = shingle_size
        self.num_perm = num_perm
        self.threshold = float(threshold)
        self.seed = int(seed)
        self.records: dict[str, ReuseRecord] = {}
        self._sets: dict[str, set[str]] = {}
        self._incoming: dict[str, set[str]] = {}
        self._minhashes: dict[str, Any] = {}
        self._new_lsh()

    @property
    def version(self) -> str:
        payload = [self.shingle_size, self.num_perm, self.threshold, self.seed]
        digest = hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:12]
        return f"noesis-minhash-lsh-v1:{digest}"

    def _new_lsh(self) -> None:
        try:
            from datasketch import MinHashLSH
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("install datasketch to use MinHashReuseIndex") from exc
        self._lsh = MinHashLSH(threshold=self.threshold, num_perm=self.num_perm)

    def _signature(self, values: set[str]):
        from datasketch import MinHash

        signature = MinHash(num_perm=self.num_perm, seed=self.seed)
        for value in sorted(values):
            signature.update(value.encode("utf-8"))
        return signature

    def add(self, record: ReuseRecord) -> None:
        identity = str(record.record_id).strip()
        if not identity or not record.revision:
            raise ValueError("record id and revision are required")
        if (
            not isinstance(record.text, str)
            or not record.text.strip()
            or len(record.text) > 262144
            or not _tokens(record.text)
        ):
            raise ValueError("bounded nonempty evidence text required")
        if len(record.provenance_links) > 1000 or any(
            not isinstance(v, str) or not v for v in record.provenance_links
        ):
            raise ValueError("bounded provenance link identities required")
        existing = self.records.get(identity)
        if existing is None and len(self.records) >= 10000:
            raise ValueError("index record budget exceeded")
        if (
            sum(len(v.text) for v in self.records.values())
            - (len(existing.text) if existing else 0)
            + len(record.text)
            > 16000000
        ):
            raise ValueError("index text budget exceeded")
        if existing == record:
            return
        if existing is not None:
            self.remove(identity)
        values = shingles(record.text, self.shingle_size)
        signature = self._signature(values)
        self._lsh.insert(identity, signature)
        self.records[identity] = record
        for target in record.provenance_links:
            self._incoming.setdefault(target, set()).add(identity)
        self._sets[identity] = values
        self._minhashes[identity] = signature

    def remove(self, record_id: str) -> bool:
        if record_id not in self.records:
            return False
        self._lsh.remove(record_id)
        previous = self.records.pop(record_id)
        for target in previous.provenance_links:
            incoming = self._incoming.get(target, set())
            incoming.discard(record_id)
            if not incoming:
                self._incoming.pop(target, None)
        self._sets.pop(record_id, None)
        self._minhashes.pop(record_id, None)
        return True

    def query(self, record: ReuseRecord) -> list[dict[str, Any]]:
        values = shingles(record.text, self.shingle_size)
        signature = self._signature(values)
        candidates = set(self._lsh.query(signature))
        candidates.discard(record.record_id)
        # Provenance-linked records are mandatory candidates even if lexical LSH
        # misses them. This preserves the independence/reuse audit boundary.
        candidates.update(
            link for link in record.provenance_links if link in self.records
        )
        candidates.update(self._incoming.get(record.record_id, set()))
        candidates.discard(record.record_id)
        rows = []
        for identity in sorted(candidates):
            candidate = self.records[identity]
            mandatory = (
                identity in record.provenance_links
                or record.record_id in candidate.provenance_links
            )
            rows.append(
                {
                    "record_id": identity,
                    "revision": candidate.revision,
                    "estimated_similarity": float(
                        signature.jaccard(self._minhashes[identity])
                    ),
                    "exact_similarity": jaccard(values, self._sets[identity]),
                    "mandatory_provenance_candidate": mandatory,
                }
            )
        return rows

    def adjudicate(
        self, record: ReuseRecord, *, exact_threshold: float = 0.5
    ) -> list[dict[str, Any]]:
        if not 0 <= exact_threshold <= 1:
            raise ValueError("invalid exact threshold")
        return [
            {
                **candidate,
                "reuse_decision": bool(
                    candidate["mandatory_provenance_candidate"]
                    or candidate["exact_similarity"] >= exact_threshold
                ),
                "decision_method": "explicit-provenance-or-exact-shingle-jaccard",
            }
            for candidate in self.query(record)
        ]

    def export_state(self) -> dict[str, Any]:
        return {
            "contract": "noesis-minhash-reuse-index-v1",
            "version": self.version,
            "configuration": {
                "shingle_size": self.shingle_size,
                "num_perm": self.num_perm,
                "threshold": self.threshold,
                "seed": self.seed,
            },
            "records": [asdict(self.records[key]) for key in sorted(self.records)],
        }

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> MinHashReuseIndex:
        if state.get("contract") != "noesis-minhash-reuse-index-v1":
            raise ValueError("unsupported MinHash index contract")
        index = cls(**state["configuration"])
        if state.get("version") != index.version:
            raise ValueError("MinHash index configuration/version mismatch")
        for value in state.get("records", []):
            index.add(
                ReuseRecord(
                    record_id=value["record_id"],
                    text=value["text"],
                    revision=value["revision"],
                    provenance_links=tuple(value.get("provenance_links") or ()),
                )
            )
        return index


def exhaustive_adjudication(
    records: list[ReuseRecord], *, shingle_size: int = 5, exact_threshold: float = 0.5
) -> set[tuple[str, str]]:
    sets = {record.record_id: shingles(record.text, shingle_size) for record in records}
    lookup = {record.record_id: record for record in records}
    decisions: set[tuple[str, str]] = set()
    for i, left in enumerate(records):
        for right in records[i + 1 :]:
            mandatory = (
                right.record_id in left.provenance_links
                or left.record_id in right.provenance_links
            )
            if (
                mandatory
                or jaccard(sets[left.record_id], sets[right.record_id])
                >= exact_threshold
            ):
                decisions.add(tuple(sorted((left.record_id, right.record_id))))
    # Preserve declared links even if the linked record is missing from this
    # bounded benchmark slice only when both endpoints exist.
    for record in records:
        for linked in record.provenance_links:
            if linked in lookup:
                decisions.add(tuple(sorted((record.record_id, linked))))
    return decisions
