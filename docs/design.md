# Design

Two Claude Code skills on top of one SQLite database (`data/companies.db`):

1. **company-scraper**: a prompt, then subagents doing WebSearch and WebFetch that save raw results, then Haiku parsers, then `ingest.py` normalizes everything into SQLite.
2. **company-ranker**: a free-text query (optionally naming an anchor company), then BM25 and cosine retrieval, then Haiku judges on strided batches, then a final order that puts the judge score first and uses RRF as a tie-break.

Why not just ask a chat model: the data is cached and structured, and every field links back to the page it came from. Rankings are reproducible, the score breakdown is visible, and I never pay for the same web search twice.

## Layout

```
schema.sql
db.py               connect(): WAL, foreign keys, busy_timeout; shared helpers
embed.py            model2vec embeddings, shared by ingest (write) and the ranker (query)
scraper/
  add_jobs.py       discovery agent registers companies -> scrape_jobs
  list_jobs.py      jobs by status, split into batches for subagents
  save_page.py      search agents store a result or fetched page -> raw_pages
  export_pages.py   print a company's stored pages for a parser agent
  normalize.py      messy strings -> numbers (revenue, employees, visits, year)
  ingest.py         parsed JSON -> companies / locations / field_sources / embeddings
  prompts/          discover.md, search.md, parse.md (the subagent prompts)
ranker/
  rank.py           retrieve -> judge batches -> combine; show re-prints a saved run
  judge_prompt.md              rubric for similar and search modes
  judge_prompt_acquisition.md  rubric for acquisition mode
.claude/skills/company-scraper/SKILL.md
.claude/skills/company-ranker/SKILL.md
staging/parsed/<domain>.json         parser output, deleted once ingested
staging/judge/<run>_b<k>.json        judge output, deleted once combined
```

The environment is a project `.venv` with `numpy` and `model2vec` (static embeddings, `minishlab/potion-base-8M`, 256 dimensions; small, CPU only, no torch). BM25 is implemented inline in about 30 lines. `COMPANIES_DB` points any CLI at a scratch database, which is how I test without touching the real one.

## Tables

### `scrape_jobs`: work queue, makes the scraper resumable and idempotent

| col | type | notes |
|---|---|---|
| id | INTEGER PK | |
| company_name | TEXT NOT NULL | as found by discovery or given by the user |
| domain | TEXT UNIQUE NOT NULL | normalized: lowercase, no scheme, no `www.` |
| sector | TEXT | discovery tag; not used for ranking |
| prompt | TEXT | the prompt that produced this job |
| status | TEXT NOT NULL | `pending`, `searched`, `parsed`, `ingested` or `failed` |
| error | TEXT | last error |
| created_at, updated_at | TEXT | ISO-8601 UTC |

### `raw_pages`: everything scraped, verbatim (the messy layer)

| col | type | notes |
|---|---|---|
| id | INTEGER PK | |
| job_id | INTEGER FK to scrape_jobs | |
| url | TEXT NOT NULL | |
| source_type | TEXT NOT NULL | `company_site`, `wikipedia`, `news`, `data_provider` (Crunchbase, LinkedIn, Similarweb and the like) or `other` |
| retrieval | TEXT NOT NULL | `search_snippet` (WebSearch result) or `fetched_page` (WebFetch output) |
| query | TEXT | the search that surfaced it |
| text | TEXT NOT NULL | the returned text, stored as-is, capped at 20,000 characters |
| content_hash | TEXT | sha256(text); identical content is not inserted twice |
| fetched_at | TEXT | |

`UNIQUE(job_id, url, content_hash)`. WebSearch and WebFetch return model-written summaries, not raw HTML. The pages are verbatim copies of what the tools returned, and a production version would use a raw search API.

### `companies`: one normalized row per company

