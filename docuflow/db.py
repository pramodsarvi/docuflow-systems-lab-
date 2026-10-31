"""Postgres access: pool, schema, and small helpers. One database holds documents, chunks, and the job queue."""
import threading

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config

# Full-text vector for chunks. Hyphens between letters/digits become spaces first: the default parser reads "INV-1005"
# as 'inv' + '-1005' (a negative number), so a search for "1005" would never match the document code.
TSV_EXPR = """to_tsvector('simple', regexp_replace(text, '([[:alnum:]])-([[:alnum:]])', '\\1 \\2', 'g'))"""

SCHEMA = f"""
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS documents (
  id text PRIMARY KEY, dataset text NOT NULL, split text, lang text DEFAULT 'en',
  image bytea, width int, height int,
  gt_text text,                       -- ground-truth transcription when the dataset ships one
  gt_qa jsonb,                        -- [{{question, answers}}] for DocVQA
  status text NOT NULL DEFAULT 'fetched',  -- fetched -> extracted -> indexed
  batch_id bigint, meta jsonb DEFAULT '{{}}', created_at timestamptz DEFAULT now(), indexed_at timestamptz);
CREATE INDEX IF NOT EXISTS documents_ds ON documents(dataset, status);
CREATE TABLE IF NOT EXISTS chunks (
  id bigserial PRIMARY KEY, doc_id text NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  dataset text NOT NULL, lang text, seq int, kinds text[], text text NOT NULL,
  embedding vector({config.EMBED_DIM}),
  tsv tsvector GENERATED ALWAYS AS ({TSV_EXPR}) STORED);
CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv);
CREATE INDEX IF NOT EXISTS chunks_emb ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE TABLE IF NOT EXISTS extractions (       -- raw VLM output kept for evals and debugging
  doc_id text PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE, regions jsonb, text text,
  model text, seconds real, used_fallback boolean DEFAULT false);
CREATE TABLE IF NOT EXISTS batches (
  id bigserial PRIMARY KEY, dataset text NOT NULL, request jsonb, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY, batch_id bigint, dataset text NOT NULL, stage text NOT NULL, lane text NOT NULL,
  priority int NOT NULL DEFAULT 0, payload jsonb NOT NULL DEFAULT '{{}}',
  status text NOT NULL DEFAULT 'queued',   -- queued | running | done | dead
  attempts int NOT NULL DEFAULT 0, max_attempts int NOT NULL DEFAULT {config.MAX_ATTEMPTS},
  run_after timestamptz NOT NULL DEFAULT now(), locked_by text, locked_at timestamptz,
  error text, created_at timestamptz DEFAULT now(), started_at timestamptz, finished_at timestamptz);
CREATE INDEX IF NOT EXISTS jobs_claim ON jobs(lane, status, priority DESC, id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS outages int NOT NULL DEFAULT 0;   -- deferrals caused by model-server outages
CREATE TABLE IF NOT EXISTS control (key text PRIMARY KEY, value jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS inflight (id text PRIMARY KEY, beat timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS queries (
  id bigserial PRIMARY KEY, question text, answer text, trace jsonb, seconds real,
  during_ingest boolean, created_at timestamptz DEFAULT now());
"""

_pool = None
_lock = threading.Lock()


def _configure(conn):
    register_vector(conn)


def pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(config.DSN, min_size=1, max_size=8, kwargs={"row_factory": dict_row},
                                   configure=_configure, check=ConnectionPool.check_connection,
                                   open=True)
    return _pool


def init_schema():
    # The vector type must exist before pgvector can be registered on pooled connections.
    with psycopg.connect(config.DSN, autocommit=True) as c:
        c.execute("SELECT pg_advisory_lock(727)")      # several pods may initialise at once
        try:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
            with pool().connection() as c2:
                c2.execute(SCHEMA)
                _migrate(c2)
        finally:
            c.execute("SELECT pg_advisory_unlock(727)")


def _migrate(c):
    """In-place upgrades for databases created by older versions (runs under the schema advisory lock)."""
    expr = c.execute("""SELECT pg_get_expr(d.adbin, d.adrelid) e FROM pg_attrdef d JOIN pg_attribute a
                        ON a.attrelid=d.adrelid AND a.attnum=d.adnum WHERE d.adrelid='chunks'::regclass AND a.attname='tsv'""").fetchone()
    if expr and "regexp_replace" not in expr["e"]:      # v0.1 tokenised "INV-1005" as 'inv' + '-1005'
        c.execute("ALTER TABLE chunks DROP COLUMN tsv")
        c.execute(f"ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS ({TSV_EXPR}) STORED")
        c.execute("CREATE INDEX IF NOT EXISTS chunks_tsv ON chunks USING gin(tsv)")


def reset():
    with pool().connection() as c:
        c.execute("TRUNCATE chunks, extractions, jobs, batches, documents, queries, inflight, control RESTART IDENTITY CASCADE")


def jb(x):
    return Jsonb(x)


def control_get(key, default=None):
    with pool().connection() as c:
        r = c.execute("SELECT value FROM control WHERE key=%s", (key,)).fetchone()
    return r["value"] if r else default


def control_set(key, value):
    with pool().connection() as c:
        c.execute("INSERT INTO control VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, jb(value)))
