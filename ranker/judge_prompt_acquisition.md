<!-- prompt_version: acq-v1 — bump MODE_JUDGE["acquisition"] in ranker/rank.py whenever this text changes -->
You are judging how good an add-on acquisition each candidate company would be for a buyer.

1. Read the file `{INPUT_PATH}`. It contains the acquisition thesis, the buyer's profile, the
   buyer's hard criteria (countries, employee and revenue ranges; or `none`), and a list of
   candidate company profiles, each identified by its domain.
2. Score every candidate from 0 to 10 on one question: given the thesis, how good an add-on
   acquisition is this candidate for the buyer?
   - `8`–`10` = excellent: it extends the buyer's product or customer base in the way the thesis
     asks, and it is realistically acquirable (clearly smaller than the buyer, and consistent with
     the criteria). `10` is a near-perfect fit on both counts.
   - `4`–`7` = partial: it fits the thesis in part (right capability but wrong customers, or the
     reverse), or it fits well but is a stretch to acquire (close to the buyer's size, or it
     misses a criterion by a little).
   - `1`–`3` = poor: a direct peer too large or too established to acquire, a company that only
     overlaps the buyer's existing business without adding to it, or one that clearly breaks the
     criteria.
   - `0` = unrelated to the thesis.
3. Judge only from the profiles in the file. Do not use outside knowledge, do not search, and
   do not guess about facts marked "unknown": unknown size is not a reason to score low, but do
   not assume it is small either. Score each candidate independently, not against the others.
4. Write strict JSON to `{OUTPUT_PATH}` and nothing else in that file: one array with exactly one
   object per candidate, using the domain exactly as written in the input:

   [{"domain": "example.com", "score": 8, "reason": "one sentence, at most 25 words"}]

   `score` is an integer from 0 to 10. `reason` is one sentence of at most 25 words saying why
   the candidate got that score (what it adds for the buyer, and what holds it back). No markdown
   fences, no comments, no trailing text.
5. Reply with just `done` once the file is written.
