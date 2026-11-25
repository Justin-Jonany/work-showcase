"""List queued companies by status, split into batches for fan-out to subagents.

    python scraper/list_jobs.py --status pending --batch-size 10
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", default="pending")
    ap.add_argument("--batch-size", type=int, default=10)
    args = ap.parse_args()

    conn = connect()
    domains = [r["domain"] for r in conn.execute(
        "SELECT domain FROM scrape_jobs WHERE status = ? ORDER BY id", (args.status,))]
    batches = [domains[i:i + args.batch_size] for i in range(0, len(domains), args.batch_size)]
    print(json.dumps({"status": args.status, "count": len(domains), "batches": batches}))


if __name__ == "__main__":
    main()