| col | type | notes |
|---|---|---|
| id | INTEGER PK | |
| job_id | INTEGER FK UNIQUE | |
| name | TEXT NOT NULL | |
| domain | TEXT UNIQUE NOT NULL | the dedupe key |
| description, product_description, business_model_description, customer_description | TEXT | one to three sentences each; what it is, what it sells, how it earns, who buys |
| revenue_raw | TEXT | verbatim, e.g. "$1.6 billion (fiscal 2023)" |
| revenue_usd_min, revenue_usd_max | REAL | parsed; a point estimate gives min = max |
| revenue_year | INTEGER | |
| web_traffic_raw, monthly_visits | TEXT, INTEGER | verbatim and parsed |
| employees_raw | TEXT | verbatim, e.g. "1,001-5,000" |
| employees_min, employees_max | INTEGER | parsed |
| founded_year | INTEGER | |
| headquarters | TEXT | as found |
| profile_text | TEXT | the company name plus the four description fields, newline-joined; this is the retrieval document |
| field_coverage | REAL | fraction of 11 target fields that are non-null: name, the four descriptions, revenue, web traffic, employees, founded year, headquarters, and at least one location |
| created_at, updated_at | TEXT | |

Missing is NULL, never an empty string or 0.

### `company_locations`

`company_id`, `location_raw`, `city`, `country` (a best-effort split on commas: first part and last part), `is_hq`. `UNIQUE(company_id, location_raw)`.

### `field_sources`: provenance for every extracted value

| col | type | notes |
|---|---|---|
| company_id | INTEGER FK | |
| field_name | TEXT NOT NULL | `description`, `revenue`, `employees` and so on |
| raw_value | TEXT NOT NULL | exactly what the parser pulled |
| page_id | INTEGER FK to raw_pages | NULL if not resolvable |
| source_url | TEXT | |
| confidence | TEXT | `high` (stated by the company, Wikipedia or a filing), `medium` (third-party estimate), `low` (inferred) |
| chosen | INTEGER | 1 if this value won conflict resolution |
| extracted_at | TEXT | |

Every candidate the parser found is stored, so conflicts stay visible. When sources disagree, the winner is picked by higher confidence, then source priority (`company_site` over `wikipedia` over `data_provider` over `news` over `other`), then the most recently fetched page. Anything left tied keeps the order the parser listed it in. For `founded_year`, a candidate that does not parse to a year ("early 2010s") is skipped in favour of the next one that does.

### `embeddings`

`company_id` (PK), `model`, `dim`, `vector` (float32 little-endian, L2-normalized, stored as a BLOB), `text_hash` (sha256 of `profile_text`). `ingest.py` re-embeds a company only when the hash or the model changes. Because rows are unit length, a dot product is the cosine.

### `llm_judgments`: cache so the same question is never judged twice

| col | notes |
|---|---|
| query_key | sha256 of the anchor domain and the normalized query text, joined by a vertical bar. In acquisition mode the criteria are appended, because the judge sees them |
| candidate_company_id | |
| prompt_version | `v2` for similar and search, `acq-v1` for acquisition; bump it when the prompt text changes |
| score | 0 to 10 |
| reason | one line |

The primary key is `(query_key, candidate_company_id, prompt_version)`. Because the version is in the key, a rubric change never reads back an old rubric's scores. The key has no hash of the company profile, so a re-scraped company keeps its old judgment. That is a known gap.

### `rank_runs` and `rank_results`: audit trail for every ranking

`rank_runs`: id, query_text, anchor_company_id (nullable), query_key, mode (`similar`, `search` or `acquisition`), params_json (pool size, batches, BM25 and RRF constants, embedding model, prompt version, and in acquisition mode the expansions and criteria), created_at.

`rank_results`: run_id, company_id, bm25, bm25_rank, cosine, cosine_rank, llm_score, llm_rank, rrf_score, final_rank, fit_score, fit_rank. `fit_*` are NULL outside acquisition mode.

`rank.py show --run N` prints any saved run from these rows without recomputing, which is what makes the runs in `examples/` replayable.

## Skill 1: company-scraper

There is no HTTP scraper of my own. The skill orchestrates subagents that use Claude's built-in WebSearch and WebFetch. Input is a free-form prompt: "B2B payments companies", "20 data-infrastructure startups", or a single company name such as "Ramp", which the ranker uses on demand.

