---
name: literature-watch
description: Keep a Noesis paper current while it is being written — standing evidence watches on the paper's topics and key claims, feed subscriptions for journals, preprint servers and policy newsletters triaged in short Awareness sessions, education-statistics monitors for new or revised release vintages, and AI-model monitors for the systems the cited studies used. Each check ends with a decision per item (ignore, note, add to the review, revise the draft) that is recorded, not remembered. Use when a paper on a fast-moving topic such as AI in education will take weeks or months to finish, or between submission and revision. Drives the noesis gateway watch tool and the noesis-knowledge-engine feed, awareness and monitor tools.
---

# Literature watch

On a topic that moves weekly, a paper drafted in spring is out of date by its
review round. This skill sets up the watches once and then runs a short, recorded
check on a fixed rhythm. It does not change the paper by itself: anything worth
acting on goes to **systematic-review** (as a new candidate under the
protocol) or **paper-draft** (as a revision). Run it inside the
**paper-project** namespace.

## Set up once

**1. Evidence watches on topics and claims** — gateway `watch` tool on the
`noesis` MCP server:

```
watch(action="create", domain="research", selector_type="topic",  selector_value="generative AI tutoring learning outcomes")
watch(action="create", domain="research", selector_type="claim",  selector_value="<claim id the paper's conclusion rests on>")
watch(action="create", domain="research", selector_type="query",  selector_value="AI detection accuracy non-native writers")
```

`selector_type` is `query`, `claim`, `entity` or `topic`. Watch the claims the
paper's main conclusions depend on, not just broad topics. Poll with
`watch(action="poll", watch_id=..., cursor=...)`; a watch only sees documents
that get ingested, so pair it with the next step.

**2. Feeds** — `subscribe_intake_feed(namespace, url, name)` for public,
credential-free RSS/Atom (see **awareness-inbox**). For AI in education, useful
streams include the tables of contents of the journals and proceedings the
review draws on, arXiv listings for `cs.CY` and `cs.CL` filtered by
education terms, EdArXiv, and the newsletters of UNESCO, OECD education and
the national bodies relevant to the paper. Feeds bring items to the inbox; they
are not evidence until ingested.

**3. Statistics monitors** — `create_education_monitor(namespace, request_key,
watch={"country": "DE"} | {"indicator": "<code>"} | {"ror": "..."} | {"institution": {"scheme": "ipeds-unitid", "code": "..."}},
delivery=None)`, one key per monitor (plus optional `"providers"`). Notices
report new vintages, revised values and identity-match changes for statistics
the paper cites.

**4. AI-model monitors** — `create_ai_models_monitor(namespace, request_key,
target={"repo_id": "..."} | {"epoch_model": "..."} | {"record_id": "..."} | {"source": "..."})`
for the systems the cited studies evaluated: new revisions, licence changes,
gating and removals.

Record all watch, subscription and monitor ids in the research project
(`revise_research_project`, as references) so the next check can find them.

## Each check (weekly while drafting is a good default)

1. `refresh_intake_feed_inbox(namespace)`, then
   `start_awareness_from_inbox(namespace, request_key="watch-<date>")` — the
   Awareness budget is 15 minutes; it is triage, not reading.
2. Poll everything: `watch(action="poll", ...)` per watch,
   `run_education_monitor` / `poll_education_monitor`,
   `run_ai_models_monitor` / `poll_ai_models_monitor`.
3. Decide each item with `triage_awareness_item` (decisions `watch`,
   `escalate`, `schedule`, `discard`, `archive`, `flag`) using this test:

| Item | Decision |
|---|---|
| New study inside the review's question and date window | `escalate`: ingest it, then `add_review_candidate` under the protocol (or an amendment if the window must move). It is screened like every other candidate. |
| New study that challenges a claim the paper makes | `escalate` → Deep Research; then a **paper-draft** revision or a discussion point. Never drop it because it is inconvenient. |
| Revised statistic the paper cites | `flag`; update through `assess_authored_report_changes` in **paper-draft**. |
| Retraction or correction of a cited work | `flag`; handle with **reference-integrity**. |
| Commentary, news, vendor announcements | `archive` or `discard`, unless the paper discusses public claims. |

4. `promote_awareness_item(..., target_mode="Deep Research")` for anything that
   needs more than triage, then complete the session.

## Freezing for submission

Pick a search cut-off date, state it in the methods, and set the protocol's
`date_to` accordingly. Studies found after the cut-off are reported as
"identified after the search" or left for revision, not quietly added.

## Rules

- A watch notice is a lead, not evidence; cite only ingested, retained sources.
- Every check ends with a recorded decision per item.
- Do not subscribe to private or paywalled feeds with credentials, and do not
  ingest new material without the user's go-ahead.
