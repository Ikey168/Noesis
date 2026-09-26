# Cross-domain market as-of snapshots

`MarketAsOfSnapshotStore` composes retained revisions from the existing market, economic and document stores. It saves an immutable manifest containing the selected revision IDs and hashes, the effective-time and availability cutoffs, transformation receipts, and known gaps. It references source records instead of copying their payloads.

```python
manifest = MarketAsOfSnapshotStore(conn).create_snapshot(
    "market:research",
    "software-peer-review-2026q3",
    effective_at_ms=1790208000000,
    publicly_available_by_ms=1790208000000,
    acquired_by_ms=1790208000000,
    selection={
        "listings": ["listing:issuer-a"],
        "prices": [{
            "listing_id": "listing:issuer-a",
            "interval": "1d",
            "start_ms": 1787529600000,
            "end_ms": 1790208000000,
        }],
        "actions": [{"security_id": "security:issuer-a"}],
        "financial_facts": [{"issuer_id": "issuer-a", "taxonomy": "us-gaap"}],
        "economic_snapshots": [{
            "namespace": "economics",
            "snapshot_id": "economic-snapshot:quarter-close",
        }],
        "documents": [{"document_id": "filing:issuer-a:10-q"}],
    },
    transformations=[],
    principal_id="analyst:research",
    scopes={"operator"},
    gap_policy="record",
)
```

The effective time checks listing validity, the public cutoff checks what the provider or source had made available, and the acquisition cutoff checks what Noesis had actually retained. The public cutoff cannot be later than the acquisition cutoff. Market bars, corporate actions and financial facts use the stores' bitemporal revision selectors. Documents require an explicit millisecond `public_at_ms` or `published_at_ms` on the revision payload; the service does not infer public time from ingestion. An economic release input carries its release cutoff and per-series release-time basis, because a provider vintage date can differ from the exact public availability time.

Supported selectors are listings, bounded price ranges, corporate actions by security, filing facts by issuer with optional taxonomy/concept/form filters, existing economic release snapshots, and document IDs. Optional transformations pin either a market adjustment calculation or a shared quantitative calculation. Their exact input revision IDs must also occur among the selected inputs; missing dependencies or nondeterministic replay become explicit gaps.

`gap_policy="record"` returns a partial manifest with gaps such as unavailable revisions, late documents, inactive listings, and missing history. `gap_policy="fail"` refuses to save when one of those gaps exists. Price ranges also carry a coverage marker. They remain marked as unproven and make overall coverage partial because the current selector does not include a trading calendar that could establish every expected session. The service never labels a sparse observed range complete.

`inspect_snapshot()` rechecks the manifest hashes and current source access before returning it. A caller who has lost access to a source cannot use an old manifest to bypass that source's current authorization. Fixture coverage exercises corrected bars at separate cutoffs, late documents, inactive listings, idempotent creation, transformation replay and integrity verification.

The contract schema is [noesis-market-asof-snapshot-v1](../../contracts/schemas/jsonschema/noesis-market-asof-snapshot-v1.json), with a synthetic [example manifest](../../contracts/examples/noesis-market-asof-snapshot-v1.json). The example is shape-only and contains no provider data. This is a Python domain service; live provider coverage and REST/MCP adapters remain separate acceptance work.
