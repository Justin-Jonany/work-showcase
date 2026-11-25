"""Load parser output (staging/parsed/<domain>.json) into companies / locations / field_sources,
then refresh embeddings for any company whose profile_text changed. Successfully ingested files
are deleted.

    python scraper/ingest.py                      # every file in staging/parsed
    python scraper/ingest.py --domain stripe.com  # just one
    python scraper/ingest.py --parsed-dir /tmp/parsed
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import connect, job_by_domain, normalize_domain, now, sha256
from normalize import parse_employees, parse_money, parse_visits, parse_year

DESCRIPTION_FIELDS = ["description", "product_description", "business_model_description",
                      "customer_description"]
FIELDS = DESCRIPTION_FIELDS + ["revenue", "web_traffic", "employees", "founded_year", "headquarters"]
CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}
SOURCE_RANK = {"company_site": 0, "wikipedia": 1, "data_provider": 2, "news": 3, "other": 4}
COVERAGE_COLUMNS = ["name"] + DESCRIPTION_FIELDS + [
    "revenue_raw", "web_traffic_raw", "employees_raw", "founded_year", "headquarters"]


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def clean_text(value):
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def candidates_for(field_value, pages, pages_by_url):
    """Normalize one field's candidates and sort them best-first."""
    if not field_value:
        return []
    # Older parser prompts emitted a single object instead of a list.
    if isinstance(field_value, dict):
        field_value = [field_value]
    if not isinstance(field_value, list):
        field_value = [{"value": field_value}]
    out = []
    for c in field_value:
        if not isinstance(c, dict):
            c = {"value": c}
        value = clean_text(c.get("value"))
        if value is None:
            continue
        source_url = clean_text(c.get("source_url"))
        page = None
        pid = c.get("page_id")
        if isinstance(pid, int) and not isinstance(pid, bool) and pid in pages:
            page = pages[pid]
        elif source_url and source_url in pages_by_url:
            page = pages_by_url[source_url]
        confidence = c.get("confidence") if c.get("confidence") in CONFIDENCE_RANK else None
        out.append({
            "value": value,
            "page_id": page["id"] if page else None,
            "source_url": source_url or (page["url"] if page else None),
            "confidence": confidence,
            "source_rank": SOURCE_RANK.get(page["source_type"], 4) if page else 4,
            "fetched_at": page["fetched_at"] if page else "",
        })
    # Two stable sorts: most recent first, then confidence and source priority dominate.
    out.sort(key=lambda c: c["fetched_at"], reverse=True)
    out.sort(key=lambda c: (CONFIDENCE_RANK.get(c["confidence"], 3), c["source_rank"]))
    return out


def split_location(raw):
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) < 2:
        return None, None
    return parts[0], parts[-1]


def mark_failed(conn, job, error):
    conn.execute("UPDATE scrape_jobs SET status='failed', error=?, updated_at=? WHERE id=?",
                 (error, now(), job["id"]))
    conn.commit()


