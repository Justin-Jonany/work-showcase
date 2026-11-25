# Example: companies similar to Stripe

This is a condensed log of a real run, saved as run 4 in `data/companies.db`. The table is copied from `rank.py show --run 4`, trimmed to the most useful columns. I did not keep a transcript, so the commands below are reconstructed from the skill definition and the parameters saved in `rank_runs`. Replay the output with:

```
.venv/bin/python ranker/rank.py show --run 4
```

## The request

"companies similar to Stripe"

## What the skill did

1. **Parse the query.** Anchor: Stripe. Looked it up in the database:
   `sqlite3 data/companies.db "SELECT domain, name FROM companies WHERE name LIKE '%Stripe%' OR domain LIKE '%stripe%'"`
   It is there (`stripe.com`), so no scraping was needed. Mode: `similar`, because an anchor was named.
2. **Retrieve.** Parameters are the ones saved in `rank_runs` for run 4:
   `.venv/bin/python ranker/rank.py retrieve --query "companies similar to Stripe" --anchor stripe.com --pool 50 --batches 5`
   BM25 and cosine ranked all other companies. Stage-1 RRF kept the top 50. The pool was split into 5 strided batches of 10, one file per batch.
3. **Judge.** 5 Haiku subagents launched in one message, so they ran in parallel. Each got the fixed prompt `ranker/judge_prompt.md` with its batch's input and output paths.
4. **Combine.** `.venv/bin/python ranker/rank.py combine --run 4` loaded the 5 judge files into `llm_judgments`, computed RRF, ordered by judge score with RRF as the tie-break, and saved the result.

The saved run records the retrieve parameters. The exact `--top` and `--min-score` values passed to `combine` are not recorded.

## Output

Top 10 of run 4. `bm25#` and `cos#` are each signal's rank within the pool; `judge` is the 0 to 10 score.

| # | company | bm25# | cos# | judge | why | employees |
|---|---|---|---|---|---|---|
| 1 | Adyen | 1 | 4 | 9 | Direct competitor offering online and in-person payments, platforms, issuing and capital; differs mainly by its enterprise-only focus. | 4,345 (2024) |
| 2 | Worldpay | 3 | 2 | 9 | Direct competitor: accepts online and in-store payments, payouts and platform embedding for SMBs and enterprises; lacks stated billing and card-issuing products. | About 8,500 (2024) |
| 3 | Checkout.com | 4 | 3 | 9 | Direct competitor: enterprise payment acceptance, fraud tools and money movement, with a narrower product set than Stripe's billing, Atlas and platform offerings. | About 2,000 people |
| 4 | Payabli | 2 | 1 | 8 | Payments infrastructure for software platforms spanning acceptance, payouts and invoicing; much smaller, with a narrower product set. | 11-50 to about 120 (range across sources) |
| 5 | Square | 7 | 11 | 8 | Strong match in payments and checkout for in-person and online sales, but aimed at small businesses with POS hardware and less developer platform breadth. | about 6,000 (2026; Block, Inc.) |
| 6 | Global Payments | 8 | 10 | 8 | Direct competitor processing card, digital and omnichannel payments for merchants and financial institutions; transaction-based, with no stated billing, banking or issuing products. | About 27,000 as of 2024; nearly 24,000 shortly after the 2019 TSYS merger |
| 7 | Fiserv | 32 | 15 | 7 | Large payments processor and banking platform; its merchant processing and commerce orchestration overlap Stripe's payments, but its focus is banks and financial institutions. | About 38,000 (2024) |
| 8 | Lead Bank | 5 | 8 | 6 | Offers API-driven banking infrastructure, card programs and money movement for fintechs, overlapping Stripe Treasury and Issuing; lacks Stripe's merchant payments and checkout. | Approximately 504 total employees worldwide as of March 2026 (Revelio Labs) |
| 9 | PayPal | 20 | 12 | 6 | Overlaps in merchant payment processing and checkout, but its core product is a consumer digital wallet and BNPL rather than payments infrastructure. | About 23,800 (2025) |
| 10 | CapitalOS | 33 | 16 | 6 | Embedded cards, bill pay and collections for B2B platforms resemble Stripe Issuing and Connect, but it lacks payment acceptance and checkout. | 17 employees (StartupHub) |

## Reading the result

The top three, Adyen, Worldpay and Checkout.com, score 9. BM25 and cosine put them in their top four as well, so all three signals agree. Payabli is #1 on cosine and #2 on BM25 but gets an 8, because the judge notes it is much smaller with a narrower product set. Fiserv is deep in both retrieval lists (BM25 32, cosine 15) and still reaches #7, since the judge sees that its merchant processing overlaps Stripe's payments. That is what judge-first ordering is for.
