---
name: company-ranker
description: Rank companies in data/companies.db against a free-text query, which may name an anchor company ("companies similar to Stripe", "Stripe competitors for SMBs in Europe", "AI tools for legal teams"). Uses BM25 + model2vec cosine retrieval, Haiku judges on strided batches, and RRF fusion, with every run persisted and every judgment cached. Use when the user asks for similar companies, competitors, alternatives, or "companies that do X".
---

# company-ranker

Repo root: the directory containing `schema.sql`. Always run `.venv/bin/python` from the repo root.

## Modes

`retrieve --mode` picks the judge rubric; each mode has its own prompt file and `prompt_version`, so cached
judgments never mix between modes.

- **similar** (default when an anchor is named): companies like the anchor ("similar to Stripe"). Prompt `ranker/judge_prompt.md`.
- **search** (default with no anchor): companies matching a free-text description ("AI tools for legal teams"). Same prompt.
- **acquisition**: add-on targets for a buyer. The anchor is the **buyer**, the query is the **acquisition thesis**,
  and optional hard criteria filter the pool. Prompt `ranker/judge_prompt_acquisition.md`. See "Acquisition mode" below.

`--expand "<text>"` (repeatable, optional, all modes) adds an extra query document, e.g. the same need worded
another way. BM25 and cosine run once per document and all the rank lists are fused with RRF into the pool.
Without `--expand` the behavior is unchanged; the other modes rarely need it.

## Inputs

- `query` (required): free text.
- `top` (optional, default 10); `min_score` (optional, default 3: hide candidates the judge scored below it).

## Steps

### 0. Parse the query

Split it into:
- **anchor**: a specific company the query is relative to ("similar to Stripe", "Snowflake competitors",
  "alternatives to Gong"). Resolve it to its domain (`stripe.com`). Check the DB first:
  `sqlite3 data/companies.db "SELECT domain, name FROM companies WHERE name LIKE '%<name>%' OR domain LIKE '%<name>%'"`.
- **intent**: the full query text. Pass it unchanged as `--query`; constraints like "for SMBs in Europe" matter to the judges.

A query like "AI tools for legal teams" has no anchor.

### 1. Retrieve

```
.venv/bin/python ranker/rank.py retrieve --query "<query>" [--anchor <domain>] [--mode similar|search|acquisition] [--expand "<text>" ...] --pool 50 --batches 5
```

- **Exit code 2 with `ANCHOR_MISSING <domain>`**: the anchor isn't in the DB. Run the **company-scraper**
  skill in single-company mode for it (search → parse → `ingest.py --domain <domain>`), then re-run retrieve.
  This is the cache payoff: the anchor is scraped once, ever.
- Otherwise it prints JSON: `run_id`, `mode`, `judge_prompt`, `batches` (each with `input`, `output`, `domains`) and `cached`.
  The pool is the top 50 by stage-1 RRF(BM25, cosine), split into strided batches (batch k = pool indices
  i where i % 5 == k), so each judge sees a similar spread of strong and weak candidates. Pairs that were
  already judged (same query_key, candidate and prompt_version) are left out of the batches.

### 2. Judge (parallel, Haiku)

Read the file named by `judge_prompt` in the retrieve output once (`ranker/judge_prompt.md` for similar and search). For **every** entry in `batches`, launch a subagent with model `haiku`,
**all in one message so they run in parallel**. Each one gets the prompt text with `{INPUT_PATH}` and
`{OUTPUT_PATH}` replaced by that batch's `input` / `output`. Don't add any other instructions: the prompt is
fixed and versioned (`MODE_JUDGE` in rank.py). If `batches` is empty, everything is cached; skip to step 3.

### 3. Combine

```
.venv/bin/python ranker/rank.py combine --run <run_id> --top <top> --min-score <min_score>
```

This loads the judge JSON (scores 0–10) into `llm_judgments`, computes RRF (k=60) over bm25 / cosine /
llm ranks, orders the results by judge score with RRF breaking ties, writes `rank_results`, and prints a
markdown table. Judge files it loaded cleanly are deleted, because the
judgments now live in SQLite. Files with problems are kept, and a `WARNING: no judgment for ...` line names
the candidates without a score. Re-run that one batch's judge, then run combine again.

`show --run <id>` re-prints a past run without recomputing anything. Add `--json` for machine-readable output.

### 4. Present

Show the table exactly as combine prints it: rank, company, RRF, bm25/cos ranks, judge score (0–10), the
judge's **why** (always include it, untruncated and unedited: it is the explanation for each result),
employees, revenue, HQ and coverage. Under it, say in two or three lines what drove the top results:
which signals agreed, where BM25 and cosine disagreed with the judge, and which candidates have low `cov`
(sparse data). Provenance for any field:
`sqlite3 data/companies.db "SELECT field_name, raw_value, source_url, confidence, chosen FROM field_sources WHERE company_id=<id>"`.

## Acquisition mode

Use when the user wants add-on or bolt-on acquisition targets for a company ("what could Stripe acquire to
expand into lending", "tuck-in targets for Datadog under 200 employees in the US").

**Intake** (replaces step 0). Work out three things from the request:
- **buyer**: the acquirer, resolved to a domain as in step 0 (it becomes `--anchor`; `ANCHOR_MISSING` is handled the same way).
- **thesis**: what the acquisition should add, in a sentence or two (becomes `--query`).
- **criteria** (optional): a JSON object with any of `countries_include`, `countries_exclude`, `employees_min`,
  `employees_max`, `revenue_usd_min`, `revenue_usd_max`. Take only what the user stated; don't invent bounds.
  Companies with no data for a filtered field are kept, not dropped.

Ask the user only if the buyer or the thesis is missing. Don't ask about criteria.

**Expansions.** Write 3 short query documents, each a description of an ideal target seen from one angle,
in the vocabulary a target's profile would use:
1. product angle (what the target's product does),
2. customer angle (who buys it),
3. business-model angle (how it makes money).

**Run.**
```
.venv/bin/python ranker/rank.py retrieve --mode acquisition --anchor <buyer-domain> --query "<thesis>" \
  --expand "<product angle>" --expand "<customer angle>" --expand "<business-model angle>" \
  [--criteria-json '{"employees_max": 200, "countries_include": ["United States"]}'] --pool 50 --batches 5
```
Then judge (step 2, prompt `ranker/judge_prompt_acquisition.md`) and combine (step 3) as usual.

`combine` adds a **fit** column: a size-fit score in [0,1] (`-`/NULL when the target has no employee or revenue
data). It is high for a target much smaller than the buyer or inside the criteria range and low for one
comparable to or larger than the buyer. The order is judge score first; ties are broken by RRF over bm25,
cosine, llm and fit ranks. In step 4, mention fit and any criteria applied.

## Testing

Use `COMPANIES_DB=<tmpdir>/x.db` and `--judge-dir <tmpdir>` for both retrieve and combine. Never test against data/companies.db.
