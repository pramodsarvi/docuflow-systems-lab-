"""Hybrid retrieval over whatever has been indexed so far: pgvector cosine + Postgres full-text, fused with RRF."""
import re

from . import db, llm

_WORD = re.compile(r"\w{3,}", re.U)


def search(query, k=5, dataset=None, kind=None, lang=None, pool_size=30):
    qv = llm.embed([query], query=True)[0]
    words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query)))[:24]
    tsq = " | ".join(words) if words else None
    filt, args = "embedding IS NOT NULL", {}
    if dataset:
        filt += " AND dataset = %(dataset)s"; args["dataset"] = dataset
    if lang:
        filt += " AND lang = %(lang)s"; args["lang"] = lang
    if kind:
        filt += " AND %(kind)s = ANY(kinds)"; args["kind"] = kind
    sql = f"""
    WITH v AS (SELECT id, row_number() OVER (ORDER BY embedding <=> %(qv)s) r FROM chunks WHERE {filt}
               ORDER BY embedding <=> %(qv)s LIMIT %(pool)s),
         t AS (SELECT id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) r
               FROM chunks, to_tsquery('simple', %(tsq)s) q WHERE %(tsq)s IS NOT NULL AND tsv @@ q AND {filt} LIMIT %(pool)s)
    SELECT c.id, c.doc_id, c.dataset, c.lang, c.kinds, c.text,
           coalesce(1.0/(60+v.r),0) + coalesce(1.0/(60+t.r),0) AS score
    FROM chunks c LEFT JOIN v ON v.id=c.id LEFT JOIN t ON t.id=c.id
    WHERE v.id IS NOT NULL OR t.id IS NOT NULL ORDER BY score DESC LIMIT %(k)s"""
    with db.pool().connection() as c:
        return c.execute(sql, {**args, "qv": qv, "tsq": tsq, "pool": pool_size, "k": k}).fetchall()


def page_text(doc_id):
    with db.pool().connection() as c:
        rows = c.execute("SELECT text FROM chunks WHERE doc_id=%s ORDER BY seq", (doc_id,)).fetchall()
    return "\n".join(r["text"] for r in rows)


def corpus_stats():
    with db.pool().connection() as c:
        docs = c.execute("SELECT dataset, status, count(*) n FROM documents GROUP BY 1,2 ORDER BY 1,2").fetchall()
        pending = c.execute("SELECT count(*) n FROM jobs WHERE status IN ('queued','running')").fetchone()["n"]
        kinds = c.execute("SELECT k, count(*) n FROM chunks, unnest(kinds) k GROUP BY k").fetchall()
    return {"documents": docs, "pending_jobs": pending, "chunks_by_kind": {r["k"]: r["n"] for r in kinds}}
