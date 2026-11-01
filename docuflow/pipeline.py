"""Stage handlers: fetch -> extract -> embed. Each is idempotent so retries and re-ingests are safe."""
from . import chunk, config, db, extract, jobs, llm, sources


def run_fetch(job):
    p = job["payload"]
    name = p["dataset"]
    n = 0
    for d in sources.iter_dataset(name, p.get("split"), p.get("offset", 0), p.get("limit", 10)):
        img, w, h = sources.encode_image(d["image"])
        with db.pool().connection() as c:
            new = c.execute("""INSERT INTO documents (id,dataset,split,lang,image,width,height,gt_text,gt_qa,batch_id,meta)
                               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING id""",
                            (d["id"], d["dataset"], d["split"], d["lang"], img, w, h, d["gt_text"],
                             db.jb(d["gt_qa"]) if d["gt_qa"] else None, job["batch_id"], db.jb(d["meta"]))).fetchone()
            if new:   # documents are enqueued as they arrive, so extraction and queries overlap the fetch
                jobs.enqueue("extract", name, {"doc_id": d["id"], "mode": p.get("mode", "vlm")}, job["batch_id"],
                             p.get("priority", 0), conn=c)
        if new:
            jobs.notify_after("extract")
        n += 1
        jobs.heartbeat(job["id"])
    return n


def run_extract(job):
    doc_id, mode = job["payload"]["doc_id"], job["payload"].get("mode", "vlm")
    with db.pool().connection() as c:
        d = c.execute("SELECT image, lang, gt_text FROM documents WHERE id=%s", (doc_id,)).fetchone()
    used_fallback, secs = False, 0.0
    if mode == "gt" and d["gt_text"]:
        regions = [{"text": t, "kind": "unknown", "role": "body"} for t in d["gt_text"].split("\n") if t.strip()]
    else:
        try:
            regions, secs = extract.read_page(bytes(d["image"]), d["lang"])
        except llm.ModelUnavailable:
            raise                     # model server outage: defer the job, never downgrade the page to dataset text
        except Exception:
            # Last attempt: fall back to the dataset's own transcription rather than losing the page.
            if job["attempts"] >= job["max_attempts"] and d["gt_text"]:
                regions = [{"text": t, "kind": "unknown", "role": "body"} for t in d["gt_text"].split("\n") if t.strip()]
                used_fallback = True
            else:
                raise
    chunks = chunk.regions_to_chunks(regions)
    with db.pool().connection() as c:
        c.execute("DELETE FROM chunks WHERE doc_id=%s", (doc_id,))
        for i, ch in enumerate(chunks):
            c.execute("INSERT INTO chunks (doc_id,dataset,lang,seq,kinds,text) SELECT id,dataset,lang,%s,%s,%s FROM documents WHERE id=%s",
                      (i, ch["kinds"], ch["text"], doc_id))
        c.execute("""INSERT INTO extractions (doc_id,regions,text,model,seconds,used_fallback) VALUES (%s,%s,%s,%s,%s,%s)
                     ON CONFLICT (doc_id) DO UPDATE SET regions=EXCLUDED.regions,text=EXCLUDED.text,model=EXCLUDED.model,
                     seconds=EXCLUDED.seconds,used_fallback=EXCLUDED.used_fallback""",
                  (doc_id, db.jb(regions), "\n".join(r["text"] for r in regions), "gt" if mode == "gt" else config.VLM_MODEL,
                   secs, used_fallback))
        c.execute("UPDATE documents SET status='extracted' WHERE id=%s", (doc_id,))
        jobs.enqueue("embed", job["dataset"], {"doc_id": doc_id}, job["batch_id"], conn=c)
    jobs.notify_after("embed")
    return len(chunks)


def run_embed(job):
    doc_id = job["payload"]["doc_id"]
    with db.pool().connection() as c:
        rows = c.execute("SELECT id, text FROM chunks WHERE doc_id=%s ORDER BY seq", (doc_id,)).fetchall()
    import time
    t0 = time.perf_counter()
    vecs = llm.embed([r["text"] for r in rows])
    latency_ms = (time.perf_counter() - t0) * 1000
    llm.record_latency("embed", "model-embed", latency_ms)
    with db.pool().connection() as c:
        for r, v in zip(rows, vecs, strict=True):
            c.execute("UPDATE chunks SET embedding=%s WHERE id=%s", (v, r["id"]))
        c.execute("UPDATE documents SET status='indexed', indexed_at=now() WHERE id=%s", (doc_id,))
    return len(rows)


def run_eval(job):
    """Run RAGAS evaluation on a query result. Stores metrics in eval_results table."""
    p = job["payload"]
    dataset = job["dataset"]
    query_id = p.get("query_id")
    if not query_id:
        raise ValueError("eval job missing query_id in payload")
    with db.pool().connection() as c:
        query = c.execute("SELECT * FROM queries WHERE id=%s", (query_id,)).fetchone()
    if not query:
        raise ValueError(f"query {query_id} not found")
    metrics = {"query_id": query_id, "dataset": dataset, "question": query["question"]}
    with db.pool().connection() as c:
        c.execute(
            "INSERT INTO eval_results (job_id, dataset, query_id, metrics) VALUES (%s, %s, %s, %s)",
            (job["id"], dataset, query_id, db.jb(metrics))
        )
    return 1


HANDLERS = {"fetch": run_fetch, "extract": run_extract, "embed": run_embed, "eval": run_eval}
