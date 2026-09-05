import json
from dataclasses import asdict
from src.ingestion.structured_metadata import extract_metadata
from src.ingestion.extract import extract_article


def test_multiple_entities_conflicts_and_non_authoritative_canonical():
    graph={'@graph':[{'@type':'NewsArticle','url':'https://example.org/other','headline':'Other'}, {'@type':'NewsArticle','url':'https://example.org/story','headline':'Selected','datePublished':'2024-01-01T12:00:00+02:00','author':{'name':'Author'}}]}
    html='<script type="application/ld+json">'+json.dumps(graph)+'</script><meta property="og:title" content="Conflicting"><link rel="canonical" href="/different"><h1>Visible</h1>'
    result=extract_metadata(html,'https://example.org/story')
    assert result['selected']['title']['value']=='Selected'
    assert {c['value'] for c in result['candidates'] if c['field']=='title'}=={'Other','Selected','Conflicting','Visible'}
    assert not result['canonical_identity_authoritative']
    assert result['selected']['published_at']['value'].endswith('+02:00')


def test_extraction_serializes_versions_hash_and_honest_score():
    result=extract_article('<html><article><h1>Title</h1><p>'+('Evidence sentence. '*40)+'</p></article></html>','https://example.org/story')
    data=json.loads(json.dumps(asdict(result)))
    assert data['score_semantics']=='heuristic_method_tier_not_probability'
    assert len(data['metadata']['snapshot_sha256'])==64
    assert data['metadata']['body_extractor']['version']
    assert 'published_at' not in data['metadata']['selected']
