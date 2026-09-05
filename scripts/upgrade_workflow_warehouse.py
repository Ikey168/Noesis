"""Upgrade a stopped workflow warehouse; back up the database before invoking."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.kb.warehouse_upgrade import upgrade_workflow_warehouse

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('database')
    args=parser.parse_args()
    path=Path(args.database)
    if not path.is_file():parser.error('database must already exist')
    import duckdb
    with duckdb.connect(str(path)) as conn:
        print(json.dumps(upgrade_workflow_warehouse(conn),sort_keys=True))
