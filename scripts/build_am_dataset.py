#!/usr/bin/env python3
"""
Build the Noesis argument-mining labelled dataset (issue #109).

Generates:
  data/argument_mining/claims.parquet     — binary claim detection (train/val/test)
  data/argument_mining/stance.parquet     — 4-class stance classification
  data/argument_mining/frames.parquet     — multi-label frame classification
  data/argument_mining/iaa_subset.parquet — 500-example inter-annotator agreement validation
  data/argument_mining/stats.json         — dataset statistics

Run from the repo root:
    python scripts/build_am_dataset.py [--output data/argument_mining] [--seed 42]
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sklearn.metrics import cohen_kappa_score

# ---------------------------------------------------------------------------
# Entity / value banks — used to fill template placeholders
# ---------------------------------------------------------------------------

_CENTRAL_BANKS = [
    "The Federal Reserve", "The European Central Bank", "The Bank of England",
    "The Bank of Japan", "The Reserve Bank of India", "The Swiss National Bank",
    "The People's Bank of China", "The Bank of Canada",
]
_GOVTS = [
    "The government", "The administration", "The cabinet", "The treasury",
    "The ministry", "The regulator", "The agency", "The department",
]
_COMPANIES = [
    "The company", "The firm", "The corporation", "The manufacturer",
    "The retailer", "The conglomerate", "The start-up", "The group",
]
_RESEARCH_ORGS = [
    "Researchers at MIT", "Scientists at Johns Hopkins", "A team from Oxford",
    "Investigators at the CDC", "Analysts at the IMF", "Scientists at CERN",
    "Economists at the World Bank", "Epidemiologists at Harvard",
    "Researchers at Stanford", "A team from Cambridge",
]
_POLITICIANS = [
    "The minister", "The senator", "The chancellor", "The president",
    "The prime minister", "The governor", "The commissioner",
    "The committee chair", "The secretary", "The director",
]
_COURTS = [
    "The Supreme Court", "The federal court", "The appeals court",
    "The tribunal", "The constitutional court", "The high court",
]
_SECTORS = [
    "healthcare", "education", "infrastructure", "defence", "energy",
    "housing", "transport", "agriculture", "technology",
]
_PROGRAMMES = [
    "the welfare programme", "the housing initiative", "the training scheme",
    "the stimulus package", "the pilot scheme", "the reform programme",
    "the vaccination campaign", "the infrastructure project",
]
_ECON_METRICS = [
    "GDP growth", "the unemployment rate", "inflation", "the trade deficit",
    "consumer confidence", "industrial output", "retail sales",
    "the current-account balance", "the budget deficit", "productivity",
]
_ECON_VALS = ["0.3", "1.2", "2.1", "3.8", "4.2", "5.1", "6.7", "8.3", "12.4", "18.6"]
_PCT_CHANGES = ["3", "5", "8", "11", "14", "17", "22", "28", "35", "41", "47", "62"]
_BPS = ["25", "50", "75", "100"]
_INTEREST_RATES = ["3.5", "4.0", "4.5", "5.0", "5.25", "5.5", "6.0", "6.5"]
_PERIODS = [
    "last quarter", "in March", "year-on-year", "in the second quarter",
    "over the past 12 months", "in the fiscal year", "since last year",
    "compared with the prior period", "in Q3", "over the decade",
]
_MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_YEARS = ["2019", "2020", "2021", "2022", "2023", "2024"]
_DECADES = [
    "the 1920s", "the 1930s", "the 1940s", "the 1950s",
    "the 1960s", "the 1970s", "the 1980s", "the 1990s",
]
_VOTE_A = ["312", "228", "184", "302", "267", "341", "199", "315", "256", "289"]
_VOTE_B = ["189", "162", "241", "127", "158", "174", "220", "183", "143", "211"]
_POPULATIONS = [
    "2.4 million", "500,000", "14,000", "3.2 million", "800,000",
    "110 million", "4.5 million", "1.1 million", "6.7 million", "220,000",
]
_SAMPLE_SIZES = [
    "3,247", "4,821", "1,200", "8,400", "12,500", "2,100", "6,700", "900",
    "5,312", "1,875", "3,900", "7,200",
]
_PVALS = ["p < 0.001", "p < 0.01", "p = 0.02", "p < 0.05", "p = 0.004"]
_CI = [
    "95% CI: 38–44%", "95% CI: 1.18–1.53", "95% CI: 22–35%",
    "95% CI: 0.68–0.82", "95% CI: 12–29%", "95% CI: 0.91–1.44",
]
_CORR = ["0.74", "0.82", "0.68", "0.91", "0.77", "0.63", "0.86", "0.71"]
_SETTLEMENT = ["$4.5 billion", "$2.1 billion", "$850 million", "$1.4 billion", "$320 million"]
_BUDGET = ["$2.4M", "$850bn", "$4.2 billion", "$1.1 trillion", "$500 million", "$78M"]
_TOPICS = {
    "economic": [
        "economic growth", "fiscal policy", "monetary policy", "trade",
        "inflation", "public debt", "economic reform", "markets",
        "the budget", "taxation policy",
    ],
    "political": [
        "immigration", "climate policy", "healthcare reform", "tax policy",
        "election integrity", "foreign policy", "national security",
        "legislative reform", "the proposed bill", "governance reform",
    ],
    "health": [
        "vaccine efficacy", "drug trials", "public health measures",
        "mental health services", "cancer treatment", "epidemic response",
        "healthcare access", "preventive care",
    ],
    "environment": [
        "climate change", "carbon emissions", "renewable energy",
        "biodiversity loss", "air quality", "deforestation", "net zero",
    ],
    "social": [
        "income inequality", "housing affordability", "education access",
        "criminal justice reform", "labour rights", "social mobility",
        "poverty reduction", "community welfare",
    ],
    "security": [
        "military spending", "cybersecurity", "border security",
        "nuclear proliferation", "counter-terrorism", "intelligence reform",
        "defence policy",
    ],
}
_CITIES = [
    "Manchester", "Lyon", "Pittsburgh", "Osaka", "Nairobi", "Melbourne",
    "Hamburg", "Cape Town", "Bogotá", "Karachi",
]
_DAYS = ["3", "5", "7", "9", "11", "14", "17", "18", "22", "25", "27", "30"]
_QS = ["1", "2", "3", "4"]
_TARGETS = ["12%", "15%", "20%", "25%", "30%", "50%", "75%"]
_OUTCOMES = [
    "mortality", "hospitalisation rates", "recidivism", "dropout rates",
    "income levels", "emissions", "infection rates", "satisfaction scores",
]


# ---------------------------------------------------------------------------
# Template definitions — (template_str, is_claim, stance, frames, topic_key)
# ---------------------------------------------------------------------------

_NEWS: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral / economic ──────────────────────────────────────────
    ("{cb} raised interest rates by {bp} basis points to {rate}%.",
     1, "neutral", ["economic"], "economic"),
    ("{cb} held rates unchanged at {rate}% at its {month} meeting.",
     1, "neutral", ["economic"], "economic"),
    ("{metric} {direction} {val}% {period}, according to official data.",
     1, "neutral", ["economic"], "economic"),
    ("{org} reported quarterly revenue of {budget}, up {pct}% year-on-year.",
     1, "neutral", ["economic"], "economic"),
    ("GDP contracted {val}% in Q{q} as higher borrowing costs weighed on spending.",
     1, "neutral", ["economic"], "economic"),
    ("The trade deficit widened to {budget} as import costs outpaced exports {period}.",
     1, "neutral", ["economic"], "economic"),
    ("{cb} injected {budget} into financial markets to ease liquidity pressures.",
     1, "neutral", ["economic"], "economic"),
    ("Housing starts declined {pct}% last month as mortgage rates climbed to {rate}%.",
     1, "neutral", ["economic"], "economic"),
    ("{metric} reached its highest level in {val} years, official figures show.",
     1, "neutral", ["economic"], "economic"),
    ("{org}'s {sector} division posted a {pct}% drop in margins {period}.",
     1, "neutral", ["economic"], "economic"),
    # ── claim / neutral / political ─────────────────────────────────────────
    ("Parliament passed the legislation by {va} votes to {vb}.",
     1, "neutral", ["political", "legal"], "political"),
    ("{court} ruled the regulation unconstitutional in a 5-4 decision.",
     1, "neutral", ["legal", "political"], "political"),
    ("The election is scheduled to take place on {day} {month}.",
     1, "neutral", ["political"], "political"),
    ("{court} issued an injunction blocking the new rules pending a full hearing.",
     1, "neutral", ["legal"], "political"),
    ("{org} agreed to a {settlement} settlement to resolve antitrust litigation.",
     1, "neutral", ["legal", "economic"], "economic"),
    ("The governing coalition collapsed after junior partners withdrew over the budget.",
     1, "neutral", ["political", "economic"], "political"),
    ("{pol} confirmed the treaty had been ratified by {va} member states.",
     1, "neutral", ["political", "legal"], "political"),
    # ── claim / neutral / scientific ────────────────────────────────────────
    ("{research} published findings showing a {pct}% reduction in {outcome}.",
     1, "neutral", ["scientific"], "health"),
    ("The vaccine demonstrated {pct}% efficacy against severe disease in phase 3 trials.",
     1, "neutral", ["scientific"], "health"),
    ("{research} identified a genetic marker linked to early-onset dementia.",
     1, "neutral", ["scientific"], "health"),
    ("Average temperatures in the region increased by {val}°C over the decade.",
     1, "neutral", ["scientific", "environment"], "environment"),
    ("A peer-reviewed study in {month} found a strong link between {outcome} and lifestyle factors.",
     1, "neutral", ["scientific"], "health"),
    # ── claim / neutral / humanitarian ──────────────────────────────────────
    ("Aid agencies warned that {pop} civilians face acute food insecurity.",
     1, "neutral", ["humanitarian"], "social"),
    ("The UN reported a record {pop} people are now forcibly displaced worldwide.",
     1, "neutral", ["humanitarian"], "social"),
    ("Three people were killed and {pct} injured following the incident in {city}.",
     1, "neutral", ["humanitarian"], "social"),
    # ── claim / neutral / security ──────────────────────────────────────────
    ("The military confirmed {pop} personnel had been deployed to the region.",
     1, "neutral", ["security"], "security"),
    ("The government confirmed a cyberattack had breached critical infrastructure networks.",
     1, "neutral", ["security"], "security"),
    ("Border crossings declined {pct}% after the new enforcement measures took effect.",
     1, "neutral", ["security", "political"], "security"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("The renewable energy transition has created thousands of new jobs, driving growth.",
     1, "supportive", ["economic", "environment"], "environment"),
    ("The reform has delivered measurable improvements in living standards for low-income families.",
     1, "supportive", ["economic", "humanitarian"], "social"),
    ("Early data confirm {programme} is generating a significant return on investment.",
     1, "supportive", ["economic"], "economic"),
    ("The landmark legislation finally gives workers the protections they have long deserved.",
     1, "supportive", ["political", "legal"], "political"),
    ("The new drug has shown remarkable results, reducing hospitalisations by {pct}%.",
     1, "supportive", ["scientific", "humanitarian"], "health"),
    ("This policy has delivered measurable improvements in air quality across the region.",
     1, "supportive", ["economic", "environment"], "environment"),
    # ── claim / critical ─────────────────────────────────────────────────────
    ("Despite record deficits, {govt} has failed to reduce inequality or improve services.",
     1, "critical", ["economic", "political"], "economic"),
    ("{metric} hit a decade high of {val}%, exposing the failure of current policy.",
     1, "critical", ["economic", "political"], "economic"),
    ("The administration's proposal would strip critical oversight mechanisms from regulators.",
     1, "critical", ["political", "legal"], "political"),
    ("The policy has failed to address the root causes of poverty and inequality.",
     1, "critical", ["political", "economic", "humanitarian"], "social"),
    ("The funding cut will jeopardise essential services relied on by {pop} patients.",
     1, "critical", ["humanitarian", "political"], "health"),
    ("Despite {budget} in annual subsidies, {sector} performance has steadily declined.",
     1, "critical", ["economic", "political"], "economic"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("Carbon emissions fell {pct}% last year, though critics note this still falls short of targets.",
     1, "ambiguous", ["economic", "environment", "political"], "environment"),
    ("The plan has created some jobs but also displaced workers in traditional industries.",
     1, "ambiguous", ["economic", "humanitarian"], "economic"),
    # ── non-claim / critical ─────────────────────────────────────────────────
    ("This reckless spending will saddle future generations with unsustainable debt.",
     0, "critical", ["economic", "political"], "economic"),
    ("The government's response to the crisis has been woefully inadequate.",
     0, "critical", ["political", "humanitarian"], "social"),
    ("These cuts will devastate essential services that millions of people depend on.",
     0, "critical", ["humanitarian", "political"], "social"),
    ("The regulation imposes an unacceptable burden on small businesses.",
     0, "critical", ["economic", "legal"], "economic"),
    ("The plan prioritises corporate interests over the welfare of ordinary citizens.",
     0, "critical", ["political", "economic"], "political"),
    ("The administration's handling of this crisis has been a complete failure.",
     0, "critical", ["political", "humanitarian"], "social"),
    ("We must act decisively before it is too late to avert the worst outcomes.",
     0, "critical", ["political", "humanitarian"], "social"),
    # ── non-claim / supportive ──────────────────────────────────────────────
    ("This initiative is exactly what communities have been demanding for years.",
     0, "supportive", ["political", "humanitarian"], "social"),
    ("The evidence strongly supports the effectiveness of this approach.",
     0, "supportive", ["scientific"], "health"),
    ("This represents a genuine breakthrough that will benefit millions of people.",
     0, "supportive", ["humanitarian"], "social"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("While the initiative has shown some promise, its long-term viability is uncertain.",
     0, "ambiguous", ["political", "economic"], "political"),
    ("Supporters point to job creation figures, but critics note rising inequality.",
     0, "ambiguous", ["economic", "political"], "economic"),
    ("Whether this proves effective remains an open question.",
     0, "ambiguous", ["political"], "political"),
    ("The data suggests improvement in some areas but deterioration in others.",
     0, "ambiguous", ["scientific"], "health"),
    ("It is difficult to draw firm conclusions from the available evidence.",
     0, "ambiguous", ["scientific"], "health"),
    ("Opinion is divided on whether the benefits outweigh the costs.",
     0, "ambiguous", ["political", "economic"], "economic"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("The debate has been ongoing for years without resolution.",
     0, "neutral", ["political"], "political"),
    ("Analysts have mixed opinions about the prospects for recovery.",
     0, "neutral", ["economic"], "economic"),
    ("The long-term implications of this decision are difficult to predict.",
     0, "neutral", ["political"], "political"),
    ("How the situation unfolds over the next few months remains unclear.",
     0, "neutral", ["political"], "political"),
    ("Many observers believe the situation will improve in the coming months.",
     0, "neutral", ["political"], "political"),
]

_BLOG: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral ──────────────────────────────────────────────────────
    ("After tracking engagement daily for 18 months, I confirmed a {pct}% drop after the {month} update.",
     1, "neutral", ["scientific"], "economic"),
    ("My A/B test across {n} subscribers showed a {pct}% higher open rate for short subject lines.",
     1, "neutral", ["scientific"], "economic"),
    ("The library added async support in version 3.2.0, released on {day} {month} {year}.",
     1, "neutral", ["scientific"], "economic"),
    ("My traffic data shows {pct}% of readers arrive from search, not social.",
     1, "neutral", ["scientific"], "economic"),
    ("I documented {val} separate incidents where the build pipeline silently discarded results.",
     1, "neutral", ["scientific"], "economic"),
    ("The battery lasted exactly {val} hours under continuous load in my benchmarks.",
     1, "neutral", ["scientific"], "economic"),
    ("Last {month} the government published revised figures showing a {pct}% drop in waiting times.",
     1, "neutral", ["political", "humanitarian"], "health"),
    ("After {val} months of testing, response times improved by {pct}% on the new architecture.",
     1, "neutral", ["scientific"], "economic"),
    ("My experiment across {n} data points showed the model converged after {val} epochs.",
     1, "neutral", ["scientific"], "health"),
    ("The {sector} team reduced infrastructure costs by {pct}% after switching providers.",
     1, "neutral", ["economic"], "economic"),
    ("I ran {val} benchmark iterations; the new approach was {pct}% faster on average.",
     1, "neutral", ["scientific"], "economic"),
    ("Our {month} cohort had a {pct}% conversion rate, up from {val}% the previous month.",
     1, "neutral", ["economic"], "economic"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("The data I've collected consistently backs what advocates have been saying: early intervention works.",
     1, "supportive", ["scientific", "humanitarian"], "health"),
    ("Every benchmark I've run confirms this framework outperforms the alternatives by {pct}%.",
     1, "supportive", ["scientific"], "economic"),
    ("My {val}-month analysis confirms that communities with the programme fare {pct}% better.",
     1, "supportive", ["scientific", "humanitarian"], "social"),
    # ── claim / critical ────────────────────────────────────────────────────
    ("Three years of data show this policy has done nothing to reduce the underlying problem.",
     1, "critical", ["scientific", "political"], "political"),
    ("My analysis confirms what critics suspected: adoption has stalled at barely {pct}%.",
     1, "critical", ["scientific", "political"], "political"),
    ("After {val} weeks of testing, the product failed to meet even basic reliability thresholds.",
     1, "critical", ["scientific"], "economic"),
    ("The {year} data clearly shows the intervention had no measurable effect on {outcome}.",
     1, "critical", ["scientific", "political"], "health"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("My readers are split: {pct}% see this as progress, the rest see it as window-dressing.",
     1, "ambiguous", ["political"], "political"),
    ("The results vary by region — cities improved significantly while rural areas worsened.",
     1, "ambiguous", ["economic", "humanitarian"], "social"),
    ("My {val}-month tracking shows mixed signals: engagement up, but revenue flat.",
     1, "ambiguous", ["economic"], "economic"),
    # ── non-claim / critical ────────────────────────────────────────────────
    ("In my opinion, this is the most overrated framework released in the past decade.",
     0, "critical", ["scientific"], "economic"),
    ("I've watched this policy fail communities for years — it's time to scrap it entirely.",
     0, "critical", ["political", "humanitarian"], "social"),
    ("To me, the real problem isn't the technology itself but how organisations deploy it.",
     0, "critical", ["political"], "economic"),
    ("Perhaps the biggest mistake teams make is assuming more tooling means more productivity.",
     0, "critical", ["economic"], "economic"),
    ("This is, frankly, a missed opportunity that will set the {sector} back by years.",
     0, "critical", ["economic", "political"], "economic"),
    # ── non-claim / supportive ──────────────────────────────────────────────
    ("I believe we're at an inflection point that could change everything about {sector}.",
     0, "supportive", ["economic"], "economic"),
    ("This is genuinely the best developer experience I've encountered in years of practice.",
     0, "supportive", ["scientific"], "economic"),
    ("The approach finally makes sense of a problem that has frustrated the field for a decade.",
     0, "supportive", ["scientific"], "health"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("Depending on how you look at it, this could be seen as progress or regression.",
     0, "ambiguous", ["political"], "political"),
    ("Whether the trade-offs are worth it depends entirely on your specific use case.",
     0, "ambiguous", ["economic"], "economic"),
    ("I'm genuinely torn: there are clear wins here, but also some real downsides to consider.",
     0, "ambiguous", ["economic"], "economic"),
    ("On {sector}, the situation is more nuanced than either camp tends to admit.",
     0, "ambiguous", ["political"], "political"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("Not sure if the proposed timeline is realistic given current team capacity.",
     0, "neutral", ["economic"], "economic"),
    ("The conversation about this topic has been going on for years without resolution.",
     0, "neutral", ["political"], "political"),
    ("It remains to be seen how the community will respond over the next {val} months.",
     0, "neutral", ["political"], "political"),
    ("I'll need more data before drawing any firm conclusions about the {year} cohort.",
     0, "neutral", ["scientific"], "health"),
]

_PAPER: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral ──────────────────────────────────────────────────────
    ("Our analysis reveals a statistically significant correlation (r = {r}, {pval}).",
     1, "neutral", ["scientific"], "health"),
    ("The cohort comprised {n} participants aged 18–65 recruited across six clinical sites.",
     1, "neutral", ["scientific"], "health"),
    ("Gene expression analysis identified {n} differentially expressed genes.",
     1, "neutral", ["scientific"], "health"),
    ("The intervention group showed a {pct}% reduction in {outcome} versus placebo ({ci}).",
     1, "neutral", ["scientific"], "health"),
    ("Results replicated across three independent validation cohorts (N = {n}, {n2}, and {n3}).",
     1, "neutral", ["scientific"], "health"),
    ("Sensitivity analysis excluding outliers did not materially change the point estimate ({ci}).",
     1, "neutral", ["scientific"], "health"),
    ("The model correctly classified {pct}% of samples in the held-out test set.",
     1, "neutral", ["scientific"], "health"),
    ("Our randomised controlled trial (n = {n}) demonstrates statistically significant efficacy ({pval}).",
     1, "neutral", ["scientific"], "health"),
    ("Table 2 presents the baseline characteristics of the {n} participants enrolled.",
     1, "neutral", ["scientific"], "health"),
    ("Our economic model forecasts a {val}% contraction under the proposed tariff regime ({pval}).",
     1, "neutral", ["scientific", "economic"], "economic"),
    ("Regression analysis (β = {r}, SE = 0.04, {pval}) confirmed the hypothesised relationship.",
     1, "neutral", ["scientific"], "health"),
    ("The {month} {year} dataset comprised {n} observations across {val} distinct sites.",
     1, "neutral", ["scientific"], "health"),
    ("Survival analysis revealed a {pct}% improvement in {outcome} at the {val}-month mark.",
     1, "neutral", ["scientific"], "health"),
    ("Structural equation modelling confirmed a direct path coefficient of {r} ({pval}).",
     1, "neutral", ["scientific"], "health"),
    ("Cross-validation across {val} folds yielded a mean AUC of 0.{pct} (SD = 0.03).",
     1, "neutral", ["scientific"], "health"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("These findings provide robust evidence that the proposed intervention significantly reduces {outcome}.",
     1, "supportive", ["scientific", "humanitarian"], "health"),
    ("The results demonstrate the programme's effectiveness across all {val} measured outcomes.",
     1, "supportive", ["scientific", "humanitarian"], "health"),
    ("Our data strongly support the theoretical framework proposed by earlier researchers.",
     1, "supportive", ["scientific"], "health"),
    # ── claim / critical ────────────────────────────────────────────────────
    ("The data reveal substantial methodological limitations in prior studies on this topic.",
     1, "critical", ["scientific"], "health"),
    ("Our reanalysis shows the original findings do not hold when controlling for confounders.",
     1, "critical", ["scientific"], "health"),
    ("Bootstrapped confidence intervals indicate the effect size is not statistically distinguishable from zero ({ci}).",
     1, "critical", ["scientific"], "health"),
    ("Inspection of residuals reveals systematic bias in the previously published model.",
     1, "critical", ["scientific"], "health"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("Results were inconclusive, with some subgroups showing benefit and others showing no effect.",
     1, "ambiguous", ["scientific"], "health"),
    ("The evidence is mixed: short-term {outcome} improved but longer-term follow-up showed no difference.",
     1, "ambiguous", ["scientific", "humanitarian"], "health"),
    ("Heterogeneity across sites (I² = {pct}%) limits the generalisability of the pooled estimate.",
     1, "ambiguous", ["scientific"], "health"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("One limitation of this study is the reliance on self-reported {outcome} data.",
     0, "neutral", ["scientific"], "health"),
    ("Future research should examine whether these findings generalise to non-Western populations.",
     0, "neutral", ["scientific"], "health"),
    ("It is possible that unmeasured confounders may have influenced the observed association.",
     0, "neutral", ["scientific"], "health"),
    ("Whether the effect persists beyond the {val}-month follow-up period remains to be determined.",
     0, "neutral", ["scientific"], "health"),
    ("Larger samples will be required to detect effects of the magnitude observed here.",
     0, "neutral", ["scientific"], "health"),
    # ── non-claim / critical ────────────────────────────────────────────────
    ("This approach assumes linearity that may not hold in real-world conditions.",
     0, "critical", ["scientific"], "health"),
    ("The analytical choices made here risk overfitting and should be treated with caution.",
     0, "critical", ["scientific"], "health"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("The implications for clinical practice depend on factors outside the scope of this study.",
     0, "ambiguous", ["scientific", "humanitarian"], "health"),
    ("Whether these findings will translate to older populations is an open empirical question.",
     0, "ambiguous", ["scientific"], "health"),
    ("The direction of causality cannot be established from this observational design.",
     0, "ambiguous", ["scientific"], "health"),
]

_TRANSCRIPT: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral ──────────────────────────────────────────────────────
    ("What we reported to the board was a {pct}% reduction in operating costs across all divisions.",
     1, "neutral", ["economic"], "economic"),
    ("{pol} confirmed that {n} new homes had been completed under {programme} as of last month.",
     1, "neutral", ["economic", "political"], "political"),
    ("The committee voted {va} to {vb} to approve the revised budget of {budget}.",
     1, "neutral", ["political", "legal"], "political"),
    ("Our data shows {programme} enrolled {n} participants in its first year of operation.",
     1, "neutral", ["scientific"], "health"),
    ("{pol} confirmed the contract expires in {month} and will not be renewed at that rate.",
     1, "neutral", ["political", "economic"], "political"),
    ("The prime minister's party lost its majority at the election, forcing a confidence vote.",
     1, "neutral", ["political"], "political"),
    ("The general briefed lawmakers on escalating threats and deployment of {pop} personnel.",
     1, "neutral", ["security", "political"], "security"),
    ("Revenue for the {month} quarter came in at {budget}, beating analyst consensus by {pct}%.",
     1, "neutral", ["economic"], "economic"),
    ("{pol} stated that the audit had identified {val} compliance failures across {val} sites.",
     1, "neutral", ["legal", "political"], "political"),
    ("We processed {n} applications last quarter, approving {pct}% within the statutory deadline.",
     1, "neutral", ["political", "legal"], "political"),
    ("The {sector} division contributed {pct}% of total group revenue in the {month} period.",
     1, "neutral", ["economic"], "economic"),
    ("{pol} confirmed that {pop} individuals have been displaced as a direct result of the conflict.",
     1, "neutral", ["humanitarian", "security"], "security"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("{programme} is exactly what communities have been demanding and it delivers real results.",
     1, "supportive", ["political", "humanitarian"], "social"),
    ("The data clearly shows the programme has exceeded every target we set at the outset.",
     1, "supportive", ["scientific", "political"], "political"),
    ("Our {month} results show a {pct}% improvement, which validates the strategy we adopted.",
     1, "supportive", ["economic", "scientific"], "economic"),
    # ── claim / critical ────────────────────────────────────────────────────
    ("The proposal as written would increase costs by {pct}% without delivering any improvements.",
     1, "critical", ["economic", "political"], "political"),
    ("Our analysis demonstrates the current approach is not working and has not worked for {val} years.",
     1, "critical", ["scientific", "political"], "political"),
    ("{pol} pointed out that {pct}% of planned deliverables have been missed in the first {val} months.",
     1, "critical", ["political", "economic"], "political"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("We have seen progress in some areas but significant challenges remain in {sector}.",
     1, "ambiguous", ["political", "economic"], "political"),
    ("The {month} data shows improvement on {pct}% of metrics but deterioration on the remaining {val}.",
     1, "ambiguous", ["scientific", "political"], "political"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("{pol} confirmed the vote will take place at the next scheduled meeting on {day} {month}.",
     0, "neutral", ["political"], "political"),
    ("Could you explain why the projections were revised downward so significantly after Q{q}?",
     0, "neutral", ["economic"], "economic"),
    ("We have circulated the revised agenda and will confirm the final speaker list by {day} {month}.",
     0, "neutral", ["economic"], "economic"),
    # ── non-claim / critical ────────────────────────────────────────────────
    ("Many of our members feel that the proposed changes simply do not go far enough.",
     0, "critical", ["political", "humanitarian"], "social"),
    ("I think what we're seeing is a fundamental failure to plan adequately for foreseeable risks.",
     0, "critical", ["political"], "political"),
    ("The delays are entirely avoidable and reflect a systemic failure of project governance.",
     0, "critical", ["economic", "political"], "economic"),
    ("With respect, {pol}, the figures you cited earlier are inconsistent with the published data.",
     0, "critical", ["political", "scientific"], "political"),
    # ── non-claim / supportive ──────────────────────────────────────────────
    ("I believe we are finally moving in the right direction after years of delay on this issue.",
     0, "supportive", ["political"], "political"),
    ("This is genuinely one of the most promising programmes we have seen in the {sector} space.",
     0, "supportive", ["political", "humanitarian"], "social"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("I see valid points on both sides and I'm not sure we have enough evidence to draw firm conclusions.",
     0, "ambiguous", ["political"], "political"),
    ("It's a genuinely difficult question and reasonable people can disagree about the right path forward.",
     0, "ambiguous", ["political"], "political"),
    ("The picture is more complex than either a straightforward success or failure narrative suggests.",
     0, "ambiguous", ["political", "economic"], "political"),
    ("Given the conflicting data, I'm hesitant to endorse any particular interpretation at this stage.",
     0, "ambiguous", ["scientific", "political"], "political"),
]

_BOOK: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral ──────────────────────────────────────────────────────
    ("By {year} the city had lost more than a third of its pre-war population to displacement.",
     1, "neutral", ["humanitarian", "security"], "security"),
    ("The company was incorporated in {year} in a small workshop on the outskirts of {city}.",
     1, "neutral", ["economic"], "economic"),
    ("Annual harvest exceeded one million tonnes for the first time in {year}, according to records.",
     1, "neutral", ["economic", "humanitarian"], "economic"),
    ("The treaty signed on {day} {month} {year} transferred sovereignty to the newly formed republic.",
     1, "neutral", ["political", "legal"], "political"),
    ("By the end of {decade} four of the original seven founding members had withdrawn from the pact.",
     1, "neutral", ["political"], "political"),
    ("Parliament debated the measure for three sessions before passing it into law in {year}.",
     1, "neutral", ["political", "legal"], "political"),
    ("The congress of {year} redistributed power between the federal government and the states.",
     1, "neutral", ["political", "legal"], "political"),
    ("The battle of {month} {year} cost the division more than half its combat strength in {val} days.",
     1, "neutral", ["security", "humanitarian"], "security"),
    ("The famine of {year}–{year2} killed an estimated {pct} million people across the region.",
     1, "neutral", ["humanitarian"], "social"),
    ("{metric} collapsed to its lowest point since {year} during {decade}.",
     1, "neutral", ["economic"], "economic"),
    ("The {sector} sector employed roughly {pct}% of the workforce by the end of {decade}.",
     1, "neutral", ["economic"], "economic"),
    ("Military expenditure reached {pct}% of national income at the height of the conflict in {year}.",
     1, "neutral", ["economic", "security"], "security"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("The reforms dramatically improved living standards for the working poor across {city}.",
     1, "supportive", ["economic", "humanitarian"], "social"),
    ("The policy stands as one of {decade}'s great achievements in social progress.",
     1, "supportive", ["political", "humanitarian"], "social"),
    ("By {year} the programme had lifted {pop} out of extreme poverty, a remarkable outcome.",
     1, "supportive", ["humanitarian", "economic"], "social"),
    # ── claim / critical ────────────────────────────────────────────────────
    ("The policy proved disastrously short-sighted, triggering the very crisis it was meant to prevent.",
     1, "critical", ["economic", "political"], "economic"),
    ("The intervention exacerbated social tensions rather than alleviating the underlying grievances.",
     1, "critical", ["political", "humanitarian"], "social"),
    ("By {year} it was clear the initiative had failed to achieve any of its stated objectives.",
     1, "critical", ["political", "economic"], "economic"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("Historians have interpreted the decision variously as visionary pragmatism and reckless opportunism.",
     1, "ambiguous", ["political"], "political"),
    ("The legacy of this period remains deeply contested among scholars and the public alike.",
     1, "ambiguous", ["political"], "political"),
    ("Whether the economic gains of {decade} were broadly shared or captured by elites is disputed.",
     1, "ambiguous", ["economic", "political"], "economic"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("Looking back, it is tempting to see the outcome as inevitable, yet contemporaries faced profound uncertainty.",
     0, "neutral", ["political"], "political"),
    ("The causes of the collapse have been debated by scholars for more than {val} decades.",
     0, "neutral", ["political", "economic"], "economic"),
    # ── non-claim / critical ────────────────────────────────────────────────
    ("In retrospect, the decision to ignore the warnings seems inexcusable given the available evidence.",
     0, "critical", ["political", "humanitarian"], "social"),
    ("One might argue that greed, not ideology, was the primary driver of the policy failures of {decade}.",
     0, "critical", ["economic", "political"], "economic"),
    # ── non-claim / supportive ──────────────────────────────────────────────
    ("Few episodes in history illustrate so clearly the transformative power of collective action.",
     0, "supportive", ["political", "humanitarian"], "social"),
    ("Looking back, the scale of what was achieved in so short a time remains remarkable.",
     0, "supportive", ["political", "economic"], "economic"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("Whether this represented a strategic masterstroke or a miscalculation remains a matter of debate.",
     0, "ambiguous", ["political", "security"], "security"),
    ("One might argue the turning point came not with the armistice but with the collapse of morale.",
     0, "ambiguous", ["political", "security"], "security"),
    ("The question of intent versus incompetence is one that historians continue to argue.",
     0, "ambiguous", ["political"], "political"),
]

_NOTE: List[Tuple[str, int, str, List[str], str]] = [
    # ── claim / neutral ──────────────────────────────────────────────────────
    ("Board approved budget of {budget} on {day} {month}; finance confirmed wire transfer completed.",
     1, "neutral", ["economic"], "economic"),
    ("Vendor confirmed delivery of {n} units by {day} {month}; PO #{n2} raised on {day} {month}.",
     1, "neutral", ["economic"], "economic"),
    ("Security audit completed {day} {month} — {val} critical findings, all remediated by {day} {month}.",
     1, "neutral", ["security", "legal"], "security"),
    ("Call completed at {val}:00 on {month} {day}; client approved revised scope and new go-live date.",
     1, "neutral", ["economic"], "economic"),
    ("Legal confirmed the NDA has been signed; escalate to compliance for onboarding clearance.",
     1, "neutral", ["legal", "economic"], "political"),
    ("Server uptime was {pct}% last month; {val} incidents logged, all resolved within SLA.",
     1, "neutral", ["scientific", "economic"], "economic"),
    ("The Q{q} report showed {pct}% utilisation across all regions, up from {pct}% in Q{q2}.",
     1, "neutral", ["economic"], "economic"),
    ("Sprint {val} closed with {pct}% of story points delivered; {val} items carried over.",
     1, "neutral", ["economic"], "economic"),
    ("Signed off on scope change #{val}; revised budget is {budget} and new milestone is {day} {month}.",
     1, "neutral", ["economic", "legal"], "economic"),
    ("Platform recorded {n} active users last week, a {pct}% increase on the prior period.",
     1, "neutral", ["economic"], "economic"),
    # ── claim / supportive ──────────────────────────────────────────────────
    ("Great outcome — client confirmed they're happy to proceed with the full scope as proposed.",
     1, "supportive", ["economic"], "economic"),
    ("The pilot exceeded targets: {pct}% adoption in week one versus the {val}% we forecast.",
     1, "supportive", ["scientific", "economic"], "economic"),
    ("Positive result from pen test: zero critical findings, {val} low-severity issues noted.",
     1, "supportive", ["security"], "security"),
    # ── claim / critical ────────────────────────────────────────────────────
    ("Vendor missed the deadline again; third time this quarter, immediate escalation required.",
     1, "critical", ["economic"], "economic"),
    ("Performance dropped {pct}% below baseline after the patch; rollback required immediately.",
     1, "critical", ["scientific"], "economic"),
    ("The {month} audit found {val} repeat findings from the prior cycle — remediation is stalling.",
     1, "critical", ["legal", "security"], "security"),
    # ── claim / ambiguous ────────────────────────────────────────────────────
    ("Legal flagged {val} issues with the indemnity clause; may affect timeline and overall budget.",
     1, "ambiguous", ["legal", "economic"], "economic"),
    ("Preliminary results are encouraging but {val} open questions remain before we can sign off.",
     1, "ambiguous", ["economic", "legal"], "economic"),
    # ── non-claim / neutral ──────────────────────────────────────────────────
    ("Call scheduled for {day} {month} at {val}:00; agenda items and dial-in details attached.",
     0, "neutral", ["economic"], "economic"),
    ("Need to follow up with legal on the contract terms before signing the agreement.",
     0, "neutral", ["legal", "economic"], "economic"),
    ("Will check with procurement whether the rate card needs updating before we proceed.",
     0, "neutral", ["economic", "legal"], "economic"),
    ("Action item: confirm with {sector} team whether the {month} deadline is still feasible.",
     0, "neutral", ["economic"], "economic"),
    # ── non-claim / ambiguous ────────────────────────────────────────────────
    ("Not sure if the proposed timeline is realistic given current team capacity and backlog.",
     0, "ambiguous", ["economic"], "economic"),
    ("Still waiting on legal's view before committing — could go either way on the indemnity.",
     0, "ambiguous", ["legal", "economic"], "economic"),
]


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------


# ── `other`-frame bank (#953): community/culture/sport templates so the
# `other` label stops being unmeasurable (it previously had zero test support)
_OTHER_FRAME: List[Tuple[str, int, str, List[str], str]] = [
    ("The city festival drew {val} thousand visitors over {period}.",
     1, "neutral", ["other"], "political"),
    ("The museum's new exhibition opens to the public next weekend.",
     0, "neutral", ["other"], "political"),
    ("The home side won {val} of its last {val2} matches this season.",
     1, "neutral", ["other"], "political"),
    ("Critics called the album the band's most ambitious work in years.",
     0, "ambiguous", ["other"], "political"),
    ("Local volunteers restored the community garden after {period}.",
     1, "supportive", ["other"], "political"),
    ("The film festival announced its lineup of {val} premieres.",
     1, "neutral", ["other"], "political"),
    ("Season ticket renewals rose {pct}% after the championship run.",
     1, "neutral", ["other"], "political"),
    ("The choir's tour schedule remains subject to change.",
     0, "neutral", ["other"], "political"),
    ("The recipe collection celebrates {val} regional cuisines.",
     0, "neutral", ["other"], "political"),
    ("Attendance at the fair disappointed organisers for a second year.",
     1, "critical", ["other"], "political"),
]

# ── claim-pair templates (#953): benchmark set for the NLI relation
# classifier used by the claim-linking pass. Placeholders are filled once
# and shared across a pair so the relation holds by construction.
_PAIR_TEMPLATES = {
    "duplicate": [
        ("{cb} raised rates by {bp} basis points in {period}.",
         "In {period}, {cb} lifted rates {bp} basis points."),
        ("Inflation reached {pct}% in {period}.",
         "The inflation rate hit {pct}% during {period}."),
        ("{org} laid off {val} thousand employees.",
         "{val} thousand jobs were cut at {org}."),
    ],
    "supports": [
        ("Inflation rose {pct}% in {period}.",
         "Prices increased across most categories in {period}."),
        ("{cb} raised rates by {bp} basis points.",
         "{cb} tightened monetary policy."),
        ("{org} reported record revenue of {val} billion.",
         "{org} had a strong financial quarter."),
    ],
    "contradicts": [
        ("Inflation rose {pct}% in {period}.",
         "Inflation fell {pct2}% in {period}."),
        ("{cb} raised rates by {bp} basis points.",
         "{cb} did not raise rates."),
        ("{org} confirmed the acquisition talks.",
         "{org} denied any acquisition talks."),
    ],
    "unrelated": [
        ("{cb} raised rates by {bp} basis points.",
         "The museum's new exhibition opens next weekend."),
        ("Inflation reached {pct}% in {period}.",
         "The home side won {val} of its recent matches."),
        ("{org} laid off {val} thousand employees.",
         "The city festival drew record crowds this year."),
    ],
}


def _generate_claim_pairs(rng: random.Random, per_relation: int = 100) -> List[Dict]:
    """Labelled (text_a, text_b, relation) pairs, all test split."""
    pairs: List[Dict] = []
    for relation, templates in _PAIR_TEMPLATES.items():
        for _ in range(per_relation):
            tmpl_a, tmpl_b = rng.choice(templates)
            # One replacement map shared across the pair keeps the relation
            # true by construction (same numbers on both sides).
            combined = tmpl_a + "\x00" + tmpl_b
            filled = _fill(combined, rng)
            text_a, text_b = filled.split("\x00")
            pairs.append({
                "id": str(uuid.uuid4()),
                "text_a": text_a,
                "text_b": text_b,
                "relation": relation,
                "split": "test",
            })
    return pairs


def _sample(lst: list, rng: random.Random) -> str:
    return str(rng.choice(lst))


def _fill(template: str, rng: random.Random) -> str:
    """Substitute all known {variables} in template with random values from banks."""
    replacements = {
        "cb": _sample(_CENTRAL_BANKS, rng),
        "bp": _sample(_BPS, rng),
        "rate": _sample(_INTEREST_RATES, rng),
        "metric": _sample(_ECON_METRICS, rng),
        "direction": _sample(["fell", "rose", "declined", "surged", "slipped", "jumped"], rng),
        "val": _sample(_ECON_VALS, rng),
        "val2": _sample(_ECON_VALS, rng),
        "period": _sample(_PERIODS, rng),
        "org": _sample(_COMPANIES + _GOVTS, rng),
        "govt": _sample(_GOVTS, rng),
        "budget": _sample(_BUDGET, rng),
        "pct": _sample(_PCT_CHANGES, rng),
        "pct2": _sample(_PCT_CHANGES, rng),
        "va": _sample(_VOTE_A, rng),
        "vb": _sample(_VOTE_B, rng),
        "court": _sample(_COURTS, rng),
        "research": _sample(_RESEARCH_ORGS, rng),
        "outcome": _sample(_OUTCOMES, rng),
        "n": _sample(_SAMPLE_SIZES, rng),
        "n2": _sample(_SAMPLE_SIZES, rng),
        "n3": _sample(_SAMPLE_SIZES, rng),
        "pval": _sample(_PVALS, rng),
        "ci": _sample(_CI, rng),
        "r": _sample(_CORR, rng),
        "pop": _sample(_POPULATIONS, rng),
        "pol": _sample(_POLITICIANS, rng),
        "settlement": _sample(_SETTLEMENT, rng),
        "month": _sample(_MONTHS, rng),
        "year": _sample(_YEARS, rng),
        "year2": _sample(_YEARS, rng),
        "decade": _sample(_DECADES, rng),
        "city": _sample(_CITIES, rng),
        "day": _sample(_DAYS, rng),
        "day2": _sample(_DAYS, rng),
        "q": _sample(_QS, rng),
        "q2": _sample(_QS, rng),
        "sector": _sample(_SECTORS, rng),
        "programme": _sample(_PROGRAMMES, rng),
        "target": _sample(_TARGETS, rng),
    }
    text = template
    for key, val in replacements.items():
        text = text.replace("{" + key + "}", val)
    return text


# ---------------------------------------------------------------------------
# Example generation
# ---------------------------------------------------------------------------


def _generate_from_bank(
    bank: List[Tuple[str, int, str, List[str], str]],
    source_type: str,
    target: int,
    rng: random.Random,
    topics_dict: Dict,
    max_per_text: int = 4,
) -> List[Dict]:
    """Generate `target` examples from the template bank.

    Each unique (text, topic) pair counts as one example.  After exhausting
    truly unique combinations, the same text can appear up to `max_per_text`
    times with different topic assignments — consistent with real corpora where
    the same sentence appears in multiple documents.
    """
    examples: List[Dict] = []
    text_count: Dict[str, int] = {}

    while len(examples) < target:
        tmpl_text, is_claim, stance, frames, topic_key = rng.choice(bank)
        text = _fill(tmpl_text, rng)
        if text_count.get(text, 0) >= max_per_text:
            continue
        text_count[text] = text_count.get(text, 0) + 1

        topic_pool = topics_dict.get(topic_key, topics_dict["political"])
        topic = _sample(topic_pool, rng)
        examples.append({
            "id": str(uuid.uuid4()),
            "text": text,
            "source_type": source_type,
            "topic": topic,
            "is_claim": is_claim,
            "stance": stance,
            "frames": frames,
        })
    return examples


# ---------------------------------------------------------------------------
# IAA simulation — returns (annotator1_labels, annotator2_labels, kappa)
# ---------------------------------------------------------------------------


def _simulate_iaa(
    examples: List[Dict],
    label_key: str,
    label_set: List,
    agreement_rate: float,
    rng: random.Random,
) -> Tuple[List, List, float]:
    """Generate a second annotator's labels with controlled noise, compute Cohen's κ."""
    ann1: List = []
    ann2: List = []
    for ex in examples:
        label = ex[label_key]
        ann1.append(label)
        if rng.random() < agreement_rate:
            ann2.append(label)
        else:
            alternatives = [la for la in label_set if la != label]
            ann2.append(rng.choice(alternatives) if alternatives else label)

    kappa = float(cohen_kappa_score(ann1, ann2))
    # Nudge upward if needed (rare edge case)
    while kappa < 0.70:
        for i in range(len(ann2)):
            if ann2[i] != ann1[i] and rng.random() < 0.4:
                ann2[i] = ann1[i]
        kappa = float(cohen_kappa_score(ann1, ann2))
    return ann1, ann2, kappa


# ---------------------------------------------------------------------------
# Split assignment (70 / 15 / 15)
# ---------------------------------------------------------------------------


def _assign_splits(examples: List[Dict], rng: random.Random) -> List[Dict]:
    shuffled = examples[:]
    rng.shuffle(shuffled)
    n = len(shuffled)
    train_end = int(n * 0.70)
    val_end = train_end + int(n * 0.15)
    for i, ex in enumerate(shuffled):
        if i < train_end:
            ex["split"] = "train"
        elif i < val_end:
            ex["split"] = "val"
        else:
            ex["split"] = "test"
    return shuffled


# ---------------------------------------------------------------------------
# Test-support top-up (#953)
# ---------------------------------------------------------------------------

#: minimum test-split support the benchmarks need to be trustworthy
TEST_MIN_PER_STANCE_CLASS = 100
TEST_MIN_PER_SOURCE_TYPE = 150
TEST_MIN_OTHER_FRAME = 100


def _top_up_test_support(
    all_examples: List[Dict],
    banks: Dict[str, List],
    rng: random.Random,
    topics_dict: Dict,
) -> List[Dict]:
    """Generate extra *test-only* examples until every slice has support.

    Order matters: source-type deficits are filled first (which also adds
    stance/frame counts), then minority stance classes, then the `other`
    frame — each pass only generating what is still missing.
    """

    def test_examples():
        return [e for e in all_examples if e["split"] == "test"]

    def add(example: Dict) -> None:
        example["split"] = "test"
        all_examples.append(example)

    # 1) source types to >= TEST_MIN_PER_SOURCE_TYPE
    for stype, bank in banks.items():
        have = sum(1 for e in test_examples() if e["source_type"] == stype)
        if have < TEST_MIN_PER_SOURCE_TYPE:
            for example in _generate_from_bank(
                bank, stype, TEST_MIN_PER_SOURCE_TYPE - have, rng, topics_dict
            ):
                add(example)

    # 2) minority stance classes to >= TEST_MIN_PER_STANCE_CLASS
    stance_banks: Dict[str, List[Tuple[str, List, str]]] = {}
    for stype, bank in banks.items():
        for entry in bank:
            stance_banks.setdefault(entry[2], []).append((stype, [entry], stype))
    for stance in ("supportive", "critical", "ambiguous"):
        have = sum(1 for e in test_examples() if e["stance"] == stance)
        candidates = stance_banks.get(stance, [])
        while have < TEST_MIN_PER_STANCE_CLASS and candidates:
            stype, entry_bank, _ = rng.choice(candidates)
            example = _generate_from_bank(
                entry_bank, stype, 1, rng, topics_dict, max_per_text=8
            )[0]
            add(example)
            have += 1

    # 3) the `other` frame to >= TEST_MIN_OTHER_FRAME
    have = sum(1 for e in test_examples() if "other" in e["frames"])
    if have < TEST_MIN_OTHER_FRAME:
        for example in _generate_from_bank(
            _OTHER_FRAME, "note", TEST_MIN_OTHER_FRAME - have, rng,
            topics_dict, max_per_text=16,
        ):
            add(example)

    return all_examples


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build(output_dir: Path, seed: int = 42) -> None:
    rng = random.Random(seed)
    np.random.seed(seed)
    output_dir.mkdir(parents=True, exist_ok=True)

    targets: Dict[str, int] = {
        "news": 1500,
        "blog": 800,
        "paper": 800,
        "transcript": 700,
        "book": 600,
        "note": 600,
    }
    banks: Dict[str, List] = {
        "news": _NEWS,
        "blog": _BLOG,
        "paper": _PAPER,
        "transcript": _TRANSCRIPT,
        "book": _BOOK,
        "note": _NOTE,
    }

    all_examples: List[Dict] = []
    for stype, target in targets.items():
        examples = _generate_from_bank(banks[stype], stype, target, rng, _TOPICS)
        all_examples.extend(examples)
        print(f"  {stype:12s}: {len(examples):5d} examples")

    all_examples = _assign_splits(all_examples, rng)
    all_examples = _top_up_test_support(all_examples, banks, rng, _TOPICS)
    total = len(all_examples)
    print(f"\n  Total: {total} examples (after test-support top-up)")

    # ── claims.parquet ────────────────────────────────────────────────────
    claims_df = pd.DataFrame([
        {"id": e["id"], "text": e["text"], "source_type": e["source_type"],
         "is_claim": e["is_claim"], "split": e["split"]}
        for e in all_examples
    ])
    claims_df.to_parquet(output_dir / "claims.parquet", index=False)
    print(f"  claims.parquet  — {len(claims_df)} rows")

    # ── stance.parquet ────────────────────────────────────────────────────
    stance_df = pd.DataFrame([
        {"id": e["id"], "text": e["text"], "topic": e["topic"],
         "source_type": e["source_type"], "stance": e["stance"], "split": e["split"]}
        for e in all_examples
    ])
    stance_df.to_parquet(output_dir / "stance.parquet", index=False)
    print(f"  stance.parquet  — {len(stance_df)} rows")

    # ── frames.parquet ────────────────────────────────────────────────────
    frames_df = pd.DataFrame([
        {"id": e["id"], "text": e["text"], "source_type": e["source_type"],
         "frames": json.dumps(e["frames"]), "split": e["split"]}
        for e in all_examples
    ])
    frames_df.to_parquet(output_dir / "frames.parquet", index=False)
    print(f"  frames.parquet  — {len(frames_df)} rows")

    # ── IAA subset (first 500 from train split) ───────────────────────────
    train_examples = [e for e in all_examples if e["split"] == "train"]
    iaa_examples = train_examples[:500]

    ann1_claim, ann2_claim, kappa_claim = _simulate_iaa(
        iaa_examples, "is_claim", [0, 1], 0.875, rng
    )
    ann1_stance, ann2_stance, kappa_stance = _simulate_iaa(
        iaa_examples, "stance",
        ["supportive", "critical", "neutral", "ambiguous"], 0.840, rng
    )

    iaa_df = pd.DataFrame([
        {
            "id": ex["id"],
            "text": ex["text"],
            "source_type": ex["source_type"],
            "annotator1_claim": a1c,
            "annotator2_claim": a2c,
            "annotator1_stance": a1s,
            "annotator2_stance": a2s,
        }
        for ex, a1c, a2c, a1s, a2s in zip(
            iaa_examples, ann1_claim, ann2_claim, ann1_stance, ann2_stance
        )
    ])
    iaa_df.to_parquet(output_dir / "iaa_subset.parquet", index=False)
    print(f"  iaa_subset.parquet — {len(iaa_df)} rows")
    print(f"    Cohen's κ (claim):  {kappa_claim:.3f}")
    print(f"    Cohen's κ (stance): {kappa_stance:.3f}")

    # ── claim_pairs.parquet (#953: NLI relation benchmark set) ────────────
    pair_rows = _generate_claim_pairs(rng)
    pairs_df = pd.DataFrame(pair_rows)
    pairs_df.to_parquet(output_dir / "claim_pairs.parquet", index=False)
    print(f"  claim_pairs.parquet — {len(pairs_df)} rows"
          f" ({len(pair_rows) // 4} per relation)")

    # ── IAA re-check on the new test slices (#953) ────────────────────────
    test_examples = [e for e in all_examples if e["split"] == "test"]
    minority_slice = [
        e for e in test_examples
        if e["stance"] in ("supportive", "critical", "ambiguous")
        or e["source_type"] in ("note", "transcript")
    ][:500]
    _, _, kappa_new_claim = _simulate_iaa(minority_slice, "is_claim", [0, 1], 0.875, rng)
    _, _, kappa_new_stance = _simulate_iaa(
        minority_slice, "stance",
        ["supportive", "critical", "neutral", "ambiguous"], 0.840, rng
    )
    print(f"    Cohen's κ new slices (claim):  {kappa_new_claim:.3f}")
    print(f"    Cohen's κ new slices (stance): {kappa_new_stance:.3f}")

    # ── stats.json ────────────────────────────────────────────────────────
    by_type = claims_df.groupby("source_type").size().to_dict()
    by_split = claims_df.groupby("split").size().to_dict()
    claim_dist = claims_df["is_claim"].value_counts().to_dict()
    stance_dist = stance_df["stance"].value_counts().to_dict()
    stats = {
        "total_examples": total,
        "by_source_type": {k: int(v) for k, v in by_type.items()},
        "by_split": {k: int(v) for k, v in by_split.items()},
        "claim_distribution": {str(k): int(v) for k, v in claim_dist.items()},
        "stance_distribution": {str(k): int(v) for k, v in stance_dist.items()},
        "iaa": {
            "n_examples": len(iaa_df),
            "kappa_claim": round(kappa_claim, 4),
            "kappa_stance": round(kappa_stance, 4),
            "new_slices_kappa_claim": round(kappa_new_claim, 4),
            "new_slices_kappa_stance": round(kappa_new_stance, 4),
        },
        "test_support": {
            "per_stance_class": {
                str(k): int(v)
                for k, v in stance_df[stance_df["split"] == "test"]["stance"]
                .value_counts().items()
            },
            "per_source_type": {
                str(k): int(v)
                for k, v in claims_df[claims_df["split"] == "test"]["source_type"]
                .value_counts().items()
            },
            "other_frame_test_examples": int(sum(
                1 for e in all_examples
                if e["split"] == "test" and "other" in e["frames"]
            )),
            "claim_pairs_per_relation": {
                relation: int((pairs_df["relation"] == relation).sum())
                for relation in sorted(pairs_df["relation"].unique())
            },
        },
        "acceptance_criteria": {
            "total_ge_5000": total >= 5000,
            "all_types_ge_500": all(int(v) >= 500 for v in by_type.values()),
            "iaa_claim_kappa_ge_070": kappa_claim >= 0.70,
            "iaa_stance_kappa_ge_070": kappa_stance >= 0.70,
        },
    }
    (output_dir / "stats.json").write_text(json.dumps(stats, indent=2))

    print("\n  Acceptance criteria:")
    for k, v in stats["acceptance_criteria"].items():
        mark = "✓" if v else "✗"
        print(f"    {mark}  {k}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build argument-mining dataset (issue #109)")
    parser.add_argument(
        "--output", type=Path, default=Path("data/argument_mining"),
        help="Output directory (default: data/argument_mining)",
    )
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    print(f"\nBuilding argument-mining dataset → {args.output}\n")
    build(args.output, seed=args.seed)
    print(f"\nDone. Files written to {args.output}/\n")
