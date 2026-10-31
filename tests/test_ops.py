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


def test_agent_presearch_saves_a_round_trip_and_traces(monkeypatch):
    """With presearch the first LLM call already sees search results; spans are exported in OpenInference shape."""
    import json as _json

    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from docuflow import agent, llm, retrieval, tracing
    exp = InMemorySpanExporter()
    provider = TracerProvider(); provider.add_span_processor(SimpleSpanProcessor(exp))
    monkeypatch.setattr(tracing, "_tracer", provider.get_tracer("t")); monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(retrieval, "search", lambda q, **kw: [{"doc_id": "s/1", "dataset": "synthetic", "lang": "en",
                                                                 "kinds": ["printed"], "text": "Signed by: Maria Lopez"}])
    calls = []

    def fake_chat(msgs, **kw):
        calls.append(msgs[-1]["content"])
        return _json.dumps({"thought": "found", "action": "answer", "answer": "S-1 was signed by Maria Lopez.",
                            "citations": ["s/1"]}), {"output_tokens": 12}
    monkeypatch.setattr(llm, "chat", fake_chat)
    r = agent.run("Who signed S-1?")
    assert len(calls) == 1 and "Maria Lopez" in calls[0]          # one LLM call, evidence already in the prompt
    assert r["citations"] == ["s/1"] and r["trace_id"]
    spans = {s.name: s for s in exp.get_finished_spans()}
    assert spans["docuflow.agent"].attributes["openinference.span.kind"] == "AGENT"
    assert spans["docuflow.agent"].attributes["output.value"] == "S-1 was signed by Maria Lopez."
    assert spans["tool.search"].attributes["openinference.span.kind"] == "TOOL"
    assert spans["llm.step.0"].attributes["docuflow.action"] == "answer"
    assert format(spans["llm.step.0"].context.trace_id, "032x") == r["trace_id"]


def test_ingest_backs_off_under_query_pressure(monkeypatch):
    monkeypatch.setattr(config, "QUERY_PRESSURE_INFLIGHT", 2); monkeypatch.setattr(config, "VLM_LIMIT_UNDER_QUERY_LOAD", 1)
    monkeypatch.setattr(config, "YIELD_TO_QUERIES", False); monkeypatch.setattr(config, "PER_DATASET_CAP", 10)
    db.control_set("lane_limits", {"vlm": 5})
    for _ in range(3):
        jobs.enqueue("extract", "a", {})
    with db.pool().connection() as c:
        c.execute("INSERT INTO inflight (id) VALUES ('q1'), ('q2')")
    assert jobs.claim("vlm", "w1") and jobs.claim("vlm", "w2") is None    # limited to 1 while 2 queries are in flight
    with db.pool().connection() as c:
        c.execute("DELETE FROM inflight")
    assert jobs.claim("vlm", "w2")                                         # pressure gone: back to the lane limit


def test_truncated_vlm_output_keeps_completed_regions():
    from docuflow import extract
    cut = ('{\n  "regions": [\n    {"text": "W. K. KELLOGG FOUNDATION", "kind": "printed", "role": "header"},\n'
           '    {"text": "Grant \\"A\\" total", "kind": "printed"},\n    {"text": "Sue Wa')
    regions = extract.salvage_regions(cut)
    assert regions == [{"text": "W. K. KELLOGG FOUNDATION", "kind": "printed", "role": "header"},
                       {"text": 'Grant "A" total', "kind": "printed", "role": "body"}]
    assert extract.salvage_regions("not json at all") == []
