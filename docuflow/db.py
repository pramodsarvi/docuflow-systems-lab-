"""Postgres access: pool, schema, and small helpers. One database holds documents, chunks, and the job queue."""
import threading

import psycopg
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config

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
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED);
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
        finally:
            c.execute("SELECT pg_advisory_unlock(727)")


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
