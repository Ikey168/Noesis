"""
Derived analytics views over the local DuckDB warehouse.

The event-clustering, trending-topics and breaking-news subsystems are normally
backed by separate stores (an ML clusterer over Redshift/Postgres, a keyword
topic database, …). For a local, zero-service setup those aren't available, so
this module derives equivalent, data-driven results directly from the
``news_articles`` table.

Articles are grouped into **event clusters** by content similarity: a TF-IDF
cosine similarity matrix (scikit-learn) when available, otherwise an O(n)
signature-term fallback. Only the most-recent slice of the corpus is clustered
per request (see ``NOESIS_CLUSTER_MAX_ARTICLES``) so the matrix stays bounded.
**Trending topics** come from keyword document-frequency across that slice, and
**breaking news** is the most recent / active clusters. (The previous
implementation grouped by the first word of each title, which produced
meaningless clusters on real headlines.)

The returned dict shapes match what the API route response models expect.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from src.database.local_analytics_connector import LocalAnalyticsConnector
from src.config.env import resolve_env

# Cap how many of the most-recent articles we cluster in one request, so cost
# stays bounded no matter how large the corpus grows. Override with
# NOESIS_CLUSTER_MAX_ARTICLES.
def _max_cluster_articles() -> int:
    try:
        return max(100, int(resolve_env("CLUSTER_MAX_ARTICLES", "3000") or "3000"))
    except ValueError:
        return 3000


# Common English + news-filler words that should never anchor a cluster/topic.
_STOPWORDS = {
    # articles / conjunctions / prepositions
    "the", "a", "an", "and", "or", "but", "nor", "of", "to", "in", "on", "for",
    "with", "at", "by", "from", "as", "into", "onto", "upon", "about", "against",
    "before", "after", "between", "during", "without", "within", "under", "over",
    "above", "below", "amid", "amongst", "among", "across", "behind", "beyond",
    "near", "off", "out", "up", "down", "through", "per", "via", "than", "then",
    # pronouns / determiners
    "it", "its", "this", "that", "these", "those", "there", "here", "their",
    "they", "them", "he", "she", "him", "his", "her", "you", "your", "yours",
    "we", "our", "ours", "us", "i", "my", "mine", "me", "who", "whom", "whose",
    "which", "what", "whatever", "some", "any", "all", "both", "each", "few",
    "more", "most", "other", "others", "such", "no", "nor", "not", "only", "own",
    "same", "so", "too", "very", "one", "ones", "two", "three", "four", "five",
    # verbs / auxiliaries
    "is", "are", "was", "were", "be", "been", "being", "am", "will", "would",
    "could", "should", "shall", "can", "may", "might", "must", "has", "have",
    "had", "having", "do", "does", "did", "done", "doing", "get", "gets", "got",
    "make", "makes", "made", "say", "says", "said", "tell", "told", "see", "seen",
    "go", "goes", "went", "set", "put", "take", "took", "give", "gave", "use",
    "used", "call", "calls", "called", "warn", "warns", "warned", "join", "joins",
    "show", "shows", "showed", "face", "faces", "faced", "back", "backs",
    # adverbs / time
    "now", "when", "where", "while", "why", "how", "again", "ever", "never",
    "once", "always", "often", "still", "just", "also", "even", "yet", "soon",
    "today", "year", "years", "day", "days", "week", "weeks", "month", "months",
    "time", "times", "ago", "first", "last", "next", "new", "old", "end", "live",
    # generic news nouns (not useful as standalone topics)
    "news", "report", "reports", "update", "updates", "people", "man", "woman",
    "men", "women", "way", "ways", "thing", "things", "part", "case", "cases",
    "plan", "plans", "deal", "deals", "data", "media", "company", "companies",
    "group", "groups", "department", "official", "officials", "leader", "leaders",
    "minister", "government", "president", "according", "amphtml", "video", "watch",
}

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'&-]+")


def _normalize_token(tok: str) -> str:
    """Lowercase, strip surrounding punctuation and a trailing possessive."""
    t = tok.lower().strip("'-&")
    if t.endswith("'s"):
        t = t[:-2]
    return t


def _terms(text: str) -> List[str]:
    """Lowercased significant terms (length > 2, not stopwords)."""
    out = []
    for tok in _TOKEN_RE.findall(text or ""):
        t = _normalize_token(tok)
        if len(t) > 2 and t not in _STOPWORDS:
            out.append(t)
    return out



# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #


async def _fetch_recent(
    days: int, category: Optional[str] = None
) -> List[Dict[str, Any]]:
    db = LocalAnalyticsConnector()
    db.connect()
    cutoff = datetime.now() - timedelta(days=days)
    conditions = ["publish_date >= %s"]
    params: List[Any] = [cutoff]
    if category:
        conditions.append("category = %s")
        params.append(category)
    where = " AND ".join(conditions)
    # Only cluster the most-recent slice so cost is bounded on large corpora.
    rows = await db.execute_query(
        """
        SELECT id, title, content, publish_date, source, category, sentiment_score
        FROM news_articles
        WHERE {where}
        ORDER BY publish_date DESC
        LIMIT %s
        """.format(where=where),
        params + [_max_cluster_articles()],
    )
    db.disconnect()

    recent_cut = datetime.now() - timedelta(hours=24)
    articles = []
    for (aid, title, content, pub, source, cat, sent) in rows:
        articles.append(
            {
                "id": aid,
                "title": title or "",
                "content": content or "",
                "publish_date": pub,
                "source": source or "Unknown",
                "category": cat or "General",
                "sentiment": float(sent or 0.0),
                "terms": set(_terms(title or "")) | set(_terms(content or "")[:20]),
                "is_recent": bool(pub and pub >= recent_cut),
            }
        )
    return articles


# --------------------------------------------------------------------------- #
# Clustering
# --------------------------------------------------------------------------- #


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _cluster_sklearn(articles: Sequence[Dict[str, Any]], threshold: float) -> Optional[List[int]]:
    """Cluster via a TF-IDF cosine similarity matrix + union-find.

    Builds the dense pairwise cosine similarity matrix (O(n^2) memory) and
    connects every pair above ``threshold`` into the same component. The recent
    window is capped (see ``NOESIS_CLUSTER_MAX_ARTICLES``) so the matrix stays
    bounded. Returns a label per article, or None if scikit-learn is missing.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except Exception:
        return None

    n = len(articles)
    docs = [a["title"] + ". " + a["content"][:200] for a in articles]
    try:
        vec = TfidfVectorizer(
            stop_words="english", ngram_range=(1, 2), max_features=8000, min_df=1
        )
        matrix = vec.fit_transform(docs)
        sims = cosine_similarity(matrix)
    except Exception:
        return None

    uf = _UnionFind(n)
    for i in range(n):
        row = sims[i]
        for j in range(i + 1, n):
            if row[j] >= threshold:
                uf.union(i, j)
    return [uf.find(i) for i in range(n)]


