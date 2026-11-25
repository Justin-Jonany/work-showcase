import hashlib
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
# COMPANIES_DB lets tests point at a scratch database.
DB_PATH = Path(os.environ.get("COMPANIES_DB", ROOT / "data" / "companies.db"))
SCHEMA_PATH = ROOT / "schema.sql"


def connect(path=DB_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout let several agents write small rows concurrently.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA_PATH.read_text())
    migrate(conn)
    return conn


# Columns added after the first schema. CREATE TABLE IF NOT EXISTS won't touch an
# existing table, so older databases get them here.
NEW_COLUMNS = {
    "rank_runs": [
        ("mode", "TEXT NOT NULL DEFAULT 'similar'"),
    ],
    "rank_results": [
        ("fit_score", "REAL"),
        ("fit_rank", "INTEGER"),
    ],
}


def migrate(conn):
    for table, columns in NEW_COLUMNS.items():
        have = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_domain(value):
    value = value.strip().lower()
    if "://" not in value:
        value = "http://" + value
    host = urlparse(value).hostname or ""
    return host.removeprefix("www.")


def job_by_domain(conn, domain):
    """Return the scrape_jobs row for a domain, or None if there is no job yet.

    The domain is normalized first, so "https://www.Stripe.com/about",
    "www.stripe.com" and "stripe.com" all find the same job. Jobs are always
    stored normalized (add_jobs.py) and the column is UNIQUE, so there is at
    most one match. Callers use it to dedupe new jobs and to resolve the
    domain a CLI or staging filename names into its job id.
    """
    return conn.execute(
        "SELECT * FROM scrape_jobs WHERE domain = ?", (normalize_domain(domain),)
    ).fetchone()
