"""Rank companies against a query: BM25 + cosine -> pool -> LLM judge batches -> RRF.

    python ranker/rank.py retrieve --query "payments for SMBs" [--anchor stripe.com] [--pool 50] [--batches 5]
        [--mode similar|search|acquisition] [--expand TEXT ...] [--criteria-json PATH_OR_JSON]
    python ranker/rank.py combine --run 3 [--top 10] [--min-score 3] [--json]
    python ranker/rank.py show --run 3 [--top 10] [--json]

retrieve prints a JSON plan of judge batches; the skill runs one judge agent per batch
(the "judge_prompt" path it prints, one per mode), then calls combine, which loads the judge files into llm_judgments
and deletes them.
"""
import argparse
import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db import ROOT, connect, normalize_domain, now, sha256

PROMPT_VERSION = "v2"
# Each mode has its own rubric, so each gets its own prompt_version: judgments from one rubric
# must never be read back as another's. similar and search share the original prompt and its cache.
RANKER_DIR = Path(__file__).resolve().parent
MODE_JUDGE = {
    "similar": (PROMPT_VERSION, RANKER_DIR / "judge_prompt.md"),
    "search": (PROMPT_VERSION, RANKER_DIR / "judge_prompt.md"),
    "acquisition": ("acq-v1", RANKER_DIR / "judge_prompt_acquisition.md"),
}
CRITERIA_KEYS = ("countries_include", "countries_exclude", "employees_min", "employees_max",
                 "revenue_usd_min", "revenue_usd_max")
# Location text is messy ("USA", "U.S.", "CA"), so a country matches any of its spellings.
COUNTRY_ALIASES = {
    "united states": ["united states", "usa", "u.s.", "us", "america"],
    "united kingdom": ["united kingdom", "uk", "u.k.", "england", "britain"],
}
RRF_K = 60
DEFAULT_JUDGE_DIR = ROOT / "staging" / "judge"

STOPWORDS = set("""
a an and are as at be been but by for from has have in into is it its of on or our
that the their them they this to was were which while who will with we you your
also can more other such than these those through over not no all any most
companies company similar like competitors competitor
""".split())

PROFILE_FIELDS = [
    ("Description", "description"),
    ("Product", "product_description"),
    ("Business model", "business_model_description"),
    ("Customers", "customer_description"),
    ("Employees", "employees_raw"),
    ("Revenue", "revenue_raw"),
    ("Headquarters", "headquarters"),
]


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in STOPWORDS]


def bm25_scores(query, docs, k1=1.5, b=0.75):
    doc_tokens = [tokenize(d) for d in docs]
    n = len(doc_tokens)
    avgdl = sum(len(t) for t in doc_tokens) / n or 1.0
    df = Counter()
    for toks in doc_tokens:
        df.update(set(toks))
    # Unique query terms: a long anchor profile repeating a word shouldn't multiply its weight.
    q_terms = set(tokenize(query))
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms if df[t]}
    scores = []
    for toks in doc_tokens:
        tf = Counter(toks)
        norm = k1 * (1 - b + b * len(toks) / avgdl)
        scores.append(sum(w * tf[t] * (k1 + 1) / (tf[t] + norm) for t, w in idf.items() if tf[t]))
    return scores


def ranks_desc(scores):
    """1-based ranks by descending score; None scores rank after everything (len+1)."""
    present = sorted((i for i, s in enumerate(scores) if s is not None), key=lambda i: -scores[i])
    ranks = [len(present) + 1] * len(scores)
    for r, i in enumerate(present, 1):
        ranks[i] = r
    return ranks


def query_key(anchor_domain, query):
    return sha256((anchor_domain or "") + "|" + " ".join(query.lower().split()))


def prompt_version_for(mode):
    return MODE_JUDGE.get(mode, MODE_JUDGE["similar"])[0]