def _cluster_signature_term(articles: Sequence[Dict[str, Any]]) -> List[int]:
    """O(n) fallback: group by each article's most distinctive (rarest) term.

    Document frequency is computed once; each article is keyed by the
    lowest-frequency significant term it contains, so articles about the same
    specific entity/keyword land together. Articles whose signature term is
    unique stay as singletons.
    """
    n = len(articles)
    df: Counter = Counter()
    for a in articles:
        for t in a["terms"]:
            df[t] += 1

    signature_to_label: Dict[str, int] = {}
    labels: List[int] = []
    next_label = 0
    for i, a in enumerate(articles):
        terms = a["terms"]
        if terms:
            # Rarest term wins; ties broken alphabetically for determinism.
            signature = min(terms, key=lambda t: (df[t], t))
        else:
            signature = "__empty_{0}__".format(i)
        if signature not in signature_to_label:
            signature_to_label[signature] = next_label
            next_label += 1
        labels.append(signature_to_label[signature])
    return labels


def _build_clusters(articles: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not articles:
        return []

    labels = _cluster_sklearn(articles, threshold=0.18)
    if labels is None:
        labels = _cluster_signature_term(articles)

    groups: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for label, art in zip(labels, articles):
        groups[label].append(art)

    clusters: List[Dict[str, Any]] = []
    for members in groups.values():
        size = len(members)
        dates = [m["publish_date"] for m in members if m["publish_date"]]
        first_date = min(dates) if dates else datetime.now()
        last_date = max(dates) if dates else datetime.now()
        recent = sum(1 for m in members if m["is_recent"])
        recent_fraction = recent / size if size else 0.0

        # Representative headline: the member sharing the most terms with the
        # cluster's vocabulary (falls back to the longest title).
        vocab = Counter()
        for m in members:
            vocab.update(m["terms"])
        def _centrality(m):
            return (sum(vocab[t] for t in m["terms"]), len(m["title"]))
        rep = max(members, key=_centrality)

        top_terms = [t.title() for t, _ in vocab.most_common(4)]
        sources = sorted({m["source"] for m in members})
        category = Counter(m["category"] for m in members).most_common(1)[0][0]
        avg_sent = sum(m["sentiment"] for m in members) / size

        duration_hours = round((last_date - first_date).total_seconds() / 3600.0, 1)
        trending_score = round(min(100.0, size * 8 + recent_fraction * 25), 2)
        impact_score = round(min(100.0, size * 9 + len(sources) * 4), 2)

        clusters.append(
            {
                "size": size,
                "headline": rep["title"],
                "category": category,
                "sources": sources,
                "source_count": len(sources),
                "key_entities": top_terms,
                "avg_sentiment": round(avg_sent, 3),
                "first_date": first_date,
                "last_date": last_date,
                "recent_fraction": round(recent_fraction, 3),
                "duration_hours": duration_hours,
                "trending_score": trending_score,
                "impact_score": impact_score,
                "velocity_score": round(recent_fraction, 3),
                # Other member headlines (excluding the representative), newest first.
                "sample_titles": [
                    m["title"]
                    for m in sorted(
                        members,
                        key=lambda m: m["publish_date"] or datetime.min,
                        reverse=True,
                    )
                    if m["title"] != rep["title"]
                ][:3],
            }
        )

    # Most significant first.
    clusters.sort(key=lambda c: (c["size"], c["trending_score"]), reverse=True)
    return clusters


def _event_type(c: Dict[str, Any]) -> str:
    if c["recent_fraction"] >= 0.5:
        return "breaking"
    if c["recent_fraction"] > 0.0:
        return "developing"
    return "trending"


# --------------------------------------------------------------------------- #
# Public views
# --------------------------------------------------------------------------- #


async def get_trending_topics(days: int = 7) -> List[Dict[str, Any]]:
    """Trending topics from named-entity document-frequency across recent articles.

    Topics are the proper-noun phrases / acronyms surfaced by the same entity
    extraction that drives the entity graph (so "Elon Musk" and "Andy Burnham"
    stay whole and surname mentions fold into the full name), rather than a
    single-word bag-of-words that surfaces generic filler ("war", "study", …).
    """
    articles = await _fetch_recent(days)
    if not articles:
        return []

    doc_count, label_form, _, entity_docs = _aggregate_entities(articles)
    # Category names are not interesting as topics ("World", "Health", "Tech").
    categories = {(a["category"] or "").lower() for a in articles}

    topics = []
    for key, count in doc_count.items():
        if count < 2:  # require corroboration across at least two articles
            continue
        label = label_form[key].most_common(1)[0][0] if label_form[key] else key
        low = label.lower()
        # Drop vague single-word topics and category names; keep multi-word
        # phrases ("World Cup") and acronyms ("EU", "OPEC", "AI").
        if " " not in label and not label.isupper():
            if low in _GENERIC_TOPIC_TERMS or low in categories:
                continue
        docs = entity_docs[key]
        recent = sum(1 for d in docs if d["is_recent"])
        topics.append(
            {
                "term": label,
                "article_count": count,
                "avg_sentiment": round(sum(d["sentiment"] for d in docs) / count, 3),
                "growth_rate": round(recent / count, 3),
            }
        )
    topics.sort(key=lambda t: t["article_count"], reverse=True)
    max_count = topics[0]["article_count"] if topics else 1

    return [
        {
            "topic": t["term"],
            "topic_name": t["term"],
            "article_count": t["article_count"],
            "avg_probability": round(t["article_count"] / max_count, 3),
            "avg_sentiment": t["avg_sentiment"],
            "growth_rate": t["growth_rate"],
        }
        for t in topics
    ]


async def get_event_clusters(
    days_back: int = 7,
    category: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Event clusters in the shape expected by /api/v1/events/clusters."""
    articles = await _fetch_recent(days_back, category=category)
    clusters = _build_clusters(articles)
    now_iso = datetime.now().isoformat()
    out: List[Dict[str, Any]] = []
    for i, c in enumerate(clusters):
        etype = _event_type(c)
        if event_type and etype != event_type:
            continue
        out.append(
            {
                "cluster_id": "cl-{0:04d}".format(i),
                "cluster_name": c["headline"],
                "event_type": etype,
                "category": c["category"],
                "cluster_size": c["size"],
                "silhouette_score": 0.5,
                "cohesion_score": 0.6,
                "separation_score": 0.5,
                "trending_score": c["trending_score"],
                "impact_score": c["impact_score"],
                "velocity_score": c["velocity_score"],
                "significance_score": round(
                    c["trending_score"] * 0.3 + c["impact_score"] * 0.4, 2
                ),
                "first_article_date": c["first_date"].isoformat(),
                "last_article_date": c["last_date"].isoformat(),
                "event_duration_hours": c["duration_hours"],
                "primary_sources": c["sources"],
                "geographic_focus": [],
                "key_entities": c["key_entities"],
                "status": "active",
                "created_at": now_iso,
                "avg_sentiment": c["avg_sentiment"],
                "sample_headlines": c["sample_titles"],
            }
        )
    return out[:limit]


async def get_breaking_news(
    hours_back: int = 24,
    category: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Breaking news in the shape expected by /api/v1/breaking_news."""
    days = max(1, (hours_back + 23) // 24)
    articles = await _fetch_recent(days, category=category)
    clusters = _build_clusters(articles)
    # Prefer clusters with recent activity, then size.
    clusters.sort(key=lambda c: (c["recent_fraction"], c["size"]), reverse=True)
    events: List[Dict[str, Any]] = []
    for i, c in enumerate(clusters[:limit]):
        events.append(
            {
                "cluster_id": "bn-{0:04d}".format(i),
                "cluster_name": c["headline"],
                "event_type": _event_type(c),
                "category": c["category"],
                "trending_score": c["trending_score"],
                "impact_score": c["impact_score"],
                "velocity_score": c["velocity_score"],
                "cluster_size": c["size"],
                "first_article_date": c["first_date"].isoformat(),
                "last_article_date": c["last_date"].isoformat(),
                "peak_activity_date": c["last_date"].isoformat(),
                "event_duration_hours": c["duration_hours"],
                "sample_headlines": " | ".join(c["sample_titles"]),
                "source_count": c["source_count"],
                "avg_confidence": 0.8,
            }
        )
    return events


# --------------------------------------------------------------------------- #
# Entity graph
# --------------------------------------------------------------------------- #

# Capitalized words that are usually sentence starters, not entities.
_ENTITY_STOP = {
    "The", "This", "That", "These", "Those", "There", "Here", "After", "Before",
    "But", "And", "How", "Why", "What", "When", "Where", "Who", "Which", "Now",
    "New", "More", "Most", "First", "Last", "Then", "They", "Their", "It", "Its",
    "He", "She", "We", "You", "His", "Her", "Our", "As", "At", "In", "On", "Of",
    "For", "From", "With", "By", "To", "Up", "Out", "Over", "Amid", "One", "Two",
    "Three", "Us", "Live", "Watch", "Why", "Could", "Would", "Should", "May",
}

# Small curated hints for node typing/colouring (best-effort without NER).
_PLACE_HINTS = {
    "ukraine", "russia", "china", "israel", "gaza", "europe", "brussels", "washington",
    "geneva", "london", "paris", "germany", "france", "india", "japan", "iran", "us",
    "uk", "usa", "america", "britain", "moscow", "kyiv", "beijing", "taiwan",
}
_TOPIC_HINTS = {"ai", "ml", "gdp", "cpi", "covid", "climate", "inflation", "oil", "crypto"}
_ORG_HINTS = {
    "nvidia", "microsoft", "google", "apple", "amazon", "meta", "tesla", "opec",
    "nato", "fed", "openai", "samsung", "intel", "boeing", "spacex", "reuters",
}
# Words that mark a Title-case phrase as an organisation rather than a person.
_ORG_NAME_KEYWORDS = {
    "bank", "group", "corp", "council", "union", "company", "ministry", "federal",
    "reserve", "department", "agency", "bureau", "commission", "authority", "fund",
    "party", "court", "university", "institute", "association", "office",
}

_ENTITY_RE = re.compile(
    r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){0,3})\b"  # Capitalized phrases
    r"|\b([A-Z]{2,6})\b"  # acronyms (AI, EU, OPEC, NATO)
)


# Leading/trailing words to strip from a captured phrase (articles, honorifics,
# titles, and any generic stopword that leaked into a Title-case run).
_LEADING_DROP = {
    "the", "a", "an", "ceo", "president", "chair", "chairman", "chief", "mr",
    "ms", "mrs", "dr", "sir", "senator", "sen", "rep", "governor", "gov", "minister",
}
_LEAD_TRAIL_STOP = _STOPWORDS | _LEADING_DROP

# Vague single-word nouns that read as proper nouns (capitalised at the start of
# a headline) but are useless as standalone trending topics. Multi-word phrases
# and acronyms are never filtered by this set.
_GENERIC_TOPIC_TERMS = {
    # vague nouns
    "war", "tech", "cup", "study", "experts", "expert", "win", "wins", "rate",
    "rates", "since", "much", "more", "talks", "deal", "vote", "poll", "growth",
    "jobs", "home", "life", "team", "game", "match", "season", "police", "court",
    "boss", "star", "stars", "world", "health", "news", "report", "watch",
    "video", "update", "warning", "fears", "crisis", "row", "bid", "ban", "cut",
    "cuts", "hit", "set", "sets", "top", "big", "key", "best", "worst", "outlook",
    "signal", "signals", "gain", "gains", "loss", "losses",
    # third-person headline verbs that Title-Case headlines surface as "nouns"
    "weighs", "prompts", "draws", "enters", "beats", "sees", "eyes", "looks",
    "seeks", "aims", "vows", "slams", "urges", "sparks", "fuels", "marks",
    "opens", "closes", "leads", "names", "picks", "reveals", "unveils",
    "launches", "halts", "blocks", "bans", "mulls", "raises", "drops", "holds",
    "keeps", "adds", "loses", "wins", "warns", "calls", "backs", "faces",
    "boosts", "cuts", "hits", "plans", "moves", "pushes", "pulls", "rejects",
    "approves", "delays", "ends", "begins", "starts", "expands", "tops",
}


def _extract_entities(text: str) -> List[str]:
    """Best-effort named-entity surface forms from sentence-case text."""
    out: List[str] = []
    for m in _ENTITY_RE.finditer(text or ""):
        ent = (m.group(1) or m.group(2) or "").strip()
        if not ent:
            continue
        words = ent.split()
        # Strip leading/trailing filler ("As Israel", "Join Trump", "But Burnham",
        # "The Federal Reserve", "CEO Jane Doe", "Trump Says").
        while len(words) > 1 and words[0].lower() in _LEAD_TRAIL_STOP:
            words = words[1:]
        while len(words) > 1 and words[-1].lower() in _LEAD_TRAIL_STOP:
            words = words[:-1]
        if not words:
            continue
        ent = " ".join(words)
        is_acronym = ent.isupper()
        if len(words) == 1:
            low = ent.lower()
            if low in _STOPWORDS or ent in _ENTITY_STOP:
                continue
            if not is_acronym and len(ent) < 3:
                continue
            if is_acronym and len(ent) < 2:
                continue
        if all(w.lower() in _LEAD_TRAIL_STOP or w in _ENTITY_STOP for w in words):
            continue
        out.append(ent)
    return out


def _resolve_aliases(doc_count: "Counter", label_form: Dict[str, "Counter"]) -> Dict[str, str]:
    """Map a person's surname to their fuller 'First Last' form, when present.

    e.g. "powell" -> "jerome powell". Only applies to 2-word Title-case names so
    organisations/topics aren't accidentally merged.
    """
    alias: Dict[str, str] = {}
    multiword = [k for k in doc_count if " " in k]
    for key in multiword:
        label = label_form[key].most_common(1)[0][0] if label_form[key] else key
        words = label.split()
        if len(words) == 2 and all(w[:1].isupper() and w[1:].islower() for w in words):
            surname = words[1].lower()
            if surname in doc_count and surname != key:
                # Map the surname-only mention to the fuller name.
                alias[surname] = key
    return alias


def _entity_type(label: str) -> str:
    low = label.lower()
    words = label.split()
    if low in _PLACE_HINTS:
        return "place"
    if low in _TOPIC_HINTS:
        return "topic"
    if low in _ORG_HINTS:
        return "org"
    if label.isupper():  # acronyms (OPEC, NATO, EU)
        return "org"
    # Two-word Title-case names → person, unless they read like an organisation.
    if len(words) == 2 and all(w[:1].isupper() and w[1:].islower() for w in words):
        if not any(k in low for k in _ORG_NAME_KEYWORDS):
            return "person"
        return "org"
    return "org" if len(words) >= 2 else "topic"


_ACCENT = "#FF6B6B"
_TYPE_COLOR = {
    "org": _ACCENT,
    "person": "#5B9DFF",
    "topic": "#FFD93D",
    "place": "#A78BFA",
}


def _aggregate_entities(
    articles: Sequence[Dict[str, Any]],
) -> tuple:
    """Extract named entities from articles and aggregate by canonical key.

    Returns ``(doc_count, label_form, per_article, entity_docs)`` where keys are
    lowercased canonical entity surface forms (surnames resolved to their fuller
    "First Last" form). ``per_article`` is the list of canonical-key sets aligned
    with ``articles``; ``entity_docs`` maps each key to the articles mentioning it.
    Shared by the entity graph and trending-topics views.
    """
    raw_doc_count: Counter = Counter()
    label_form: Dict[str, Counter] = defaultdict(Counter)
    raw_per_article: List[set] = []
    for a in articles:
        ents = _extract_entities(a["title"]) + _extract_entities(a["content"][:400])
        keys = set()
        for e in ents:
            key = e.lower()
            label_form[key][e] += 1
            keys.add(key)
        for key in keys:
            raw_doc_count[key] += 1
        raw_per_article.append(keys)

    # Resolve surname aliases (powell -> jerome powell) and re-aggregate.
    alias = _resolve_aliases(raw_doc_count, label_form)
    doc_count: Counter = Counter()
    per_article: List[set] = []
    entity_docs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a, keys in zip(articles, raw_per_article):
        canon = {alias.get(k, k) for k in keys}
        for k in canon:
            doc_count[k] += 1
            entity_docs[k].append(a)
        per_article.append(canon)
    return doc_count, label_form, per_article, entity_docs


async def get_entity_graph(days: int = 7, max_nodes: int = 14) -> Dict[str, Any]:
    """Entity co-occurrence graph derived from recent articles."""
    articles = await _fetch_recent(days)
    if not articles:
        return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}

    doc_count, label_form, per_article, _ = _aggregate_entities(articles)

    # Keep entities mentioned in at least 2 articles, top by frequency.
    ranked = [k for k, c in doc_count.most_common() if c >= 2][:max_nodes]
    if len(ranked) < max_nodes:
        ranked = [k for k, _ in doc_count.most_common(max_nodes)]
    selected = set(ranked)

    # Co-occurrence edges among selected entities.
    edge_w: Counter = Counter()
    for keys in per_article:
        present = sorted(keys & selected)
        for i in range(len(present)):
            for j in range(i + 1, len(present)):
                edge_w[(present[i], present[j])] += 1

    degree: Counter = Counter()
    for (x, y), w in edge_w.items():
        degree[x] += 1
        degree[y] += 1

    nodes = []
    for key in ranked:
        label = label_form[key].most_common(1)[0][0] if label_form[key] else key
        etype = _entity_type(label)
        nodes.append(
            {
                "id": key,
                "label": label,
                "type": etype,
                "color": _TYPE_COLOR.get(etype, _ACCENT),
                "count": doc_count[key],
                "degree": degree.get(key, 0),
            }
        )

    edges = [
        {"source": x, "target": y, "weight": w}
        for (x, y), w in edge_w.most_common(40)
    ]

    return {
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


# --------------------------------------------------------------------------- #
# Sentiment heatmap
# --------------------------------------------------------------------------- #


async def get_sentiment_heatmap(days: int = 14, max_topics: int = 6) -> Dict[str, Any]:
    """Category x day average-sentiment heatmap from real article sentiment."""
    db = LocalAnalyticsConnector()
    db.connect()
    cutoff = datetime.now() - timedelta(days=days)
    rows = await db.execute_query(
        """
        SELECT category,
               CAST(publish_date AS DATE) AS day,
               AVG(sentiment_score) AS avg_sent,
               COUNT(*) AS cnt
        FROM news_articles
        WHERE publish_date >= %s AND sentiment_label IS NOT NULL
        GROUP BY category, CAST(publish_date AS DATE)
        """,
        [cutoff],
    )
    db.disconnect()

    if not rows:
        return {"topics": [], "cols": 0, "labels": [], "seed": []}

    # Top categories by article volume.
    cat_total: Counter = Counter()
    cell: Dict[tuple, float] = {}
    for (category, day, avg_sent, cnt) in rows:
        cat = category or "General"
        cat_total[cat] += int(cnt)
        cell[(cat, day)] = float(avg_sent or 0.0)
    topics = [c for c, _ in cat_total.most_common(max_topics)]

    # Day columns: oldest -> newest across the window.
    today = datetime.now().date()
    day_cols = [today - timedelta(days=(days - 1 - i)) for i in range(days)]
    labels = ["{0}/{1}".format(d.month, d.day) for d in day_cols]

    seed = [
        [round(cell.get((topic, day), 0.0), 3) for day in day_cols]
        for topic in topics
    ]

    return {"topics": topics, "cols": len(day_cols), "labels": labels, "seed": seed}


# --------------------------------------------------------------------------- #
# Topic sentiment (keyword-based, replaces first-word grouping)
# --------------------------------------------------------------------------- #


def _label_for(score: float) -> str:
    return "positive" if score > 0.05 else "negative" if score < -0.05 else "neutral"


async def get_topic_sentiment(
    days: int = 7, min_articles: int = 5, max_topics: int = 12
) -> List[Dict[str, Any]]:
    """Per-keyword sentiment breakdown (shape matches /news_sentiment/topics)."""
    articles = await _fetch_recent(days)
    if not articles:
        return []

    term_docs: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for a in articles:
        for t in set(a["terms"]):
            term_docs[t].append(a)

    topics: List[Dict[str, Any]] = []
    for term, docs in term_docs.items():
        if len(docs) < min_articles:
            continue
        buckets: Dict[str, List[float]] = defaultdict(list)
        for d in docs:
            buckets[_label_for(d["sentiment"])].append(d["sentiment"])
        sentiments = {
            lbl: {"count": len(vals), "avg_score": round(sum(vals) / len(vals), 3)}
            for lbl, vals in buckets.items()
        }
        topics.append(
            {
                "topic": term.title(),
                "total_articles": len(docs),
                "sentiments": sentiments,
            }
        )

    topics.sort(key=lambda x: x["total_articles"], reverse=True)
    return topics[:max_topics]