def ingest_file(conn, path):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not isinstance(data.get("fields") or {}, dict):
            raise ValueError("expected an object with a 'fields' object")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as e:
        job = job_by_domain(conn, path.stem)
        warn(f"{path.name}: invalid parser output ({e})")
        if job:
            mark_failed(conn, job, f"ingest: invalid JSON in {path.name}: {e}")
        return False

    domain = normalize_domain(str(data.get("domain") or path.stem))
    job = job_by_domain(conn, domain)
    if not job:
        warn(f"{path.name}: no scrape job for {domain}; register it with add_jobs.py")
        return False

    pages = {p["id"]: p for p in conn.execute(
        "SELECT id, url, source_type, fetched_at FROM raw_pages WHERE job_id = ? ORDER BY fetched_at",
        (job["id"],))}
    # Ordered by fetched_at, so the latest fetch of a URL wins.
    pages_by_url = {p["url"]: p for p in pages.values()}

    fields = data.get("fields") or {}
    cands = {f: candidates_for(fields.get(f), pages, pages_by_url) for f in FIELDS}
    chosen = {f: (c[0] if c else None) for f, c in cands.items()}
    # A year like "early 2010s" is useless; fall back to the next candidate that actually parses.
    chosen["founded_year"] = next(
        (c for c in cands["founded_year"] if parse_year(c["value"]) is not None), None)

    def val(f):
        return chosen[f]["value"] if chosen[f] else None

    name = clean_text(data.get("name")) or job["company_name"]
    rev_min, rev_max, rev_year = parse_money(val("revenue"))
    emp_min, emp_max = parse_employees(val("employees"))
    row = {
        "job_id": job["id"], "name": name, "domain": job["domain"],
        **{f: val(f) for f in DESCRIPTION_FIELDS},
        "revenue_raw": val("revenue"), "revenue_usd_min": rev_min, "revenue_usd_max": rev_max,
        "revenue_year": rev_year,
        "web_traffic_raw": val("web_traffic"), "monthly_visits": parse_visits(val("web_traffic")),
        "employees_raw": val("employees"), "employees_min": emp_min, "employees_max": emp_max,
        "founded_year": parse_year(val("founded_year")),
        "headquarters": val("headquarters"),
    }

    locations = {}
    for loc in data.get("locations") or []:
        if not isinstance(loc, dict):
            loc = {"value": loc}
        raw = clean_text(loc.get("value"))
        if raw is None:
            continue
        key = raw.casefold()
        if key in locations:
            locations[key]["is_hq"] |= bool(loc.get("is_hq"))
        else:
            locations[key] = {"raw": raw, "is_hq": bool(loc.get("is_hq"))}
    if row["headquarters"]:
        key = row["headquarters"].casefold()
        locations.setdefault(key, {"raw": row["headquarters"], "is_hq": True})["is_hq"] = True

    row["profile_text"] = "\n".join(
        t for t in [name] + [row[f] for f in DESCRIPTION_FIELDS] if t)
    present = sum(row[c] is not None for c in COVERAGE_COLUMNS) + (1 if locations else 0)
    row["field_coverage"] = round(present / (len(COVERAGE_COLUMNS) + 1), 4)

    ts = now()
    with conn:
        cols = list(row)
        conn.execute(
            f"INSERT INTO companies ({', '.join(cols)}, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(cols))}, ?, ?)"
            f" ON CONFLICT(domain) DO UPDATE SET"
            f" {', '.join(f'{c}=excluded.{c}' for c in cols)}, updated_at=excluded.updated_at",
            [row[c] for c in cols] + [ts, ts],
        )
        company_id = conn.execute("SELECT id FROM companies WHERE domain = ?",
                                  (job["domain"],)).fetchone()["id"]

        conn.execute("DELETE FROM field_sources WHERE company_id = ?", (company_id,))
        for f, cs in cands.items():
            for c in cs:
                conn.execute(
                    "INSERT INTO field_sources (company_id, field_name, raw_value, page_id,"
                    " source_url, confidence, chosen, extracted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (company_id, f, c["value"], c["page_id"], c["source_url"], c["confidence"],
                     int(c is chosen[f]), ts),
                )

        conn.execute("DELETE FROM company_locations WHERE company_id = ?", (company_id,))
        for loc in locations.values():
            city, country = split_location(loc["raw"])
            conn.execute(
                "INSERT INTO company_locations (company_id, location_raw, city, country, is_hq)"
                " VALUES (?, ?, ?, ?, ?)",
                (company_id, loc["raw"], city, country, int(loc["is_hq"])),
            )

        conn.execute("UPDATE scrape_jobs SET status='ingested', error=NULL, updated_at=? WHERE id=?",
                     (ts, job["id"]))
    print(f"ingested {job['domain']} coverage={row['field_coverage']:.2f}")
    return True


def refresh_embeddings(conn):
    from embed import MODEL_NAME, embed, to_blob

    stale = [r for r in conn.execute(
        "SELECT c.id, c.profile_text, e.text_hash, e.model FROM companies c"
        " LEFT JOIN embeddings e ON e.company_id = c.id WHERE c.profile_text IS NOT NULL")
        if r["text_hash"] != sha256(r["profile_text"]) or r["model"] != MODEL_NAME]
    if not stale:
        print("embeddings up to date")
        return
    vecs = embed([r["profile_text"] for r in stale])
    with conn:
        for r, v in zip(stale, vecs):
            conn.execute(
                "INSERT INTO embeddings (company_id, model, dim, vector, text_hash)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT(company_id) DO UPDATE SET"
                " model=excluded.model, dim=excluded.dim, vector=excluded.vector,"
                " text_hash=excluded.text_hash",
                (r["id"], MODEL_NAME, len(v), to_blob(v), sha256(r["profile_text"])),
            )
    print(f"embedded {len(stale)} companies")


def print_coverage(conn):
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    print(f"\ncoverage over {total} companies:")
    if not total:
        return
    for c in COVERAGE_COLUMNS:
        n = conn.execute(f"SELECT COUNT(*) FROM companies WHERE {c} IS NOT NULL").fetchone()[0]
        print(f"  {c:<28} {n:>5}/{total}  {n / total:6.1%}")
    n = conn.execute("SELECT COUNT(DISTINCT company_id) FROM company_locations").fetchone()[0]
    print(f"  {'locations':<28} {n:>5}/{total}  {n / total:6.1%}")
    avg = conn.execute("SELECT AVG(field_coverage) FROM companies").fetchone()[0]
    print(f"  {'mean field_coverage':<28} {avg:.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", help="ingest only this company's file")
    ap.add_argument("--parsed-dir", default=str(ROOT / "staging" / "parsed"))
    args = ap.parse_args()

    parsed_dir = Path(args.parsed_dir)
    if args.domain:
        files = [parsed_dir / f"{normalize_domain(args.domain)}.json"]
        if not files[0].exists():
            sys.exit(f"no parsed file {files[0]}")
    else:
        files = sorted(parsed_dir.glob("*.json"))

    conn = connect()
    ok = 0
    for f in files:
        if ingest_file(conn, f):
            # Every candidate is now in field_sources, so the parser file has served its purpose.
            # Files that failed stay behind for inspection.
            f.unlink()
            ok += 1
    print(f"{ok}/{len(files)} files ingested")
    refresh_embeddings(conn)
    print_coverage(conn)


if __name__ == "__main__":
    main()
