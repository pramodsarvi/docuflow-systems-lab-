from fastapi.testclient import TestClient

from docuflow import api, jobs


def test_ready_and_metrics():
    jobs.enqueue("extract", "funsd", {})
    c = TestClient(api.app)
    assert c.get("/ready").json() == {"ready": True}
    body = c.get("/metrics").text
    assert 'docuflow_jobs{lane="vlm",status="queued"} 1' in body
    assert "docuflow_queries_in_flight" in body


def test_list_documents_filters():
    from docuflow import db
    with db.pool().connection() as c:
        c.execute("INSERT INTO documents (id, dataset, status) VALUES ('s/1','synthetic','indexed'), ('s/2','synthetic','fetched'), ('f/1','funsd','indexed')")
    c = TestClient(api.app)
    assert {d["id"] for d in c.get("/documents?dataset=synthetic").json()} == {"s/1", "s/2"}
    assert [d["id"] for d in c.get("/documents?ids=s/2,f/1&status=indexed").json()] == ["f/1"]


def test_queries_can_be_paused_for_ingest_priority():
    c = TestClient(api.app)
    assert c.post("/workloads/queries", json={"paused": True}).json() == {"queries_paused": True}
    r = c.post("/query", json={"question": "anything"})
    assert r.status_code == 503 and r.headers["retry-after"] == "300"
    assert c.get("/workloads").json()["queries_paused"] is True
    c.post("/workloads/queries", json={"paused": False})
    assert c.get("/workloads").json()["queries_paused"] is False


def test_operator_metrics_present():
    from docuflow import db
    with db.pool().connection() as c:
        c.execute("INSERT INTO queries (question, seconds) VALUES ('a', 2.0), ('b', 4.0)")
        c.execute("INSERT INTO documents (id, dataset, status, indexed_at) VALUES ('m/1','funsd','indexed', now())")
        c.execute("INSERT INTO extractions (doc_id, seconds, used_fallback) VALUES ('m/1', 12.5, false)")
    body = TestClient(api.app).get("/metrics").text
    assert 'docuflow_query_latency_seconds{quantile="0.50"} 3.0' in body
    assert 'docuflow_read_seconds_avg{dataset="funsd"} 12.5' in body
    assert "docuflow_queries_paused 0" in body and "docuflow_info{agent_backend=" in body
