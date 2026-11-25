# Example: where a number came from

This is a condensed log of a real lookup against the shipped database. The output is copied from `sqlite3` runs, with the columns trimmed. Checkout.com is the #3 result in the "companies similar to Stripe" example, so you can follow one row of that table back to its sources.

## The request

"Where does Checkout.com's employee count come from? Do the sources agree?"

## What the skill did

The ranker skill points at this query for provenance on any field. Every value the parser found is stored in `field_sources`, with the page it came from in `raw_pages`:

```
sqlite3 -header -column data/companies.db "SELECT f.field_name AS field, f.raw_value, f.confidence AS conf, f.chosen, p.source_type, p.retrieval FROM field_sources f LEFT JOIN raw_pages p ON p.id = f.page_id WHERE f.company_id = (SELECT id FROM companies WHERE domain = 'checkout.com') AND f.field_name IN ('employees','revenue') ORDER BY f.field_name, f.chosen DESC, f.id"
```

## Output

```
field      raw_value                                                                                 conf    chosen  source_type    retrieval
---------  ----------------------------------------------------------------------------------------  ------  ------  -------------  --------------
employees  About 2,000 people                                                                        high    1       wikipedia      fetched_page
employees  about 1,870 (GetLatka, 2024)                                                              medium  0       data_provider  search_snippet
employees  about 2,000 (Komo)                                                                        medium  0       data_provider  search_snippet
revenue    about $399 million (ZoomInfo)                                                             medium  1       data_provider  search_snippet
revenue    about $435 million (Zeliq)                                                                medium  0       data_provider  search_snippet
revenue    Crossed a $1 billion revenue milestone, with net revenue growth above 30% in 2025 (Komo)  medium  0       data_provider  search_snippet
```

The winning rows point to stored pages. Page 328 is the Wikipedia article (`https://en.wikipedia.org/wiki/Checkout.com`, fetched with WebFetch). Page 298 is a search result from `https://komo.ai/directory/checkout-com`, for the query "Checkout.com revenue employees headquarters founded". The normalized columns in `companies` hold the parsed numbers: `employees_min` and `employees_max` are 2000, and `revenue_usd_min` and `revenue_usd_max` are 399000000.

## Reading the result

**Employees.** The three sources disagree: about 2,000, about 1,870 and about 2,000 again. The value from Wikipedia wins because the parser marked it `high` (stated by the company, Wikipedia or a filing) and the other two `medium` (third-party estimates). The sources are close, so the choice barely matters here.

**Revenue.** This is the case where the rule is weak. All three candidates are `medium`, from the same data-provider page, so confidence, source type and page all tie, and the order the parser listed them in decides, so the first, $399 million, wins. One of the losing candidates says Checkout.com crossed $1 billion in revenue, a very different number. Nothing in the rule can tell which is right, and that is why every candidate is stored instead of only the winner: the disagreement stays visible, and a better tie-break can be added later without re-scraping.