def load_criteria(value):
    """Parse --criteria-json: inline JSON object or a path to a JSON file."""
    text = value if value.lstrip().startswith("{") else Path(value).read_text()
    try:
        crit = json.loads(text)
    except json.JSONDecodeError as e:
        sys.exit(f"--criteria-json is not valid JSON: {e}")
    unknown = set(crit) - set(CRITERIA_KEYS) if isinstance(crit, dict) else {"<not an object>"}
    if unknown:
        sys.exit(f"--criteria-json: unknown keys {sorted(unknown)}; allowed: {list(CRITERIA_KEYS)}")
    return crit


def country_terms(country):
    c = country.strip().lower()
    return COUNTRY_ALIASES.get(c, [c])


def in_text(terms, text):
    return any(re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", text) for t in terms)


def apply_criteria(conn, cands, crit):
    """Hard filters. A company missing the filtered field is KEPT: unknown is not a failure, and
    scraped data is sparse enough that dropping unknowns would silently empty the pool."""
    loc = {}
    for r in conn.execute("SELECT company_id, location_raw, city, country FROM company_locations"):
        loc.setdefault(r["company_id"], []).append(
            " ".join(v for v in (r["location_raw"], r["city"], r["country"]) if v).lower())
    inc = [country_terms(c) for c in crit.get("countries_include") or []]
    exc = [country_terms(c) for c in crit.get("countries_exclude") or []]

    def overlaps(lo, hi, c_lo, c_hi):
        if lo is None and hi is None:
            return True
        lo, hi = (lo if lo is not None else hi), (hi if hi is not None else lo)
        return (c_lo is None or hi >= c_lo) and (c_hi is None or lo <= c_hi)

    kept = []
    for c in cands:
        text = " ".join(loc.get(c["id"], []) + [(c["headquarters"] or "").lower()]).strip()
        if text:
            if inc and not any(in_text(t, text) for t in inc):
                continue
            if any(in_text(t, text) for t in exc):
                continue
        if not overlaps(c["employees_min"], c["employees_max"],
                        crit.get("employees_min"), crit.get("employees_max")):
            continue
        if not overlaps(c["revenue_usd_min"], c["revenue_usd_max"],
                        crit.get("revenue_usd_min"), crit.get("revenue_usd_max")):
            continue
        kept.append(c)
    return kept


def midpoint(lo, hi):
    vals = [v for v in (lo, hi) if v is not None]
    return sum(vals) / len(vals) if vals else None


def size_fit(target, buyer, crit):
    """Add-on plausibility by size in [0,1], mean over employees and revenue (whichever the target
    has; None if neither). Per dimension: inside the explicit criteria range = 1; else
    -log10(target/buyer) clamped to [0,1] (a tenth of the buyer's size or less = 1, equal or larger
    = 0); 0.5 when the buyer's own size is unknown."""
    scores = []
    for lo, hi in (("employees_min", "employees_max"), ("revenue_usd_min", "revenue_usd_max")):
        t = midpoint(target[lo], target[hi])
        if t is None:
            continue
        c_lo, c_hi = crit.get(lo), crit.get(hi)
        b = midpoint(buyer[lo], buyer[hi]) if buyer else None
        if (c_lo is not None or c_hi is not None) and (c_lo is None or t >= c_lo) \
                and (c_hi is None or t <= c_hi):
            scores.append(1.0)
        elif b and t > 0:
            scores.append(min(1.0, max(0.0, -math.log10(t / b))))
        else:
            scores.append(0.5 if t > 0 else 1.0)
    return sum(scores) / len(scores) if scores else None


def competition_ranks(scores):
    """1-based competition ranks by descending score (ties share a rank); None -> len+1."""
    present = [s for s in scores if s is not None]
    return [len(scores) + 1 if s is None else 1 + sum(o > s for o in present) for s in scores]


def profile_block(row, heading):
    lines = [f"## {heading}", f"- Domain: {row['domain']}", f"- Name: {row['name']}"]
    for label, col in PROFILE_FIELDS:
        lines.append(f"- {label}: {row[col] if row[col] is not None else 'unknown'}")
    return "\n".join(lines)


def cmd_retrieve(args):
    import embed  # deferred so combine/show work without loading the model

    conn = connect()
    anchor = None
    if args.anchor:
        domain = normalize_domain(args.anchor)
        anchor = conn.execute("SELECT * FROM companies WHERE domain = ?", (domain,)).fetchone()
        if not anchor:
            print(f"ANCHOR_MISSING {domain}")
            sys.exit(2)

    mode = args.mode or ("similar" if anchor else "search")
    if mode == "acquisition" and not anchor:
        sys.exit("acquisition mode needs --anchor (the buyer)")
    if args.criteria_json and mode != "acquisition":
        sys.exit("--criteria-json only applies to --mode acquisition")
    criteria = load_criteria(args.criteria_json) if args.criteria_json else {}
    version, prompt_path = MODE_JUDGE[mode]

    query_doc = ((anchor["profile_text"] or "") if anchor else "") + "\n" + args.query
    # Each expansion is a separate query document: fusing their rank lists keeps one angle's
    # vocabulary from diluting another's, which concatenating them into one query would do.
    docs = [query_doc] + list(args.expand or [])
    cands = conn.execute(
        "SELECT * FROM companies WHERE profile_text IS NOT NULL AND id != ? ORDER BY id",
        (anchor["id"] if anchor else -1,),
    ).fetchall()
    if criteria:
        cands = apply_criteria(conn, cands, criteria)
    if not cands:
        sys.exit("no candidate companies with profile_text" + (" after criteria" if criteria else ""))

    texts = [c["profile_text"] for c in cands]
    bm25_lists = [bm25_scores(d, texts) for d in docs]

    # Only vectors from the current model are comparable with the query embedding.
    vecs = {
        r["company_id"]: embed.from_blob(r["vector"], r["dim"])
        for r in conn.execute("SELECT * FROM embeddings WHERE model = ?", (embed.MODEL_NAME,))
    }
    cos_lists = [[float(np.dot(q, vecs[c["id"]])) if c["id"] in vecs else None for c in cands]
                 for q in embed.embed(docs)]

    bm25_rls = [ranks_desc(l) for l in bm25_lists]
    cos_rls = [ranks_desc(l) for l in cos_lists]
    if len(docs) == 1:
        bm25, cosine = bm25_lists[0], cos_lists[0]
    else:
        # Stored per-signal score is the fused RRF over that signal's lists; ranks derive from it.
        bm25 = [sum(1 / (RRF_K + r[i]) for r in bm25_rls) for i in range(len(cands))]
        cosine = [sum(1 / (RRF_K + r[i]) for r in cos_rls) for i in range(len(cands))]
    bm25_rank = ranks_desc(bm25) if len(docs) > 1 else bm25_rls[0]
    cos_rank = ranks_desc(cosine) if len(docs) > 1 else cos_rls[0]
    rrf1 = [sum(1 / (RRF_K + r[i]) for r in bm25_rls + cos_rls) for i in range(len(cands))]
    pool = sorted(range(len(cands)), key=lambda i: -rrf1[i])[: args.pool]

    anchor_domain = anchor["domain"] if anchor else None
    # The judge sees the criteria in acquisition mode, so they are part of what a judgment answers.
    key_text = args.query
    if mode == "acquisition":
        key_text += "\nacquisition " + json.dumps(criteria, sort_keys=True)
    qkey = query_key(anchor_domain, key_text)
    params = {"pool": args.pool, "batches": args.batches, "k1": 1.5, "b": 0.75, "rrf_k": RRF_K,
              "embed_model": embed.MODEL_NAME, "prompt_version": version}
    if args.expand:
        params["expansions"] = list(args.expand)
    if criteria:
        params["criteria"] = criteria
    run_id = conn.execute(
        "INSERT INTO rank_runs (query_text, anchor_company_id, query_key, params_json, created_at, mode)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (args.query, anchor["id"] if anchor else None, qkey, json.dumps(params), now(), mode),
    ).lastrowid
    conn.executemany(
        "INSERT INTO rank_results (run_id, company_id, bm25, bm25_rank, cosine, cosine_rank)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(run_id, cands[i]["id"], bm25[i], bm25_rank[i], cosine[i], cos_rank[i]) for i in pool],
    )
    conn.commit()

    cached_ids = {
        r[0] for r in conn.execute(
            "SELECT candidate_company_id FROM llm_judgments WHERE query_key = ? AND prompt_version = ?",
            (qkey, version),
        )
    }
    judge_dir = Path(args.judge_dir)
    judge_dir.mkdir(parents=True, exist_ok=True)
    if mode == "acquisition":
        header = [f"# Acquisition thesis\n\n{args.query}",
                  profile_block(anchor, f"Buyer: {anchor['name']}"),
                  "# Criteria\n\n" + (json.dumps(criteria, indent=2) if criteria else "none")]
    else:
        header = [f"# Query\n\n{args.query}"]
        if anchor:
            header.append(profile_block(anchor, f"Anchor company: {anchor['name']}"))
    batches, cached = [], 0
    for k in range(args.batches):
        members = [cands[i] for i in pool[k :: args.batches]]
        todo = [c for c in members if c["id"] not in cached_ids]
        cached += len(members) - len(todo)
        if not todo:
            continue
        input_path = judge_dir / f"{run_id}_b{k}_input.md"
        blocks = [profile_block(c, f"Candidate: {c['domain']}") for c in todo]
        input_path.write_text("\n\n".join(header + ["# Candidates"] + blocks) + "\n")
        batches.append({
            "batch": k,
            "input": str(input_path),
            "output": str(judge_dir / f"{run_id}_b{k}.json"),
            "domains": [c["domain"] for c in todo],
        })

    print(json.dumps({"run_id": run_id, "query_key": qkey, "mode": mode,
                      "judge_prompt": str(prompt_path.relative_to(ROOT)), "pool_size": len(pool),
                      "batches": batches, "cached": cached}, indent=2))


def load_pool(conn, run_id):
    run = conn.execute("SELECT * FROM rank_runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        sys.exit(f"unknown run {run_id}")
    rows = conn.execute(
        "SELECT r.*, c.name, c.domain, c.employees_raw, c.revenue_raw, c.headquarters,"
        " c.employees_min, c.employees_max, c.revenue_usd_min, c.revenue_usd_max,"
        " c.field_coverage, j.score AS j_score, j.reason AS j_reason"
        " FROM rank_results r JOIN companies c ON c.id = r.company_id"
        " LEFT JOIN llm_judgments j ON j.candidate_company_id = r.company_id"
        "  AND j.query_key = ? AND j.prompt_version = ?"
        " WHERE r.run_id = ?",
        (run["query_key"], prompt_version_for(run["mode"]), run_id),
    ).fetchall()
    return run, [dict(r) for r in rows]


def ingest_judgments(conn, run, pool, judge_dir):
    by_domain = {p["domain"]: p["company_id"] for p in pool}
    rows, consumed = [], []
    for path in sorted(Path(judge_dir).glob(f"{run['id']}_b*.json")):
        try:
            items = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            print(f"WARNING: {path.name} is not valid JSON ({e}); kept for debugging", file=sys.stderr)
            continue
        clean = isinstance(items, list)
        for it in items if isinstance(items, list) else []:
            domain = normalize_domain(str(it.get("domain", ""))) if isinstance(it, dict) else ""
            score = it.get("score") if isinstance(it, dict) else None
            if (domain not in by_domain or not isinstance(score, int) or isinstance(score, bool)
                    or not 0 <= score <= 10):
                print(f"WARNING: {path.name}: invalid entry {it!r}; skipped", file=sys.stderr)
                clean = False
                continue
            rows.append((run["query_key"], by_domain[domain], prompt_version_for(run["mode"]), score,
                         it.get("reason")))
        if clean:
            consumed.append(path)
    conn.executemany(
        "INSERT OR REPLACE INTO llm_judgments"
        " (query_key, candidate_company_id, prompt_version, score, reason) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    # The judgments now live in llm_judgments; the files were only transport. Files with
    # problems stay behind so a bad judge output can be inspected and re-run.
    for path in consumed:
        path.unlink()
        path.with_name(path.stem + "_input.md").unlink(missing_ok=True)
    return len(rows)


def pool_ranks(pool, key):
    """Re-rank a stage-1 signal within the pool so it shares a 1..len(pool) scale with the LLM rank."""
    order = sorted(pool, key=lambda p: p[key])
    return {p["company_id"]: r for r, p in enumerate(order, 1)}


def cmd_combine(args):
    conn = connect()
    run, pool = load_pool(conn, args.run)
    n_new = ingest_judgments(conn, run, pool, args.judge_dir)
    # Reload so cached and freshly ingested judgments arrive through the same join.
    run, pool = load_pool(conn, args.run)
    print(f"ingested {n_new} judgments for run {run['id']}", file=sys.stderr)

    n = len(pool)
    missing = [p["domain"] for p in pool if p["j_score"] is None]
    if missing:
        print(f"WARNING: no judgment for {len(missing)} candidates: {', '.join(missing)}", file=sys.stderr)
    # Competition ranking: equal judge scores share a rank (two 9s are both rank 1, the next 8 is
    # rank 3); unjudged candidates rank after everything.
    scores = [p["j_score"] for p in pool if p["j_score"] is not None]
    tier_rank = {s: 1 + sum(o > s for o in scores) for s in set(scores)}
    tier_rank[None] = n + 1

    bm25_r = pool_ranks(pool, "bm25_rank")
    cos_r = pool_ranks(pool, "cosine_rank")
    acquisition = run["mode"] == "acquisition"
    if acquisition:
        buyer = conn.execute("SELECT * FROM companies WHERE id = ?", (run["anchor_company_id"],)).fetchone()
        crit = json.loads(run["params_json"] or "{}").get("criteria", {})
        for p in pool:
            p["fit_score"] = size_fit(p, buyer, crit)
        for p, r in zip(pool, competition_ranks([p["fit_score"] for p in pool])):
            p["fit_rank"] = r
    for p in pool:
        p["llm_rank"] = tier_rank[p["j_score"]]
        p["rrf1"] = 1 / (RRF_K + p["bm25_rank"]) + 1 / (RRF_K + p["cosine_rank"])
        ranks = [bm25_r[p["company_id"]], cos_r[p["company_id"]], p["llm_rank"]]
        if acquisition:
            ranks.append(p["fit_rank"])
        p["rrf_score"] = sum(1 / (RRF_K + r) for r in ranks)
    # The judge reads the query and both profiles, so its score decides the order. RRF (k=60)
    # compresses rank gaps too much for 11 score levels to outweigh BM25 + cosine agreeing, so it
    # only breaks ties within a score. Unjudged candidates go last.
    pool.sort(key=lambda p: (-(p["j_score"] if p["j_score"] is not None else -1),
                             -p["rrf_score"], -p["rrf1"]))
    for i, p in enumerate(pool, 1):
        p["final_rank"] = i
    conn.executemany(
        "UPDATE rank_results SET llm_score = ?, llm_rank = ?, rrf_score = ?, final_rank = ?,"
        " fit_score = ?, fit_rank = ? WHERE run_id = ? AND company_id = ?",
        [(p["j_score"], p["llm_rank"], p["rrf_score"], p["final_rank"], p.get("fit_score"),
          p.get("fit_rank"), run["id"], p["company_id"]) for p in pool],
    )
    conn.commit()
    render(run, pool, args)


def cmd_show(args):
    conn = connect()
    run, pool = load_pool(conn, args.run)
    if any(p["final_rank"] is None for p in pool):
        sys.exit(f"run {args.run} has not been combined yet")
    pool.sort(key=lambda p: p["final_rank"])
    render(run, pool, args)


def render(run, pool, args):
    bm25_r = pool_ranks(pool, "bm25_rank")
    cos_r = pool_ranks(pool, "cosine_rank")
    rows = [p for p in pool if p["j_score"] is None or p["j_score"] >= args.min_score][: args.top]
    out = [{
        "rank": i, "name": p["name"], "domain": p["domain"], "rrf": round(p["rrf_score"], 5),
        "bm25_rank": bm25_r[p["company_id"]], "cosine_rank": cos_r[p["company_id"]],
        "llm_score": p["j_score"], "fit_score": None if p["fit_score"] is None else round(p["fit_score"], 2),
        "fit_rank": p["fit_rank"], "reason": p["j_reason"], "employees_raw": p["employees_raw"],
        "revenue_raw": p["revenue_raw"], "headquarters": p["headquarters"],
        "field_coverage": None if p["field_coverage"] is None else round(p["field_coverage"], 2),
    } for i, p in enumerate(rows, 1)]
    acquisition = run["mode"] == "acquisition"
    if not acquisition:
        for r in out:
            del r["fit_score"], r["fit_rank"]
    if args.json:
        print(json.dumps({"run_id": run["id"], "query": run["query_text"], "results": out}, indent=2))
        return

    def cell(v):
        return "-" if v is None else str(v).replace("|", "/").replace("\n", " ")

    # Markdown, untruncated: the judge's reason is the explanation the user reads.
    cols = [("#", "rank"), ("company", "name"), ("domain", "domain"), ("rrf", "rrf"),
            ("bm25#", "bm25_rank"), ("cos#", "cosine_rank"), ("llm", "llm_score"),
            ("why", "reason"), ("employees", "employees_raw"), ("revenue", "revenue_raw"),
            ("hq", "headquarters"), ("cov", "field_coverage")]
    if acquisition:
        cols.insert(7, ("fit", "fit_score"))
    print(f"run {run['id']}: {run['query_text']}  (pool {len(pool)}, judge scores 0-10,"
          f" showing score >= {args.min_score})\n")
    print("| " + " | ".join(h for h, _ in cols) + " |")
    print("|" + "---|" * len(cols))
    for r in out:
        print("| " + " | ".join(cell(r[k]) for _, k in cols) + " |")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("retrieve")
    r.add_argument("--query", required=True)
    r.add_argument("--anchor")
    r.add_argument("--pool", type=int, default=50)
    r.add_argument("--batches", type=int, default=5)
    r.add_argument("--judge-dir", default=str(DEFAULT_JUDGE_DIR))
    r.add_argument("--mode", choices=["similar", "search", "acquisition"],
                   help="default: similar with an anchor, search without")
    r.add_argument("--expand", action="append", metavar="TEXT",
                   help="extra query document (repeatable); rank lists are fused with RRF")
    r.add_argument("--criteria-json", metavar="PATH_OR_JSON",
                   help="acquisition hard filters: countries_include/exclude, employees_min/max, revenue_usd_min/max")

    for name in ("combine", "show"):
        p = sub.add_parser(name)
        p.add_argument("--run", type=int, required=True)
        p.add_argument("--top", type=int, default=10)
        p.add_argument("--min-score", type=int, default=0,
                       help="hide candidates the judge scored below this (0-10)")
        p.add_argument("--json", action="store_true")
        if name == "combine":
            p.add_argument("--judge-dir", default=str(DEFAULT_JUDGE_DIR))

    args = ap.parse_args()
    {"retrieve": cmd_retrieve, "combine": cmd_combine, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
