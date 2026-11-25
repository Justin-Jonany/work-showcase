"""Store one verbatim search result / fetched page for a company.

    python scraper/save_page.py --domain stripe.com --url https://... --source-type data_provider \
        --retrieval search_snippet --query "stripe revenue" --text-file /tmp/x.txt
    (or --text "..."; --mark-searched flips the job to 'searched' once all results are saved)
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import connect, job_by_domain, now, sha256

MAX_CHARS = 20000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", required=True)
    ap.add_argument("--url")
    ap.add_argument("--source-type",
                    choices=["company_site", "wikipedia", "news", "data_provider", "other"])
    ap.add_argument("--retrieval", choices=["search_snippet", "fetched_page"])
    ap.add_argument("--query")
    ap.add_argument("--text")
    ap.add_argument("--text-file")
    ap.add_argument("--mark-searched", action="store_true")
    args = ap.parse_args()

    conn = connect()
    job = job_by_domain(conn, args.domain)
    if not job:
        sys.exit(f"unknown domain {args.domain}; register it with add_jobs.py first")

    text = Path(args.text_file).read_text() if args.text_file else args.text
    if text:
        if not (args.url and args.source_type and args.retrieval):
            sys.exit("--url, --source-type and --retrieval are required when saving text")
        text = text.strip()[:MAX_CHARS]
        cur = conn.execute(
            "INSERT OR IGNORE INTO raw_pages (job_id, url, source_type, retrieval, query, text,"
            " content_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (job["id"], args.url, args.source_type, args.retrieval, args.query, text,
             sha256(text), now()),
        )
        print("saved" if cur.rowcount else "duplicate")

    if args.mark_searched:
        conn.execute("UPDATE scrape_jobs SET status='searched', updated_at=? WHERE id=?",
                     (now(), job["id"]))
        print(f"marked {job['domain']} searched")
    conn.commit()


if __name__ == "__main__":
    main()
