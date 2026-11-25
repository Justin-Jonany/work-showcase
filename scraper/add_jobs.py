"""Register companies in the scrape queue. Already-known domains are reported, not duplicated.

    python scraper/add_jobs.py --name Stripe --domain stripe.com --sector payments --prompt "..."
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect, job_by_domain, normalize_domain, now


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--domain", required=True)
    ap.add_argument("--sector")
    ap.add_argument("--prompt")
    args = ap.parse_args()

    conn = connect()
    domain = normalize_domain(args.domain)
    existing = job_by_domain(conn, domain)
    if existing:
        print(f"exists {domain} status={existing['status']}")
        return
    ts = now()
    conn.execute(
        "INSERT INTO scrape_jobs (company_name, domain, sector, prompt, status, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (args.name.strip(), domain, args.sector, args.prompt, ts, ts),
    )
    conn.commit()
    print(f"added {domain}")


if __name__ == "__main__":
    main()
