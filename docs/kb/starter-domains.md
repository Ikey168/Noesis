# Starter knowledge domains

`config/domains.yml` ships six domains. One command stands them up:

```bash
make kb-bootstrap            # seed feeds + harvest + assign membership
# or, offline:
python3 scripts/kb_bootstrap.py --no-harvest
```

Seeding is idempotent and non-destructive: missing feeds are subscribed into
`config/blog_subscriptions.json` under the domain's tags; existing
subscriptions only ever gain tags, and user-added feeds are never touched.
Any feed you subscribe yourself with a domain's tag (e.g. `local`) feeds that
domain via by-source membership — no config change needed.

## Curated feeds and why

| Domain | Feed | Rationale |
|---|---|---|
| news | BBC World | broad, fast wire-style coverage |
| news | The Guardian World | second general outlet, different editorial line |
| news | NPR News | US-centric complement |
| news | Al Jazeera | non-Western vantage for contradiction analysis |
| economics | The Economist — Finance & economics | macro analysis |
| economics | FRED Blog | data-first, primary-source charts |
| economics | Marginal Revolution | academic-economics commentary |
| economics | Calculated Risk | housing/markets nowcasting |
| economics | CNBC Economy | daily markets newsflow |
| technology | Ars Technica | depth-first tech reporting |
| technology | The Verge | consumer/industry breadth |
| technology | MIT Technology Review | research-adjacent analysis |
| technology | Hacker News frontpage | community signal for what matters today |
| web3 | CoinDesk | industry newswire |
| web3 | Cointelegraph | second wire, corroboration/contradiction pair |
| web3 | Ethereum Foundation Blog | primary-source protocol announcements |
| web3 | Vitalik Buterin | primary-source design arguments |
| papers | arXiv cs.AI / cs.CL / cs.CR / econ.GN | preprints as ordinary feeds; category set mirrors the other domains' subjects |
| local | *(none — add your city's feeds)* | no good generic source exists; ICS/calendar connector is a tracked expansion |

Prune or extend freely — the list is config, not code. Keyword lists are the
*seed vocabulary* for by-content membership (word-boundary matching; two
distinct hits clear the default threshold), and `embedding_anchors` describe
the domain for the similarity method once document embeddings exist
(`kb_bootstrap.py --embeddings`).

## Cross-domain overlap is a feature

A stablecoin-regulation story is legitimately both `web3` and `economics`;
an AI-chip export-control story is both `technology` and `news`. Membership
is many-to-many by design — the shared corpus with domain views exists
precisely so one document can serve several domains without duplication.

## Where papers go next

`papers` starts as a corpus-view over arXiv RSS (abstracts). The reference
increment promotes it to a namespace-backed domain with full-text ingestion,
books, and its own retention — consumers notice nothing because they only
ever see the `DomainBacking` interface.
