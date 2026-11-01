"""Stage handlers: fetch -> extract -> embed. Each is idempotent so retries and re-ingests are safe."""
import time

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
    t0 = time.perf_counter()
    vecs = llm.embed([r["text"] for r in rows])
    latency_ms = (time.perf_counter() - t0) * 1000
    llm.record_latency("embed", "model-embed", latency_ms)
    with db.pool().connection() as c:
        for r, v in zip(rows, vecs, strict=True):
            c.execute("UPDATE chunks SET embedding=%s WHERE id=%s", (v, r["id"]))
        c.execute("UPDATE documents SET status='indexed', indexed_at=now() WHERE id=%s", (doc_id,))
    return len(rows)


_CONTEXT_ACTIONS = ("search", "get_page", "read_page_image")


def _ragas_scorer():
    """Reference-free RAGAS metrics, judged by the agent model and embedded by the embed model. Built once per process.
    RAGAS (and LangChain) are only in images built with requirements-eval.txt; see the Dockerfile."""
    global _SCORER
    if _SCORER is None:
        from langchain_openai import ChatOpenAI, OpenAIEmbeddings
        from ragas import EvaluationDataset, RunConfig, evaluate
        from ragas.embeddings import LangchainEmbeddingsWrapper
        from ragas.llms import LangchainLLMWrapper
        from ragas.metrics import Faithfulness, ResponseRelevancy
        llm_url, llm_key = config.ENDPOINTS["llm"]
        emb_url, emb_key = config.ENDPOINTS["embed"]
        if not llm_url or not emb_url:
            raise RuntimeError("eval needs LLM_BASE_URL and EMBED_BASE_URL (OpenAI-compatible model servers)")
        judge = LangchainLLMWrapper(ChatOpenAI(base_url=llm_url, api_key=llm_key or "none", model=config.LLM_MODEL,
                                               temperature=0, max_tokens=2048, timeout=300))
        emb = LangchainEmbeddingsWrapper(OpenAIEmbeddings(base_url=emb_url, api_key=emb_key or "none",
                                                          model=config.EMBED_MODEL, check_embedding_ctx_length=False))
        metrics = [Faithfulness(), ResponseRelevancy()]

        def score(question, answer, contexts):
            ds = EvaluationDataset.from_list([{"user_input": question, "response": answer,
                                               "retrieved_contexts": contexts}])
            res = evaluate(ds, metrics=metrics, llm=judge, embeddings=emb, show_progress=False,
                           run_config=RunConfig(max_workers=1, timeout=300, max_retries=3), raise_exceptions=True)
            row = res.to_pandas().iloc[0]
            return {m.name: (None if row.get(m.name) != row.get(m.name) else round(float(row[m.name]), 3))
                    for m in metrics}
        _SCORER = score
    return _SCORER


_SCORER = None


def run_eval(job):
    """Score one answered query with RAGAS (faithfulness, answer relevancy) and store the result in eval_results.
    Contexts are the observations the agent actually saw, taken from the query trace."""
    query_id = job["payload"].get("query_id")
    if not query_id:
        raise ValueError("eval job missing query_id in payload")
    with db.pool().connection() as c:
        q = c.execute("SELECT question, answer, trace FROM queries WHERE id=%s", (query_id,)).fetchone()
    if not q:
        return 0   # query was garbage-collected; nothing to score
    contexts = [t["observation"][:3000] for t in (q["trace"] or [])
                if t.get("action") in _CONTEXT_ACTIONS and t.get("observation") and t["observation"] != "No results."]
    if not q["answer"] or not contexts:
        scores = {"skipped": "no answer" if not q["answer"] else "no retrieved context"}
    else:
        scores = _ragas_scorer()(q["question"], q["answer"], contexts)
    with db.pool().connection() as c:
        c.execute("INSERT INTO eval_results (job_id, dataset, query_id, metrics) VALUES (%s, %s, %s, %s)",
                  (job["id"], job["dataset"], query_id, db.jb(scores)))
    return 1


HANDLERS = {"fetch": run_fetch, "extract": run_extract, "embed": run_embed, "eval": run_eval}
