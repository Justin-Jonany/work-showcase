# Example: acquisition targets for Datadog

This is a condensed log of a real run, saved as run 8 in `data/companies.db`. The table is copied from `rank.py show --run 8`, trimmed to the most useful columns. I did not keep a transcript, so the commands below are reconstructed from the skill definition and the parameters saved in `rank_runs`. Replay the output with:

```
.venv/bin/python ranker/rank.py show --run 8
```

## The request

"What could Datadog acquire to move into feature flagging and experimentation? Targets under 500 employees."

## What the skill did

1. **Intake.** The harness read the request as acquisition mode and worked out three things:
   - buyer: Datadog, resolved to `datadoghq.com`, which is in the database;
   - thesis: "Add-on acquisitions that extend Datadog beyond observability into feature flagging, experimentation and release management for engineering teams";
   - criteria: `{"employees_max": 500}`, the only bound the user stated.
2. **Three query expansions**, one per angle (product, customer, business model), taken from the saved run's `params_json`:
   - "Feature flag and experimentation platform: toggle features, run A/B tests, progressive rollouts and release controls through SDKs"
   - "Bought by software engineering, DevOps and product teams at technology companies that ship code continuously"
   - "Subscription SaaS, often open-source core with a paid cloud tier, priced by seats or usage"
3. **Retrieve.**
   ```
   .venv/bin/python ranker/rank.py retrieve --mode acquisition --anchor datadoghq.com \
     --query "Add-on acquisitions that extend Datadog beyond observability into feature flagging, experimentation and release management for engineering teams" \
     --expand "Feature flag and experimentation platform: toggle features, run A/B tests, progressive rollouts and release controls through SDKs" \
     --expand "Bought by software engineering, DevOps and product teams at technology companies that ship code continuously" \
     --expand "Subscription SaaS, often open-source core with a paid cloud tier, priced by seats or usage" \
     --criteria-json '{"employees_max": 500}' --pool 50 --batches 5
   ```
   The criteria filter ran first, then BM25 and cosine ran once for the thesis and once per expansion (four query documents each) and were fused with RRF. The pool came out at 43 candidates, because fewer than 50 companies passed the filter.
4. **Judge.** 5 Haiku subagents in parallel, one per strided batch, with `ranker/judge_prompt_acquisition.md`. They scored all 43 candidates.
5. **Combine.** `.venv/bin/python ranker/rank.py combine --run 8`. Order is judge score first. Ties are broken by RRF over the BM25, cosine, judge and `fit` rank lists. `fit` is the size-fit score; `-` means the company has no employee or revenue data.

## Output

Top 15 of 43 for run 8.

| # | company | bm25# | cos# | judge | fit | why | employees |
|---|---|---|---|---|---|---|---|
| 1 | Flagsmith | 3 | 8 | 10 | 1.0 | Feature flags, A/B testing and governance match the thesis exactly, serve engineering teams, and it is tiny at about 12 people. | approximately 12 people (as of 2026, Latka) |
| 2 | GrowthBook | 2 | 11 | 10 | 1.0 | Warehouse-native experimentation and feature flag platform matching the thesis exactly, with 11-50 employees and small revenue, so clearly acquirable. | 11-50 employees (Promptloop) |
| 3 | LaunchDarkly | 1 | 3 | 9 | 1.0 | Feature management leader directly covering flagging, experimentation and release management; 450 employees fits the 500 cap and is far smaller than Datadog. | 450 employees (Bitscale profile, last updated Feb 2026) |
| 4 | Unleash | 5 | 12 | 8 | - | Feature flagging and FeatureOps for enterprise engineering teams matches the thesis closely; size is unknown and it may be a stretch to acquire. | - |
| 5 | CircleCI | 9 | 16 | 3 | 1.0 | CI/CD delivery tool overlapping Datadog's software delivery product rather than adding flags or experimentation; 201-500 employees is a stretch to acquire. | 201-500 (ZoomInfo) |
| 6 | Honeycomb | 4 | 1 | 2 | 1.0 | Direct observability peer that overlaps Datadog's existing business rather than adding feature flagging, experimentation or release management. | 140 Total Employees (Built In) |
| 7 | Sentry | 6 | 7 | 2 | 1.0 | Developer error tracking and performance monitoring overlaps Datadog's existing observability rather than extending into flags, experimentation or release management; size fits. | About 180 (Levels.fyi) to 391 (getlatka), with 428 and 447 in other listings |
| 8 | Redpanda | 13 | 28 | 2 | 1.0 | Kafka-compatible data streaming and agent data platform; adds data infrastructure but not feature flagging, experimentation, or release management for engineers. | 197 total (Built In) |
| 9 | Sierra | 39 | 9 | 2 | 1.0 | Customer-experience AI with an experiments feature, but it serves customer-facing business teams, not engineering teams, so the fit is weak. | 165 (Latka, January 2025) |
| 10 | JFrog | 7 | 5 | 2 | - | Large software supply chain and artifact security platform outside flags, experimentation and release management, and too large to be realistically acquired. | - |
| 11 | Monte Carlo | 37 | 40 | 2 | 1.0 | Data and AI observability overlaps Datadog's existing monitoring rather than extending into flags, experimentation or release management; size fits. | 200+ employees (Salestools) |
| 12 | RisingWave | 8 | 2 | 1 | 1.0 | Streaming data infrastructure for AI agents, not feature flagging or release management; adds a data-layer capability at best, with unknown revenue. | ~43 employees (one profile) |
| 13 | Materialize | 10 | 6 | 1 | 1.0 | Streaming SQL and live data context for agents adds nothing to feature flagging, experimentation or release management; small size fits criteria. | 75 total employees (Built In) |
| 14 | Airbyte | 12 | 13 | 1 | 1.0 | Open-source data movement pipelines, adjacent to engineering tooling but outside feature flagging, experimentation or release management. | 124 (Datanyze) |
| 15 | SingleStore | 43 | 17 | 1 | 1.0 | Real-time database is adjacent to Datadog's data stack but adds no feature flagging, experimentation or release capability. | About 380 |

## Reading the result

Four candidates scored 8 or higher: Flagsmith, GrowthBook, LaunchDarkly and Unleash. 28 of the 43 scored 0. Unleash has no size data (`fit` is `-`), and the judge says so in its reason and marks it a possible stretch. Honeycomb and Sentry rank high on retrieval (Honeycomb is #1 on cosine) because they look like Datadog, and the judge scores both 2 because they add no flags, experiments or release management. With judge-first ordering that retrieval signal cannot lift them.
