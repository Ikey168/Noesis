"""Opt-in bounded live provider checks, separate from offline fixture evidence."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.ingestion.source_packs import validate_source_pack, SourcePackError
from src.ingestion.source_pack_runtime import HTTPSPageAdapter


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    checks=[]
    for path in ('research.json','scientific.json'):
        manifest=validate_source_pack(json.loads((Path('config/source_packs')/path).read_text()))
        for source in manifest['sources']:
            if source['source_id'] not in ('crossref-works','openalex-works','europe-pmc'):continue
            adapter=HTTPSPageAdapter(source,secret=os.environ.get('NOESIS_OPENALEX_API_KEY') if source['source_id']=='openalex-works' else None)
            result={'source_id':source['source_id'],'adapter_version':'native-scholarly-v1','source_hash':source['source_hash'],'checked_at':datetime.now(timezone.utc).isoformat(),'fixture_status':'separate offline test suite','live_status':'unverified'}
            try:
                page=adapter.fetch_page({'operation':'topic' if source['source_id']=='europe-pmc' else 'search','parameters':{'query':'climate'},'limit':2},cursor=None)
                result.update(live_status='verified',records=len(page.records),has_next_cursor=page.next_cursor is not None,representations=sorted({r['content_representation'] for r in page.records}))
            except SourcePackError as exc:
                result.update(live_status='failed',failure_code=exc.code)
            checks.append(result)
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps({'checks':checks},indent=2)+'\n')

if __name__=='__main__':main()
