"""Print every stored page for a company, for a parser agent to read.

    python scraper/export_pages.py --domain stripe.com
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect, job_by_domain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    args = ap.parse_args()

    conn = connect()
    job = job_by_domain(conn, args.domain)
    if not job:
        sys.exit(f"unknown domain {args.domain}")
    print(f"# {job['company_name']} ({job['domain']})")
    pages = conn.execute(
        "SELECT * FROM raw_pages WHERE job_id = ? ORDER BY id", (job["id"],)
    ).fetchall()
    for p in pages:
        print(f"\n--- page_id={p['id']} source_type={p['source_type']} retrieval={p['retrieval']}")
        print(f"url: {p['url']}")
        print(f"query: {p['query']}")
        print(p["text"])


if __name__ == "__main__":
    main()
