"""Synthetic dataset, retention, and the worker health/metrics endpoint."""
import time

import httpx

from docuflow import config, db, jobs, pipeline, sources, worker


def test_synthetic_pages_are_deterministic_and_fetchable():
    a, b = list(sources.iter_dataset("synthetic", limit=2)), list(sources.iter_dataset("synthetic", offset=1, limit=1))
    assert a[1]["id"] == b[0]["id"] == "synthetic/gen/1" and a[1]["gt_text"] == b[0]["gt_text"]
    assert "Signed by:" in a[0]["gt_text"]
    jobs.enqueue("fetch", "synthetic", {"dataset": "synthetic", "offset": 0, "limit": 3, "mode": "gt"})
    db.control_set("lane_limits", {"io": 1})
    j = jobs.claim("io", "t")
    assert pipeline.run_fetch(j) == 3
    assert jobs.snapshot()["lanes"]["vlm"]["queued"] == 3      # one extract job per page, enqueued as fetched


def test_gc_removes_old_done_jobs_and_queries_only():
    for _ in range(3):
        jobs.enqueue("embed", "a", {})
    with db.pool().connection() as c:
        c.execute("UPDATE jobs SET status='done', finished_at=now() - interval '30 days' WHERE id=1")
        c.execute("UPDATE jobs SET status='dead', finished_at=now() - interval '30 days' WHERE id=2")
        c.execute("INSERT INTO queries (question, created_at) VALUES ('old', now() - interval '90 days'), ('new', now())")
    assert jobs.gc(done_days=7, query_days=30) == {"jobs_deleted": 1, "queries_deleted": 1, "stale_inflight_deleted": 0}
    with db.pool().connection() as c:
        assert c.execute("SELECT count(*) n FROM jobs").fetchone()["n"] == 2   # dead and queued are kept


def test_worker_health_and_metrics_endpoint():
    st = worker._State("vlm")
    srv = worker._serve(st, 0)
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert httpx.get(base + "/health").status_code == 200
        st.record("extract", "ok", 1.5)
        body = httpx.get(base + "/metrics").text
        assert 'docuflow_worker_jobs_total{lane="vlm",stage="extract",result="ok"} 1' in body
        st.last_loop = time.time() - 120                                 # idle loop stalled
        assert httpx.get(base + "/health").status_code == 503
        st.busy_since = time.time() - config.LEASE_SECONDS - 1          # job running past its lease
        assert httpx.get(base + "/health").status_code == 503
        st.busy_since = time.time()
        assert httpx.get(base + "/health").status_code == 200
    finally:
        srv.shutdown()


def test_search_finds_document_codes_among_many_similar_forms(monkeypatch):
    """Regression: 'INV-1005' used to tokenise as 'inv' + '-1005', and the text branch took 30 arbitrary matches."""
    import numpy as np
    from docuflow import llm, retrieval
    monkeypatch.setattr(llm, "embed", lambda texts, query=False: np.ones((len(texts), config.EMBED_DIM), dtype=np.float32))
    with db.pool().connection() as c:
        for i in range(60):
            _, gt = sources.synthetic_page(i)
            c.execute("INSERT INTO documents (id, dataset, status) VALUES (%s, 'synthetic', 'indexed')", (f"s/{i}",))
            c.execute("INSERT INTO chunks (doc_id, dataset, seq, kinds, text, embedding) VALUES (%s,'synthetic',0,'{printed}',%s,%s)",
                      (f"s/{i}", gt, np.ones(config.EMBED_DIM, dtype=np.float32)))
    _, gt = sources.synthetic_page(47)
    number = gt.splitlines()[1].split(": ")[1]
    hits = retrieval.search(f"Which city is listed on {number}?", k=3)
    assert hits[0]["doc_id"] == "s/47"


def test_migration_rebuilds_old_tsv_column():
    with db.pool().connection() as c:
        c.execute("ALTER TABLE chunks DROP COLUMN tsv")
        c.execute("ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED")
    db.init_schema()
    with db.pool().connection() as c:
        c.execute("INSERT INTO documents (id, dataset) VALUES ('m/1', 'x')")
        c.execute("INSERT INTO chunks (doc_id, dataset, text) VALUES ('m/1', 'x', 'Number: PUR-1106')")
        assert c.execute("SELECT count(*) n FROM chunks WHERE tsv @@ to_tsquery('simple', '1106')").fetchone()["n"] == 1
