# Published workflow review backlog

Updated 2026-09-05. All suggestions from the workflow improvement and feature reviews now have granular GitHub implementation/evaluation issues.

**Published in this pass:** 54 task issues (29 workflow improvements, 21 remaining feature tasks, and four further-validation tasks), grouped under 15 new tracking issues. The three Persistent research projects tasks and their tracker already existed and were reused. Source collection and scraping remain in their existing backlog.

**Total for these two workflow reviews:** 57 task issues under 16 tracking issues: 29 improvements, 24 feature tasks, and four further-validation tasks. Every task has a native GitHub parent relationship. Dependencies are linked in issue bodies; evaluation tasks identify proposed measurement rather than claiming unverified defects.

Review details: [workflow improvements](workflow-improvement-review.md), [feature opportunities](workflow-feature-opportunities.md), [source collection](source-collection-backlog.md).

## Tracking issues

| Tracking issue | Scope | Tasks |
|---|---|---|
| [#1393](https://github.com/Ikey168/Noesis/issues/1393) | Persistent research projects | 3 |
| [#1397](https://github.com/Ikey168/Noesis/issues/1397) | Reliable production knowledge workflows | 3 |
| [#1398](https://github.com/Ikey168/Noesis/issues/1398) | Document identity, citation offsets, domain membership, and entity resolution | 4 |
| [#1399](https://github.com/Ikey168/Noesis/issues/1399) | Revision-safe extraction, enrichment, and model evaluation | 7 |
| [#1400](https://github.com/Ikey168/Noesis/issues/1400) | Semantic indexing, embedding freshness, and bounded document coverage | 4 |
| [#1401](https://github.com/Ikey168/Noesis/issues/1401) | Query deadlines, retrieval relevance, and evidence-supported answers | 3 |
| [#1402](https://github.com/Ikey168/Noesis/issues/1402) | Subscription snapshot integrity, event semantics, and reliable delivery | 3 |
| [#1403](https://github.com/Ikey168/Noesis/issues/1403) | Research package verification and durable archive recovery | 5 |
| [#1404](https://github.com/Ikey168/Noesis/issues/1404) | Workflow conformance, migration, and workload validation | 4 |
| [#1405](https://github.com/Ikey168/Noesis/issues/1405) | Living research reports | 3 |
| [#1406](https://github.com/Ikey168/Noesis/issues/1406) | Unified evidence review inbox | 3 |
| [#1407](https://github.com/Ikey168/Noesis/issues/1407) | Systematic literature review workflow | 3 |
| [#1408](https://github.com/Ikey168/Noesis/issues/1408) | Zotero and bibliography interoperability | 3 |
| [#1409](https://github.com/Ikey168/Noesis/issues/1409) | Forecast registration and outcome tracking | 3 |
| [#1410](https://github.com/Ikey168/Noesis/issues/1410) | Reproducible analysis notebooks | 3 |
| [#1411](https://github.com/Ikey168/Noesis/issues/1411) | Evidence-linked decision records | 3 |

## Persistent research projects

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-01 | [#1394](https://github.com/Ikey168/Noesis/issues/1394) — Persist research project questions, scope, and linked investigation state | Project state can start independently. |
| NF-02 | [#1395](https://github.com/Ikey168/Noesis/issues/1395) — Execute bounded research cycles and reassess project evidence gaps | [#1394](https://github.com/Ikey168/Noesis/issues/1394), [#1412](https://github.com/Ikey168/Noesis/issues/1412), [#1413](https://github.com/Ikey168/Noesis/issues/1413), [#1441](https://github.com/Ikey168/Noesis/issues/1441), [#1422](https://github.com/Ikey168/Noesis/issues/1422), [#1425](https://github.com/Ikey168/Noesis/issues/1425), [#1427](https://github.com/Ikey168/Noesis/issues/1427) |
| NF-03 | [#1396](https://github.com/Ikey168/Noesis/issues/1396) — Branch research projects and compare investigations against a pinned baseline | [#1394](https://github.com/Ikey168/Noesis/issues/1394), [#1439](https://github.com/Ikey168/Noesis/issues/1439) |

## Reliable production knowledge workflows

Parent: [#1397](https://github.com/Ikey168/Noesis/issues/1397).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| WF-01 | [#1412](https://github.com/Ikey168/Noesis/issues/1412) — Connect production extraction to source-pack maintenance | Independent task. |
| WF-02 | [#1413](https://github.com/Ikey168/Noesis/issues/1413) — Repair index watermark publication during workflow resume | Independent task. |
| WF-03 | [#1441](https://github.com/Ikey168/Noesis/issues/1441) — Add a real-text acceptance run across domain knowledge bases | Depends on: [#1412](https://github.com/Ikey168/Noesis/issues/1412), [#1413](https://github.com/Ikey168/Noesis/issues/1413), [#1422](https://github.com/Ikey168/Noesis/issues/1422), [#1417](https://github.com/Ikey168/Noesis/issues/1417), [#1423](https://github.com/Ikey168/Noesis/issues/1423), [#1416](https://github.com/Ikey168/Noesis/issues/1416) |

## Document identity, citation offsets, domain membership, and entity resolution

Parent: [#1398](https://github.com/Ikey168/Noesis/issues/1398).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| DP-01 | [#1414](https://github.com/Ikey168/Noesis/issues/1414) — Preserve observation identity when deduplicating document content | Related: [#1392](https://github.com/Ikey168/Noesis/issues/1392) |
| DP-02 | [#1415](https://github.com/Ikey168/Noesis/issues/1415) — Carry exact source offsets through chunk splitting | Independent task. |
| DP-03 | [#1416](https://github.com/Ikey168/Noesis/issues/1416) — Reassess domain membership after document revisions | Independent task. |
| ER-01 | [#1421](https://github.com/Ikey168/Noesis/issues/1421) — Abstain on ambiguous entity aliases | Independent task. |

## Revision-safe extraction, enrichment, and model evaluation

Parent: [#1399](https://github.com/Ikey168/Noesis/issues/1399).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| EX-01 | [#1417](https://github.com/Ikey168/Noesis/issues/1417) — Replace mined claims and evidence atomically | Independent task. |
| EX-02 | [#1442](https://github.com/Ikey168/Noesis/issues/1442) — Include extractor/model configuration in mining freshness | Depends on: [#1417](https://github.com/Ikey168/Noesis/issues/1417) |
| EX-03 | [#1418](https://github.com/Ikey168/Noesis/issues/1418) — Retry failed graph projection independently of mining | Independent task. |
| EX-04 | [#1419](https://github.com/Ikey168/Noesis/issues/1419) — Refresh enrichment when content or analyzer changes | Independent task. |
| EX-05 | [#1420](https://github.com/Ikey168/Noesis/issues/1420) — Collect the missing independent human evaluation set | Independent task. |
| EX-06 | [#1443](https://github.com/Ikey168/Noesis/issues/1443) — Improve and calibrate stance classification | Depends on: [#1420](https://github.com/Ikey168/Noesis/issues/1420) |
| EX-07 | [#1444](https://github.com/Ikey168/Noesis/issues/1444) — Improve and calibrate frame classification | Depends on: [#1420](https://github.com/Ikey168/Noesis/issues/1420) |

## Semantic indexing, embedding freshness, and bounded document coverage

Parent: [#1400](https://github.com/Ikey168/Noesis/issues/1400).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| IX-01 | [#1422](https://github.com/Ikey168/Noesis/issues/1422) — Use semantic model embeddings in maintenance projections | Independent task. |
| IX-02 | [#1423](https://github.com/Ikey168/Noesis/issues/1423) — Rebuild direct embeddings after revision or model changes | Independent task. |
| IX-03 | [#1445](https://github.com/Ikey168/Noesis/issues/1445) — Embed complete documents with token-aware chunks | Depends on: [#1415](https://github.com/Ikey168/Noesis/issues/1415), [#1423](https://github.com/Ikey168/Noesis/issues/1423) |
| IX-04 | [#1424](https://github.com/Ikey168/Noesis/issues/1424) — Bound embedding batches and validate provider output | Independent task. |

## Query deadlines, retrieval relevance, and evidence-supported answers

Parent: [#1401](https://github.com/Ikey168/Noesis/issues/1401).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| QA-01 | [#1425](https://github.com/Ikey168/Noesis/issues/1425) — Enforce one end-to-end query deadline | Independent task. |
| QA-02 | [#1426](https://github.com/Ikey168/Noesis/issues/1426) — Benchmark the existing hybrid retrieval and reranking paths | Independent task. |
| QA-03 | [#1427](https://github.com/Ikey168/Noesis/issues/1427) — Evaluate whether answers are supported by their cited evidence | Independent task. |

## Subscription snapshot integrity, event semantics, and reliable delivery

Parent: [#1402](https://github.com/Ikey168/Noesis/issues/1402).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| SU-01 | [#1428](https://github.com/Ikey168/Noesis/issues/1428) — Include coverage in subscription snapshot identity | Independent task. |
| SU-02 | [#1429](https://github.com/Ikey168/Noesis/issues/1429) — Distinguish query-result disappearance from source withdrawal | Independent task. |
| SU-03 | [#1430](https://github.com/Ikey168/Noesis/issues/1430) — Implement the subscription outbox delivery lifecycle | Independent task. |

## Research package verification and durable archive recovery

Parent: [#1403](https://github.com/Ikey168/Noesis/issues/1403).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| PK-01 | [#1431](https://github.com/Ikey168/Noesis/issues/1431) — Recompute research-package dependency closure during verification | Independent task. |
| PK-02 | [#1432](https://github.com/Ikey168/Noesis/issues/1432) — Expose trusted-signature policy at package import | Independent task. |
| RT-01 | [#1433](https://github.com/Ikey168/Noesis/issues/1433) — Write and verify archive bytes through a storage adapter | Independent task. |
| RT-02 | [#1446](https://github.com/Ikey168/Noesis/issues/1446) — Restore from archive into a fresh database | Depends on: [#1433](https://github.com/Ikey168/Noesis/issues/1433) |
| RT-03 | [#1434](https://github.com/Ikey168/Noesis/issues/1434) — Reject or explicitly paginate truncated checkpoints | Independent task. |

## Workflow conformance, migration, and workload validation

Parent: [#1404](https://github.com/Ikey168/Noesis/issues/1404).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| CV-01 | [#1439](https://github.com/Ikey168/Noesis/issues/1439) — Verify namespace and access-policy conformance across query, snapshot, and export surfaces | Independent task. |
| CV-02 | [#1455](https://github.com/Ikey168/Noesis/issues/1455) — Verify retraction and deletion propagation under retention holds and snapshot pins | Depends on: [#1417](https://github.com/Ikey168/Noesis/issues/1417), [#1423](https://github.com/Ikey168/Noesis/issues/1423), [#1416](https://github.com/Ikey168/Noesis/issues/1416), [#1434](https://github.com/Ikey168/Noesis/issues/1434) |
| CV-03 | [#1440](https://github.com/Ikey168/Noesis/issues/1440) — Test workflow migrations and compatibility using prior persisted database fixtures | Independent task. |
| CV-04 | [#1456](https://github.com/Ikey168/Noesis/issues/1456) — Measure sustained workflow throughput and resource limits under recovery and cancellation | Depends on: [#1425](https://github.com/Ikey168/Noesis/issues/1425), [#1424](https://github.com/Ikey168/Noesis/issues/1424), [#1413](https://github.com/Ikey168/Noesis/issues/1413) |

## Living research reports

Parent: [#1405](https://github.com/Ikey168/Noesis/issues/1405).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-04 | [#1447](https://github.com/Ikey168/Noesis/issues/1447) — Store report sections with exact evidence dependencies | Depends on: [#1415](https://github.com/Ikey168/Noesis/issues/1415), [#1431](https://github.com/Ikey168/Noesis/issues/1431) |
| NF-05 | [#1457](https://github.com/Ikey168/Noesis/issues/1457) — Detect which report conclusions are affected by new evidence | Depends on: [#1447](https://github.com/Ikey168/Noesis/issues/1447), [#1417](https://github.com/Ikey168/Noesis/issues/1417), [#1419](https://github.com/Ikey168/Noesis/issues/1419), [#1423](https://github.com/Ikey168/Noesis/issues/1423) |
| NF-06 | [#1463](https://github.com/Ikey168/Noesis/issues/1463) — Draft and review report revisions | Depends on: [#1457](https://github.com/Ikey168/Noesis/issues/1457), [#1427](https://github.com/Ikey168/Noesis/issues/1427) |

## Unified evidence review inbox

Parent: [#1406](https://github.com/Ikey168/Noesis/issues/1406).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-07 | [#1435](https://github.com/Ikey168/Noesis/issues/1435) — Aggregate review candidates with an explainable priority | Independent task. |
| NF-08 | [#1448](https://github.com/Ikey168/Noesis/issues/1448) — Route review decisions back to authoritative ledgers | Depends on: [#1435](https://github.com/Ikey168/Noesis/issues/1435), [#1421](https://github.com/Ikey168/Noesis/issues/1421) |
| NF-09 | [#1458](https://github.com/Ikey168/Noesis/issues/1458) — Export reviewed corrections as evaluation/training candidates | Depends on: [#1448](https://github.com/Ikey168/Noesis/issues/1448), [#1420](https://github.com/Ikey168/Noesis/issues/1420) |

## Systematic literature review workflow

Parent: [#1407](https://github.com/Ikey168/Noesis/issues/1407).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-10 | [#1449](https://github.com/Ikey168/Noesis/issues/1449) — Version review protocols and literature search runs | Depends on: [#1414](https://github.com/Ikey168/Noesis/issues/1414) |
| NF-11 | [#1459](https://github.com/Ikey168/Noesis/issues/1459) — Support staged screening and adjudication | Depends on: [#1449](https://github.com/Ikey168/Noesis/issues/1449), [#1420](https://github.com/Ikey168/Noesis/issues/1420); Related: [#1361](https://github.com/Ikey168/Noesis/issues/1361), [#1362](https://github.com/Ikey168/Noesis/issues/1362), [#1364](https://github.com/Ikey168/Noesis/issues/1364) |
| NF-12 | [#1464](https://github.com/Ikey168/Noesis/issues/1464) — Export evidence tables and selection-flow counts | Depends on: [#1459](https://github.com/Ikey168/Noesis/issues/1459), [#1415](https://github.com/Ikey168/Noesis/issues/1415) |

## Zotero and bibliography interoperability

Parent: [#1408](https://github.com/Ikey168/Noesis/issues/1408).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-13 | [#1450](https://github.com/Ikey168/Noesis/issues/1450) — Import Zotero collections with stable identity | Depends on: [#1414](https://github.com/Ikey168/Noesis/issues/1414); Related: [#1394](https://github.com/Ikey168/Noesis/issues/1394) |
| NF-14 | [#1460](https://github.com/Ikey168/Noesis/issues/1460) — Track incremental library changes and annotation provenance | Depends on: [#1450](https://github.com/Ikey168/Noesis/issues/1450), [#1415](https://github.com/Ikey168/Noesis/issues/1415) |
| NF-15 | [#1451](https://github.com/Ikey168/Noesis/issues/1451) — Export bibliographies alongside evidence-rich reports | Depends on: [#1431](https://github.com/Ikey168/Noesis/issues/1431); Related: [#1394](https://github.com/Ikey168/Noesis/issues/1394) |

## Forecast registration and outcome tracking

Parent: [#1409](https://github.com/Ikey168/Noesis/issues/1409).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-16 | [#1436](https://github.com/Ikey168/Noesis/issues/1436) — Register time-bounded forecast questions | Independent task. |
| NF-17 | [#1452](https://github.com/Ikey168/Noesis/issues/1452) — Propose and review forecast resolutions | Depends on: [#1436](https://github.com/Ikey168/Noesis/issues/1436) |
| NF-18 | [#1461](https://github.com/Ikey168/Noesis/issues/1461) — Report forecast calibration and performance by domain | Depends on: [#1436](https://github.com/Ikey168/Noesis/issues/1436), [#1452](https://github.com/Ikey168/Noesis/issues/1452) |

## Reproducible analysis notebooks

Parent: [#1410](https://github.com/Ikey168/Noesis/issues/1410).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-19 | [#1437](https://github.com/Ikey168/Noesis/issues/1437) — Define notebook analysis manifests over pinned inputs | Independent task. |
| NF-20 | [#1453](https://github.com/Ikey168/Noesis/issues/1453) — Execute configured notebooks with bounded resources | Depends on: [#1437](https://github.com/Ikey168/Noesis/issues/1437), [#1425](https://github.com/Ikey168/Noesis/issues/1425) |
| NF-21 | [#1462](https://github.com/Ikey168/Noesis/issues/1462) — Attach analysis outputs to findings and research packages | Depends on: [#1453](https://github.com/Ikey168/Noesis/issues/1453), [#1431](https://github.com/Ikey168/Noesis/issues/1431) |

## Evidence-linked decision records

Parent: [#1411](https://github.com/Ikey168/Noesis/issues/1411).

| Review task | Issue | Prerequisites / related work |
|---|---|---|
| NF-22 | [#1438](https://github.com/Ikey168/Noesis/issues/1438) — Store decision alternatives and explicit criteria | Related: [#1394](https://github.com/Ikey168/Noesis/issues/1394) |
| NF-23 | [#1454](https://github.com/Ikey168/Noesis/issues/1454) — Compare options under declared assumption changes | Depends on: [#1438](https://github.com/Ikey168/Noesis/issues/1438) |
| NF-24 | [#1465](https://github.com/Ikey168/Noesis/issues/1465) — Trigger decision reviews when dependencies change | Depends on: [#1438](https://github.com/Ikey168/Noesis/issues/1438), [#1457](https://github.com/Ikey168/Noesis/issues/1457), [#1448](https://github.com/Ikey168/Noesis/issues/1448), [#1428](https://github.com/Ikey168/Noesis/issues/1428), [#1429](https://github.com/Ikey168/Noesis/issues/1429), [#1427](https://github.com/Ikey168/Noesis/issues/1427) |

## Implementation order

Start with claim preservation, watermark recovery, canonical source identity, correct citation offsets, checkpoint completeness, and package dependency verification. Then propagate revisions and connect production extraction/semantic indexing. Project state, report state, and inbox foundations can advance alongside these fixes; their production execution and evaluation retain explicit readiness dependencies.

Library/API evaluations remain scoped inside the applicable tasks: Splink, Sentence Transformers evaluators, ir-measures, fsspec, ASReview, PRISMA reporting, Zotero, and nbclient. Existing source-collection library/API evaluations were not duplicated.

Issue creation and linking were verified against GitHub. No production code implementation, model training, paid API execution, or deployment was performed by this publication step.