1. **Discover** (one Sonnet subagent, skipped when the prompt names one company). It turns the prompt into companies with domains and registers each with `add_jobs.py`. Domains are normalized and deduped, so a company already in the database comes back as `exists`.
2. **Search** (Sonnet subagents in parallel, 5 companies per batch from `list_jobs.py`). For each company: two WebSearch queries, run one at a time because parallel calls get rate-limited, then a WebFetch of the homepage and of Wikipedia. Each useful result is stored verbatim with `save_page.py`. The agents only store; they never extract. The job becomes `searched`. WebSearch allows 200 calls per turn shared by all agents, so two searches per company is what makes about 100 companies per turn possible. At most 20 agents run at once.
3. **Parse** (Haiku subagents in parallel). Each runs `export_pages.py`, reads the stored pages and writes `staging/parsed/<domain>.json`. Every field is a list of candidates, one per page that states it, each with `page_id`, `source_url` and `confidence`. Conflicting values are kept, not merged. Search snippets are capped at `medium` because they are model-written summaries. The parser uses only the stored pages and writes nothing it was not shown.
4. **Ingest** (`ingest.py`, the only writer of `companies`). It validates the JSON, runs `normalize.py` on the numeric fields, resolves conflicts, writes `companies`, `company_locations` and `field_sources`, builds `profile_text` and `field_coverage`, and refreshes embeddings. Files that fail validation stay in `staging/parsed/` and the job is marked `failed`.

Concurrency: SQLite runs in WAL mode with a 30-second busy timeout. Agents write small rows through the CLIs. Parser output goes to JSON files and a single writer loads it.

`normalize.py` handles forms such as `$1.2B`, `€40 million`, `~$10-50M`, `USD 3.4bn (FY2023)`, `1,001-5,000 employees`, `10k+`, `2.3M visits/mo` and `est. 2011`. Non-USD amounts use a static FX table dated mid-2024, which is good enough for size bucketing and not for accounting. `normalize.py --test` runs its self-test.

The database in this repo was built by running the skill on five sector prompts (payments, dev tools, data infrastructure, sales and CRM, applied AI), 20 companies each, plus one anchor company, 101 in total. Mean field coverage is 0.90.

## Skill 2: company-ranker

Input: a free-text query, plus optional `--top N` (default 10) and `--min-score` (hide candidates the judge scored below it).

### Modes

`retrieve --mode` selects the judge rubric. Each mode has its own prompt file and `prompt_version`, so cached judgments never mix between modes.

| mode | when | anchor means | prompt | version |
|---|---|---|---|---|
| similar | an anchor is named ("similar to Stripe") | the company to resemble | `judge_prompt.md` | `v2` |
| search | no anchor ("AI tools for legal teams") | none | `judge_prompt.md` | `v2` |
| acquisition | add-on targets for a buyer | the buyer | `judge_prompt_acquisition.md` | `acq-v1` |

### Steps

0. **Parse the query.** The harness splits the request into an optional anchor company and the intent text. If the anchor is not in `companies`, `retrieve` exits with code 2 and `ANCHOR_MISSING <domain>`, and the scraper skill collects that one company first. The anchor is scraped once; every later query about it is instant.
1. **Two retrievers over `profile_text`, excluding the anchor.**
   - BM25 (k1=1.5, b=0.75). The query document is the anchor's `profile_text` (if any) plus the query text, tokenized with stopwords removed and unique terms only, so a long profile that repeats a word does not multiply its weight. It catches exact vocabulary.
   - Cosine on model2vec vectors. It catches paraphrase ("checkout API" and "payment infrastructure").
