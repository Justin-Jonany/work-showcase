CREATE TABLE IF NOT EXISTS scrape_jobs (
    id            INTEGER PRIMARY KEY,
    company_name  TEXT NOT NULL,
    domain        TEXT NOT NULL UNIQUE,
    sector        TEXT,
    prompt        TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','searched','parsed','ingested','failed')),
    error         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_pages (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL REFERENCES scrape_jobs(id),
    url           TEXT NOT NULL,
    source_type   TEXT NOT NULL
                  CHECK (source_type IN ('company_site','wikipedia','news','data_provider','other')),
    retrieval     TEXT NOT NULL CHECK (retrieval IN ('search_snippet','fetched_page')),
    query         TEXT,
    text          TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    fetched_at    TEXT NOT NULL,
    UNIQUE (job_id, url, content_hash)
);

CREATE TABLE IF NOT EXISTS companies (
    id                          INTEGER PRIMARY KEY,
    job_id                      INTEGER UNIQUE REFERENCES scrape_jobs(id),
    name                        TEXT NOT NULL,
    domain                      TEXT NOT NULL UNIQUE,
    description                 TEXT,
    product_description         TEXT,
    business_model_description  TEXT,
    customer_description        TEXT,
    revenue_raw                 TEXT,
    revenue_usd_min             REAL,
    revenue_usd_max             REAL,
    revenue_year                INTEGER,
    web_traffic_raw             TEXT,
    monthly_visits              INTEGER,
    employees_raw               TEXT,
    employees_min               INTEGER,
    employees_max               INTEGER,
    founded_year                INTEGER,
    headquarters                TEXT,
    profile_text                TEXT,
    field_coverage              REAL,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_locations (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    location_raw  TEXT NOT NULL,
    city          TEXT,
    country       TEXT,
    is_hq         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (company_id, location_raw)
);

CREATE TABLE IF NOT EXISTS field_sources (
    id            INTEGER PRIMARY KEY,
    company_id    INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    field_name    TEXT NOT NULL,
    raw_value     TEXT NOT NULL,
    page_id       INTEGER REFERENCES raw_pages(id),
    source_url    TEXT,
    confidence    TEXT CHECK (confidence IN ('high','medium','low')),
    chosen        INTEGER NOT NULL DEFAULT 0,
    extracted_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
    company_id  INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    dim         INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    text_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_judgments (
    query_key             TEXT NOT NULL,
    candidate_company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    prompt_version        TEXT NOT NULL,
    score                 INTEGER NOT NULL CHECK (score BETWEEN 0 AND 10),
    reason                TEXT,
    PRIMARY KEY (query_key, candidate_company_id, prompt_version)
);

CREATE TABLE IF NOT EXISTS rank_runs (
    id                 INTEGER PRIMARY KEY,
    query_text         TEXT NOT NULL,
    anchor_company_id  INTEGER REFERENCES companies(id),
    query_key          TEXT NOT NULL,
    params_json        TEXT,
    created_at         TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'similar'  -- 'similar', 'search' or 'acquisition'
);

CREATE TABLE IF NOT EXISTS rank_results (
    run_id       INTEGER NOT NULL REFERENCES rank_runs(id) ON DELETE CASCADE,
    company_id   INTEGER NOT NULL REFERENCES companies(id),
    bm25         REAL,
    bm25_rank    INTEGER,
    cosine       REAL,
    cosine_rank  INTEGER,
    llm_score    INTEGER,
    llm_rank     INTEGER,
    rrf_score    REAL,
    final_rank   INTEGER,
    fit_score    REAL,     -- size/financial fit signal, NULL when unused
    fit_rank     INTEGER,
    PRIMARY KEY (run_id, company_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_pages_job ON raw_pages(job_id);
CREATE INDEX IF NOT EXISTS idx_field_sources_company ON field_sources(company_id, field_name);
