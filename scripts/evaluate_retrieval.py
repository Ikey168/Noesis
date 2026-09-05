"""Evaluate frozen human-judged runs; never invent judgments or provider output."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.kb.retrieval_eval import evaluate_retrieval

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('manifest'); parser.add_argument('--out',required=True)
    parser.add_argument('--allow-fixture',action='store_true')
    args=parser.parse_args()
    result=evaluate_retrieval(args.manifest,allow_fixture=args.allow_fixture)
    Path(args.out).write_text(json.dumps(result,indent=2,sort_keys=True)+'\n')
