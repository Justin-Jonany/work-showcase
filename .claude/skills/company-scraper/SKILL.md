---
name: company-scraper
description: Build or extend the company database (data/companies.db) from a free-text prompt such as "B2B payments companies", "20 data-infrastructure startups" or a single company name like "Ramp". Discovers companies, collects raw web evidence with WebSearch/WebFetch subagents, parses it with Haiku subagents and ingests it into SQLite with per-field source URLs. Use when the user asks to scrape, collect, add or refresh companies, or when the company-ranker skill reports ANCHOR_MISSING.
---

# company-scraper

Orchestrates subagents; there is no HTTP scraper of our own. Repo root is the
directory containing `schema.sql`. Always run Python as `.venv/bin/python` from the
repo root (the system numpy is broken). Every stage is idempotent and resumable through `scrape_jobs.status`
(`pending` → `searched` → `ingested` / `failed`), so a rerun never repeats a search for a company that's already done.

## Inputs

- `prompt` (required): a sector description ("applied AI for legal") or one company ("Ramp", "ramp.com").
- `count` (optional, default 20): how many companies to discover for a sector prompt.

## Steps

### 1. Discover (skipped in single-company mode)

Launch **one** subagent (model `sonnet`) with the contents of `scraper/prompts/discover.md`, with
`{PROMPT}` and `{COUNT}` filled in. It registers companies through `scraper/add_jobs.py`. Domains are
normalized and deduped, so a company already in the DB comes back as `exists` (a cache hit).

**Single-company mode** (the prompt names one company, or the ranker asks for an anchor): don't run discovery.
Resolve the company's primary domain (one WebSearch if you're not sure), then register it yourself:

```
.venv/bin/python scraper/add_jobs.py --name "<Name>" --domain <domain> --sector "<tag or 'anchor'>" --prompt "<prompt>"
```

If it prints `exists ... status=ingested`, stop: it's cached.

### 2. Search (parallel)

```
.venv/bin/python scraper/list_jobs.py --status pending --batch-size 5
```

For each batch, launch a subagent (model `sonnet`) **all in one message so they run in parallel**, with this prompt:

> Read scraper/prompts/search.md (relative to the repo root) and follow it exactly.
> {DOMAINS} = <comma-separated "Name (domain)" entries>. 2 searches per company.

(Names come from `scrape_jobs.company_name`.) WebSearch allows 200 calls per turn shared by all agents, so
the prompt uses 2 searches per company plus homepage and Wikipedia fetches. That covers about 100 companies
per turn. Launch at most 20 agents at once (the concurrent-subagent limit). The agents store the tools' returned
text in `raw_pages` through `save_page.py` (WebSearch and WebFetch return model-written summaries; a production
version would use a raw search API), then mark each job `searched`. When they're done, check the counts:

```
sqlite3 data/companies.db "SELECT j.domain, j.status, COUNT(p.id) FROM scrape_jobs j LEFT JOIN raw_pages p ON p.job_id=j.id GROUP BY j.id ORDER BY 3"
```

Re-run search for any job that's still `pending` or has 0 pages.

### 3. Parse (parallel, Haiku)

```
.venv/bin/python scraper/list_jobs.py --status searched --batch-size 5
```

For each batch, launch a subagent (model `haiku`) **all in parallel** with the contents of
`scraper/prompts/parse.md`, with `{DOMAINS}` set to that batch's domains. Each writes
`staging/parsed/<domain>.json` (a list of candidates per field, each with page_id / source_url / confidence).

### 4. Ingest (single writer)

```
.venv/bin/python scraper/ingest.py            # or --domain <domain> for one company
```

Validates the JSON, normalizes revenue / employees / visits / year (`scraper/normalize.py`), resolves conflicts
(confidence, then source priority company_site > wikipedia > data_provider > news > other, then most recent),
writes `companies`, `company_locations` and `field_sources` (every candidate, with `chosen=1` on the winner),
refreshes model2vec embeddings, and prints coverage for each field. Report that coverage table to the user.
Each parsed file is deleted once it's ingested (every candidate is in `field_sources`). Files that failed
validation stay in `staging/parsed/` and their job is marked `failed`.

## Single-company mode used by the ranker

When company-ranker gets `ANCHOR_MISSING <domain>`: run step 1 single-company mode, step 2 with one search
agent for just that company, step 3 with one Haiku parser, then `ingest.py --domain <domain>`. After that,
return to the ranker.

## Rules

- Search agents never summarize; parser agents use only stored pages; `ingest.py` is the only thing that writes `companies`.
- Missing data is NULL. Never fill fields from model knowledge.
- Testing: set `COMPANIES_DB=<tmpdir>/x.db` and `ingest.py --parsed-dir <tmpdir>`. Never test against data/companies.db.