2. **Expansions.** `--expand "<text>"` (repeatable, any mode) adds another query document. BM25 and cosine run once per document and every rank list is fused with RRF. Each expansion stays a separate document because concatenating them would let one angle's vocabulary dilute another's. The stored bm25 and cosine columns are the fused RRF over each signal's lists, and the ranks derive from them.
3. **Stage-1 RRF builds the pool.** `rrf1(d) = sum over lists of 1/(60 + rank)`, over all BM25 and cosine lists. The top 50 form the pool. A candidate missing from a list ranks `len + 1` in it.
4. **Strided batches, Haiku judges in parallel.** Sort the pool by `rrf1` and give pool index `i` to batch `i % 5`. Batch 0 gets ranks 1, 6, 11 and so on up to 46; batch 1 gets 2, 7, 12 and so on up to 47. Every batch then has the same spread of strong and weak candidates, so a judge's calibration does not depend on whether it happened to be handed all good or all bad companies. The skill launches one Haiku subagent per batch in a single message, so they run in parallel, each with the fixed, versioned prompt for the mode. A judge reads one input file (the query, the anchor or buyer profile, about 10 candidate profiles) and writes strict JSON: `[{"domain", "score" 0-10, "reason" up to 25 words}]`. Pairs already in `llm_judgments` for the same query key, candidate and prompt version are left out of the batches, so only uncached pairs are judged. Judges see only the profiles: no outside knowledge, no search, and unknown stays unknown.
5. **Combine.** `combine` validates and loads the judge files into `llm_judgments`, then computes the final order. A file that loads cleanly is deleted, because the judgments now live in SQLite. A file with problems is kept and a warning names the candidates without a score; rerun that one judge and combine again.

### Ranking math

Per-signal ranks are re-ranked within the pool so they share a 1 to pool-size scale with the judge rank. The judge rank is a competition rank: equal scores share a rank, so two 9s are both rank 1 and the next 8 is rank 3. Unjudged candidates rank last.

```
rrf(d) = sum over signals s of 1 / (60 + rank_s(d))
         s in {bm25, cosine, llm}                  similar, search
         s in {bm25, cosine, llm, fit}             acquisition

final order = sort by (judge score desc, rrf desc, stage-1 rrf desc)
```

Why RRF: BM25 and cosine are on incomparable scales, and RRF fuses their ranks with no calibration and no weights to tune. Why the judge leads: it is the only signal that reads the query and both profiles. RRF with k=60 compresses rank gaps, so if it decided the order, two retrieval signals that agreed could outvote the judge. RRF only breaks ties within a score. The cost is that a wrong judge score is not corrected by anything.

### Acquisition mode

For add-on or bolt-on targets ("what could Stripe acquire to expand into lending").

- **Intake.** The harness works out the buyer (resolved to a domain and passed as `--anchor`), the thesis (passed as `--query`), and optional criteria. It asks the user only if the buyer or the thesis is missing, and takes only the bounds the user stated.
- **Criteria** (`--criteria-json`): any of `countries_include`, `countries_exclude`, `employees_min`, `employees_max`, `revenue_usd_min`, `revenue_usd_max`. They are hard filters applied before retrieval. Country matching accepts common spellings ("USA", "U.S." for the United States). A company with no data for a filtered field is kept, not dropped, because scraped data is sparse enough that dropping unknowns would silently empty the pool. Employee and revenue ranges match when they overlap the bound. The criteria are part of the cache key and the judge sees them.
- **Three expansions**, each an ideal target seen from one angle in the vocabulary a target's profile would use: product, customer, business model.
- **Fit score** (`fit`, in [0,1], shown as `-` when the target has neither employee nor revenue data). For employees and revenue separately, a target inside the explicit criteria range scores 1. Otherwise it is `-log10(target / buyer)` clamped to [0,1], so a target a tenth of the buyer's size or smaller scores 1 and one the same size or larger scores 0. It is 0.5 when the buyer's own size is unknown. The score is the mean over the dimensions the target has. It enters the RRF as a fourth rank list and does not override the judge.
- **Judge prompt** asks one question: given the thesis, how good an add-on acquisition is this candidate, in terms of both fit with the thesis and realistic acquirability. It is told that unknown size is not a reason to score low, and not to assume small either.

### Output

A markdown table: rank, company, domain, RRF, BM25 and cosine ranks, judge score, the judge's reason (always untruncated), employees, revenue, headquarters and data coverage, plus `fit` in acquisition mode. The run is persisted to `rank_runs` and `rank_results`. The skill then says in two or three lines what drove the top results, where retrieval and the judge disagreed, and which candidates have low coverage.

## Testing

Everything that matters runs without an agent. `rank.py`, `ingest.py` and `normalize.py` are plain CLIs, so I test them with `COMPANIES_DB=<tmpdir>/x.db` and `--judge-dir <tmpdir>` and never against the real database. The agent-driven parts (search, parse, judge) have no automated tests; I check them by reading what they stored in the database.
